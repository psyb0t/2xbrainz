"""Direct, bounded Talkies native-WebSocket client."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import math
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from two_x_brainz.audio import WavFixture
from two_x_brainz.constants import (
    BEARER_PREFIX,
    DEFAULT_CHANNELS,
    DEFAULT_FRAME_BYTES,
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_SAMPLE_RATE_HZ,
    DEFAULT_WS_CLOSE_TIMEOUT_SECONDS,
    DEFAULT_WS_PING_INTERVAL_SECONDS,
    HEADER_AUTHORIZATION,
    MAX_EVENT_BYTES,
    MAX_PROVIDER_RESPONSE_BYTES,
    TALKIES_BATCH_PATH,
    TALKIES_MODELS_PATH,
    TALKIES_STREAM_PATH,
)
from two_x_brainz.contracts import (
    ASRStreamStats,
    AudioFrame,
    SpeakerRole,
    TranscriptEvent,
    TranscriptEventType,
    WordTiming,
)
from two_x_brainz.errors import ConfigurationError, ProtocolError, RemoteServiceError
from two_x_brainz.json_support import (
    decode_json,
    require_json_array,
    require_json_object,
)

_START_EVENT = "start"
_END_EVENT = "end"
_READY_EVENT = "ready"
_STATS_EVENT = "stats"
_ERROR_EVENT = "error"
_PCM_S16LE = "pcm_s16le"
_TRANSCRIPT_TYPES = frozenset(item.value for item in TranscriptEventType)
_BATCH_JSON_RESPONSE_FORMAT = "json"
_BATCH_VERBOSE_JSON_RESPONSE_FORMAT = "verbose_json"
_MULTIPART_FILE_NAME = "fixture.wav"
_MULTIPART_FILE_FIELD = "file"
_MULTIPART_MODEL_FIELD = "model"
_MULTIPART_RESPONSE_FORMAT_FIELD = "response_format"
_WARMUP_SESSION_ID = "talkies-warmup"
_WARMUP_STREAM_ID = "talkies-warmup-stream"
_WARMUP_FRAME_COUNT = 1
_WARMUP_FRAME_SEQUENCE = 0
_WARMUP_CAPTURED_AT_MONOTONIC = 0.0


class BatchResponseFormat(StrEnum):
    """The OpenAI-compatible response contracts used for file checks."""

    JSON = _BATCH_JSON_RESPONSE_FORMAT
    VERBOSE_JSON = _BATCH_VERBOSE_JSON_RESPONSE_FORMAT


@dataclass(frozen=True, slots=True)
class BatchTranscription:
    """Validated file-transcription metadata retained without rendering text."""

    text: str
    duration_seconds: float | None
    segment_count: int | None
    word_count: int | None


@dataclass(frozen=True, slots=True)
class TalkiesStreamConfig:
    """The fixed wire settings for one Talkies ASR connection."""

    url: str
    model: str
    token: str | None
    language: str | None = None


class TalkiesClient:
    """Converts one PCM stream into normalized transcript events."""

    def __init__(self, config: TalkiesStreamConfig) -> None:
        self._config = config

    async def transcribe(
        self,
        *,
        session_id: str,
        stream_id: str,
        speaker_role: SpeakerRole,
        frames: AsyncIterable[AudioFrame],
    ) -> AsyncIterator[TranscriptEvent | ASRStreamStats]:
        """Stream PCM frames and yield validated Talkies events in order."""
        headers = _authorization_headers(self._config.token)
        start = _start_message(self._config.model, self._config.language)
        transcript_reconciler = UtteranceReconciler()
        try:
            async with connect(
                self._config.url,
                additional_headers=headers,
                compression=None,
                ping_interval=DEFAULT_WS_PING_INTERVAL_SECONDS,
                close_timeout=DEFAULT_WS_CLOSE_TIMEOUT_SECONDS,
                max_queue=16,
            ) as socket:
                await socket.send(json.dumps(start, separators=(",", ":")))
                ready = await socket.recv()
                _validate_ready(ready, self._config.model)
                sender_context = contextvars.copy_context()
                sender = sender_context.run(
                    asyncio.create_task,
                    _send_frames(socket, frames),
                )
                try:
                    async for message in _receive_messages(
                        aiter(socket),
                        sender,
                    ):
                        event = parse_talkies_event(
                            message=message,
                            session_id=session_id,
                            stream_id=stream_id,
                            speaker_role=speaker_role,
                            model=self._config.model,
                        )
                        if event is not None:
                            if isinstance(event, TranscriptEvent):
                                event = transcript_reconciler.apply(event)
                            yield event
                finally:
                    sender.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await sender
        except WebSocketException as error:
            raise RemoteServiceError("Talkies WebSocket session failed") from error
        except OSError as error:
            raise RemoteServiceError("connect to Talkies") from error

    async def transcribe_file(
        self,
        fixture: WavFixture,
        response_format: BatchResponseFormat,
    ) -> BatchTranscription:
        """Send one bounded WAV through Talkies' OpenAI-compatible file route."""
        return await asyncio.to_thread(
            _post_file_transcription,
            batch_url(self._config.url),
            self._config.model,
            self._config.token,
            fixture.wav_bytes,
            response_format,
        )

    async def verify_configured_model(self) -> None:
        """Reject a benchmark model that Talkies does not currently expose."""
        model_ids = await asyncio.to_thread(
            _get_model_ids,
            models_url(self._config.url),
            self._config.token,
        )
        if self._config.model in model_ids:
            return
        raise RemoteServiceError("configured Talkies model is not available")

    async def configured_model_max_concurrency(self) -> int:
        """Return the selected model's validated advertised request limit."""
        return await asyncio.to_thread(
            _get_model_max_concurrency,
            models_url(self._config.url),
            self._config.token,
            self._config.model,
        )

    async def warm_configured_model(self) -> None:
        """Materialize one backend with synthetic silence before live audio."""
        async for event in self.transcribe(
            session_id=_WARMUP_SESSION_ID,
            stream_id=_WARMUP_STREAM_ID,
            speaker_role=SpeakerRole.REMOTE,
            frames=_warmup_frames(),
        ):
            if not isinstance(event, ASRStreamStats):
                continue
            if event.canceled:
                raise ProtocolError("Talkies canceled the serial model warm-up")
            if event.frames != _WARMUP_FRAME_COUNT:
                raise ProtocolError(
                    "Talkies warm-up reported an unexpected frame count"
                )
            return
        raise RemoteServiceError("Talkies warm-up ended without terminal statistics")


class UtteranceReconciler:
    """Build one visible utterance from timestamped native ASR fragments."""

    def __init__(self) -> None:
        self._timed_words: dict[tuple[int, int, str], WordTiming] = {}
        self._last_visible_event: TranscriptEvent | None = None

    def apply(self, event: TranscriptEvent) -> TranscriptEvent:
        reconciled_event = event
        if _has_complete_word_timing(event.words):
            for word in event.words:
                assert word.start_ms is not None
                assert word.end_ms is not None
                self._timed_words[(word.start_ms, word.end_ms, word.word)] = word
            words = tuple(
                sorted(
                    self._timed_words.values(),
                    key=lambda word: (word.start_ms, word.end_ms, word.word),
                )
            )
            started_at_ms, ended_at_ms = _timing_bounds(words)
            reconciled_event = replace(
                event,
                text=" ".join(word.word for word in words),
                words=words,
                started_at_ms=started_at_ms,
                ended_at_ms=ended_at_ms,
                confidence=min(
                    (word.confidence for word in words if word.confidence is not None),
                    default=event.confidence,
                ),
            )
        if reconciled_event.text.strip():
            self._remember_visible_event(reconciled_event)
        if event.is_final and not reconciled_event.text.strip():
            reconciled_event = self._reconcile_empty_final(event)
        if event.is_final:
            self._timed_words.clear()
            self._last_visible_event = None
        return reconciled_event

    def _remember_visible_event(self, event: TranscriptEvent) -> None:
        if self._last_visible_event is None:
            self._last_visible_event = event
            return
        if len(event.text) >= len(self._last_visible_event.text):
            self._last_visible_event = event

    def _reconcile_empty_final(self, event: TranscriptEvent) -> TranscriptEvent:
        previous = self._last_visible_event
        if previous is None:
            return event
        return replace(
            event,
            text=previous.text,
            words=previous.words,
            started_at_ms=previous.started_at_ms,
            ended_at_ms=previous.ended_at_ms,
            confidence=previous.confidence,
            language=event.language or previous.language,
        )


async def _send_frames(socket: Any, frames: AsyncIterable[AudioFrame]) -> None:
    async for frame in frames:
        if len(frame.samples) != DEFAULT_FRAME_BYTES:
            raise ProtocolError("capture emitted a non-standard PCM16LE frame")
        if frame.sample_rate_hz != DEFAULT_SAMPLE_RATE_HZ:
            raise ProtocolError("capture emitted an unsupported sample rate")
        if frame.channels != DEFAULT_CHANNELS:
            raise ProtocolError("capture emitted an unsupported channel count")
        await socket.send(frame.samples)
    await socket.send(json.dumps({"type": _END_EVENT}, separators=(",", ":")))


async def _warmup_frames() -> AsyncIterator[AudioFrame]:
    """Feed one bounded silence frame so lazy ASR backends materialize."""
    yield AudioFrame(
        session_id=_WARMUP_SESSION_ID,
        stream_id=_WARMUP_STREAM_ID,
        speaker_role=SpeakerRole.REMOTE,
        sequence=_WARMUP_FRAME_SEQUENCE,
        captured_at_monotonic=_WARMUP_CAPTURED_AT_MONOTONIC,
        sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
        channels=DEFAULT_CHANNELS,
        samples=bytes(DEFAULT_FRAME_BYTES),
    )


async def _receive_messages(
    messages: AsyncIterator[str | bytes],
    sender: asyncio.Task[None],
) -> AsyncIterator[str | bytes]:
    """Yield server events while surfacing a failed PCM sender immediately."""
    while not sender.done():
        receiver = asyncio.create_task(_receive_next(messages))
        completed, _ = await asyncio.wait(
            (receiver, sender),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if sender in completed:
            try:
                await sender
            except BaseException:
                receiver.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await receiver
                raise
            if receiver in completed:
                message = _received_message(receiver)
                if message is None:
                    return
                yield message
            else:
                message = await receiver
                if message is None:
                    return
                yield message
            break
        message = _received_message(receiver)
        if message is None:
            return
        yield message

    await sender
    async for message in messages:
        yield message


async def _receive_next(messages: AsyncIterator[str | bytes]) -> str | bytes | None:
    try:
        return await anext(messages)
    except StopAsyncIteration:
        return None


def _received_message(
    receiver: asyncio.Task[str | bytes | None],
) -> str | bytes | None:
    """Extract one already-completed WebSocket receive task."""
    return receiver.result()


def parse_talkies_event(
    *,
    message: str | bytes,
    session_id: str,
    stream_id: str,
    speaker_role: SpeakerRole,
    model: str,
) -> TranscriptEvent | ASRStreamStats | None:
    """Validate and normalize one server message without trusting its shape."""
    if isinstance(message, bytes):
        raise ProtocolError("Talkies sent a binary message where JSON was required")
    if len(message.encode("utf-8")) > MAX_EVENT_BYTES:
        raise ProtocolError("Talkies event exceeds the configured size limit")
    try:
        decoded = require_json_object(decode_json(message))
    except json.JSONDecodeError as error:
        raise ProtocolError("Talkies returned invalid JSON") from error
    except ValueError as error:
        raise ProtocolError("Talkies event must be a JSON object") from error

    event_type = _require_text(decoded, "type")
    if event_type == _ERROR_EVENT:
        detail = decoded.get("detail", "Talkies reported an unspecified error")
        raise RemoteServiceError(f"Talkies error: {detail}")
    if event_type == _READY_EVENT:
        return None
    if event_type == _STATS_EVENT:
        return ASRStreamStats(
            session_id=session_id,
            stream_id=stream_id,
            speaker_role=speaker_role,
            asr_model=model,
            audio_seconds=_require_nonnegative_number(decoded, "audio_seconds"),
            frames=_require_nonnegative_int(decoded, "frames"),
            canceled=_require_bool(decoded, "canceled"),
        )
    if event_type not in _TRANSCRIPT_TYPES:
        raise ProtocolError(f"Talkies sent an unsupported event type: {event_type}")

    revision = _require_nonnegative_int(decoded, "revision")
    text = _require_text(decoded, "text")
    audio_seconds = _require_nonnegative_number(decoded, "audio_seconds")
    words = _parse_words(decoded.get("words", []))
    started_at_ms, ended_at_ms = _timing_bounds(words)
    transcript_type = TranscriptEventType(event_type)
    return TranscriptEvent(
        session_id=session_id,
        stream_id=stream_id,
        utterance_id=f"{stream_id}:{revision}",
        revision=revision,
        speaker_role=speaker_role,
        source_event_type=transcript_type,
        asr_model=model,
        text=text,
        is_final=transcript_type is TranscriptEventType.FINAL,
        audio_seconds=audio_seconds,
        words=words,
        started_at_ms=started_at_ms,
        ended_at_ms=ended_at_ms,
        confidence=_optional_confidence(decoded.get("confidence")),
        language=_optional_language(decoded.get("language")),
    )


def _start_message(model: str, language: str | None) -> dict[str, object]:
    message: dict[str, object] = {
        "type": _START_EVENT,
        "model": model,
        "encoding": _PCM_S16LE,
        "sample_rate": DEFAULT_SAMPLE_RATE_HZ,
        "channels": DEFAULT_CHANNELS,
        "interim_results": True,
        "word_timestamps": True,
    }
    if language is not None:
        message["language"] = language
    return message


def _authorization_headers(token: str | None) -> dict[str, str] | None:
    if token is None:
        return None
    return {HEADER_AUTHORIZATION: f"{BEARER_PREFIX}{token}"}


def batch_url(stream_url: str) -> str:
    """Derive Talkies' file-transcription URL from its native stream URL."""
    return _http_url_from_stream_url(stream_url, TALKIES_BATCH_PATH)


def models_url(stream_url: str) -> str:
    """Derive Talkies' OpenAI-compatible model-inventory URL."""
    return _http_url_from_stream_url(stream_url, TALKIES_MODELS_PATH)


def _http_url_from_stream_url(stream_url: str, endpoint_path: str) -> str:
    """Preserve an optional reverse-proxy prefix while changing the endpoint."""
    parsed = urlsplit(stream_url)
    if not parsed.path.endswith(TALKIES_STREAM_PATH):
        raise ConfigurationError(
            "Talkies stream URL must end with the native streaming endpoint"
        )
    scheme = "https" if parsed.scheme == "wss" else "http"
    proxy_prefix = parsed.path.removesuffix(TALKIES_STREAM_PATH)
    return urlunsplit((scheme, parsed.netloc, f"{proxy_prefix}{endpoint_path}", "", ""))


def _get_model_ids(url: str, token: str | None) -> frozenset[str]:
    return parse_model_inventory(_get_model_inventory_payload(url, token))


def _get_model_max_concurrency(
    url: str,
    token: str | None,
    model_id: str,
) -> int:
    return parse_model_max_concurrency(
        _get_model_inventory_payload(url, token),
        model_id,
    )


def _get_model_inventory_payload(
    url: str,
    token: str | None,
) -> Mapping[str, object]:
    headers = _authorization_headers(token) or {}
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(
            request,
            timeout=DEFAULT_HTTP_TIMEOUT.total_seconds(),
        ) as response:
            raw_response = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
            status_code = response.status
    except HTTPError as error:
        raise RemoteServiceError(
            f"Talkies model inventory returned HTTP {error.code}"
        ) from error
    except URLError as error:
        raise RemoteServiceError("connect to Talkies model inventory") from error
    except OSError as error:
        raise RemoteServiceError("read Talkies model inventory") from error
    if status_code < 200 or status_code >= 300:
        raise RemoteServiceError(f"Talkies model inventory returned HTTP {status_code}")
    if len(raw_response) > MAX_PROVIDER_RESPONSE_BYTES:
        raise ProtocolError("Talkies model inventory response exceeds size limit")
    try:
        payload = require_json_object(decode_json(raw_response))
    except json.JSONDecodeError as error:
        raise ProtocolError("Talkies model inventory returned invalid JSON") from error
    except ValueError as error:
        raise ProtocolError("Talkies model inventory must be a JSON object") from error
    return payload


def parse_model_inventory(payload: Mapping[str, object]) -> frozenset[str]:
    """Validate the minimal OpenAI-compatible model inventory contract."""
    return frozenset(_model_inventory_entries(payload))


def parse_model_max_concurrency(
    payload: Mapping[str, object],
    model_id: str,
) -> int:
    """Return one model's positive integer concurrency advertisement."""
    models = _model_inventory_entries(payload)
    model = models.get(model_id)
    if model is None:
        raise RemoteServiceError("configured Talkies model is not available")
    max_concurrency = model.get("max_concurrency")
    if (
        isinstance(max_concurrency, bool)
        or not isinstance(max_concurrency, int)
        or max_concurrency < 1
    ):
        raise ProtocolError(
            "Talkies model inventory max_concurrency must be a positive integer"
        )
    return max_concurrency


def _model_inventory_entries(
    payload: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    try:
        models = require_json_array(payload.get("data"))
    except ValueError as error:
        raise ProtocolError("Talkies model inventory data must be an array") from error
    if not models:
        raise ProtocolError("Talkies model inventory must not be empty")

    model_by_id: dict[str, Mapping[str, object]] = {}
    for item in models:
        try:
            model = require_json_object(item)
        except ValueError as error:
            raise ProtocolError(
                "Talkies model inventory entry must be an object"
            ) from error
        model_id = _require_text(model, "id")
        if not model_id.strip():
            raise ProtocolError("Talkies model inventory ID must not be empty")
        if model_id in model_by_id:
            raise ProtocolError(
                "Talkies model inventory must not contain duplicate IDs"
            )
        model_by_id[model_id] = model
    return model_by_id


def _post_file_transcription(
    url: str,
    model: str,
    token: str | None,
    wav_bytes: bytes,
    response_format: BatchResponseFormat,
) -> BatchTranscription:
    boundary = f"----2xbrainz{uuid4().hex}"
    body = _multipart_body(
        boundary=boundary,
        model=model,
        wav_bytes=wav_bytes,
        response_format=response_format,
    )
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }
    authorization_headers = _authorization_headers(token)
    if authorization_headers is not None:
        headers.update(authorization_headers)
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(
            request,
            timeout=DEFAULT_HTTP_TIMEOUT.total_seconds(),
        ) as response:
            raw_response = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
            status_code = response.status
    except HTTPError as error:
        raise RemoteServiceError(
            f"Talkies file transcription returned HTTP {error.code}"
        ) from error
    except URLError as error:
        raise RemoteServiceError("connect to Talkies file transcription") from error
    except OSError as error:
        raise RemoteServiceError("read Talkies file transcription") from error
    if status_code < 200 or status_code >= 300:
        raise RemoteServiceError(
            f"Talkies file transcription returned HTTP {status_code}"
        )
    if len(raw_response) > MAX_PROVIDER_RESPONSE_BYTES:
        raise ProtocolError("Talkies file transcription response exceeds size limit")
    try:
        payload = require_json_object(decode_json(raw_response))
    except json.JSONDecodeError as error:
        raise ProtocolError(
            "Talkies file transcription returned invalid JSON"
        ) from error
    except ValueError as error:
        raise ProtocolError(
            "Talkies file transcription must be a JSON object"
        ) from error
    return parse_batch_transcription(payload, response_format)


def _multipart_body(
    *,
    boundary: str,
    model: str,
    wav_bytes: bytes,
    response_format: BatchResponseFormat,
) -> bytes:
    boundary_bytes = boundary.encode("ascii")
    fields = (
        (_MULTIPART_MODEL_FIELD, model.encode("utf-8")),
        (_MULTIPART_RESPONSE_FORMAT_FIELD, response_format.value.encode("ascii")),
    )
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.extend(
            (
                b"--" + boundary_bytes + b"\r\n",
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    "ascii"
                ),
                value,
                b"\r\n",
            )
        )
    chunks.extend(
        (
            b"--" + boundary_bytes + b"\r\n",
            (
                "Content-Disposition: form-data; "
                f'name="{_MULTIPART_FILE_FIELD}"; filename="{_MULTIPART_FILE_NAME}"\r\n'
            ).encode("ascii"),
            b"Content-Type: audio/wav\r\n\r\n",
            wav_bytes,
            b"\r\n",
            b"--" + boundary_bytes + b"--\r\n",
        )
    )
    return b"".join(chunks)


def parse_batch_transcription(
    payload: Mapping[str, object],
    response_format: BatchResponseFormat,
) -> BatchTranscription:
    """Validate one exact OpenAI-compatible file-transcription response."""
    text = _require_text(payload, "text")
    if not text.strip():
        raise ProtocolError("Talkies file transcription text must not be empty")
    if response_format is BatchResponseFormat.JSON:
        if set(payload) != {"text"}:
            raise ProtocolError("Talkies json transcription must contain only text")
        return BatchTranscription(
            text=text,
            duration_seconds=None,
            segment_count=None,
            word_count=None,
        )
    if set(payload) != {"duration", "language", "segments", "task", "text", "words"}:
        raise ProtocolError("Talkies verbose transcription has an unexpected shape")
    if payload.get("task") != "transcribe":
        raise ProtocolError("Talkies verbose transcription task must be transcribe")
    _require_text(payload, "language")
    duration_seconds = _require_nonnegative_number(payload, "duration")
    segments = _require_json_array_field(payload, "segments")
    words = _require_json_array_field(payload, "words")
    _validate_verbose_segments(segments)
    _validate_verbose_words(words)
    return BatchTranscription(
        text=text,
        duration_seconds=duration_seconds,
        segment_count=len(segments),
        word_count=len(words),
    )


def _validate_ready(message: str | bytes, expected_model: str) -> None:
    if isinstance(message, bytes):
        raise ProtocolError("Talkies ready event must be JSON")
    try:
        decoded = require_json_object(decode_json(message))
    except json.JSONDecodeError as error:
        raise ProtocolError("Talkies ready event is invalid JSON") from error
    except ValueError as error:
        raise ProtocolError("Talkies ready event must be a JSON object") from error
    if decoded.get("type") != _READY_EVENT:
        raise ProtocolError("Talkies did not acknowledge stream start")
    model = decoded.get("model")
    if model != expected_model:
        raise ProtocolError("Talkies selected a model other than the configured model")


def _require_text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise ProtocolError(f"Talkies event field {field} must be text")
    return value


def _require_nonnegative_int(payload: Mapping[str, object], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProtocolError(
            f"Talkies event field {field} must be a non-negative integer"
        )
    return value


def _require_nonnegative_number(payload: Mapping[str, object], field: str) -> float:
    value = payload.get(field)
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ProtocolError(f"Talkies event field {field} must be non-negative")
    return float(value)


def _require_bool(payload: Mapping[str, object], field: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise ProtocolError(f"Talkies event field {field} must be a boolean")
    return value


def _require_json_array_field(
    payload: Mapping[str, object],
    field: str,
) -> list[object]:
    try:
        return require_json_array(payload.get(field))
    except ValueError as error:
        raise ProtocolError(f"Talkies field {field} must be an array") from error


def _validate_verbose_segments(segments: list[object]) -> None:
    for segment in segments:
        try:
            payload = require_json_object(segment)
        except ValueError as error:
            raise ProtocolError("Talkies verbose segment must be an object") from error
        start = _require_nonnegative_number(payload, "start")
        end = _require_nonnegative_number(payload, "end")
        if end < start:
            raise ProtocolError("Talkies verbose segment end precedes its start")
        _require_text(payload, "text")


def _validate_verbose_words(words: list[object]) -> None:
    for word in words:
        try:
            payload = require_json_object(word)
        except ValueError as error:
            raise ProtocolError("Talkies verbose word must be an object") from error
        start = _require_nonnegative_number(payload, "start")
        end = _require_nonnegative_number(payload, "end")
        if end < start:
            raise ProtocolError("Talkies verbose word end precedes its start")
        _require_text(payload, "word")


def _parse_words(value: object) -> tuple[WordTiming, ...]:
    try:
        word_values = require_json_array(value)
    except ValueError as error:
        raise ProtocolError("Talkies event field words must be an array") from error
    words: list[WordTiming] = []
    for item in word_values:
        try:
            word_data = require_json_object(item)
        except ValueError as error:
            raise ProtocolError("Talkies word timing must be an object") from error
        word = _require_text(word_data, "word")
        start_ms = _optional_seconds_to_ms(word_data.get("start"))
        end_ms = _optional_seconds_to_ms(word_data.get("end"))
        if start_ms is not None and end_ms is not None and end_ms < start_ms:
            raise ProtocolError("Talkies word timing end precedes its start")
        words.append(
            WordTiming(
                word=word,
                start_ms=start_ms,
                end_ms=end_ms,
                confidence=_optional_confidence(word_data.get("confidence")),
            )
        )
    return tuple(words)


def _has_complete_word_timing(words: tuple[WordTiming, ...]) -> bool:
    return bool(words) and all(
        word.start_ms is not None and word.end_ms is not None for word in words
    )


def _optional_seconds_to_ms(value: object) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ProtocolError("Talkies word timing must be a non-negative number")
    return round(float(value) * 1_000)


def _optional_confidence(value: object) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise ProtocolError("Talkies confidence must be a finite number from 0 to 1")
    return float(value)


def _optional_language(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError("Talkies language must be non-empty text")
    return value


def _timing_bounds(words: tuple[WordTiming, ...]) -> tuple[int | None, int | None]:
    starts = [word.start_ms for word in words if word.start_ms is not None]
    ends = [word.end_ms for word in words if word.end_ms is not None]
    return (min(starts) if starts else None, max(ends) if ends else None)
