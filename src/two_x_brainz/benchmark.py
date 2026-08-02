"""Finite, reproducible Talkies transport and contract evaluation."""

from __future__ import annotations

import asyncio
import contextvars
import time
import unicodedata
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from two_x_brainz.aigate import DraftProvider
from two_x_brainz.audio import WavFixture, load_reference_text, load_wav_fixture
from two_x_brainz.config import Settings
from two_x_brainz.constants import (
    DEFAULT_CHANNELS,
    DEFAULT_FRAME_BYTES,
    DEFAULT_FRAME_DURATION_MS,
    DEFAULT_PROVIDER_GENERATION_DEADLINE,
    DEFAULT_SAMPLE_RATE_HZ,
    MAX_ASR_EVALUATION_WORDS,
)
from two_x_brainz.contracts import (
    ASRStreamStats,
    AudioFrame,
    DraftRequest,
    DraftResult,
    GenerationStatus,
    SpeakerRole,
    TranscriptEvent,
    TranscriptLine,
    TranscriptSnapshot,
)
from two_x_brainz.errors import ProtocolError
from two_x_brainz.talkies import (
    BatchResponseFormat,
    BatchTranscription,
    TalkiesClient,
    TalkiesStreamConfig,
)

_BENCHMARK_SESSION_ID = "asr-benchmark"
_BENCHMARK_USER_STREAM_ID = "fixture-user-stream"
_BENCHMARK_REMOTE_STREAM_ID = "fixture-remote-stream"
_BENCHMARK_DRAFT_GENERATION_ID = "asr-benchmark-draft"
_BENCHMARK_DRAFT_TURN_ID = "asr-benchmark-turn"
_BENCHMARK_DRAFT_TEXT = "Please provide a concise benchmark reply."
_BENCHMARK_DRAFT_REVISION = 1
_BENCHMARK_STREAMS = (
    (_BENCHMARK_USER_STREAM_ID, SpeakerRole.USER),
    (_BENCHMARK_REMOTE_STREAM_ID, SpeakerRole.REMOTE),
)


@dataclass(frozen=True, slots=True)
class NativeStreamBenchmark:
    """Aggregate contract result from one concurrent native ASR stream."""

    stream_id: str
    speaker_role: SpeakerRole
    event_types: tuple[str, ...]
    frames: int
    audio_seconds: float
    word_error_rate: float | None


@dataclass(frozen=True, slots=True)
class ASRBenchmarkReport:
    """Comparable aggregate results that never include fixture or transcript text."""

    model: str
    source_audio_seconds: float
    native_elapsed_seconds: float
    native_streams: tuple[NativeStreamBenchmark, ...]
    draft_elapsed_seconds: float | None
    batch_json_elapsed_seconds: float
    batch_json_word_error_rate: float | None
    batch_verbose_json_elapsed_seconds: float
    batch_verbose_json_word_error_rate: float | None
    verbose_segment_count: int
    verbose_word_count: int


async def run_asr_benchmark(
    settings: Settings,
    audio_path: Path,
    draft_provider: DraftProvider | None = None,
    reference_path: Path | None = None,
) -> ASRBenchmarkReport:
    """Exercise native and file routes with one source WAV and fixed contracts."""
    fixture = load_wav_fixture(audio_path)
    reference_text = (
        load_reference_text(reference_path) if reference_path is not None else None
    )
    client = TalkiesClient(
        TalkiesStreamConfig(
            url=settings.talkies_ws_url,
            model=settings.talkies_model,
            token=settings.talkies_token,
        )
    )
    await client.verify_configured_model()
    await client.warm_configured_model()
    (
        native_streams,
        native_elapsed_seconds,
        draft_elapsed_seconds,
    ) = await _run_concurrent_native_and_draft(
        client,
        fixture,
        draft_provider,
        reference_text,
    )
    batch_json_result, batch_json_elapsed_seconds = await _run_batch_request(
        client,
        fixture,
        BatchResponseFormat.JSON,
    )
    verbose_result, batch_verbose_json_elapsed_seconds = await _run_verbose_request(
        client,
        fixture,
    )
    if verbose_result.segment_count is None or verbose_result.word_count is None:
        raise ProtocolError("Talkies verbose response did not expose timing metadata")
    return ASRBenchmarkReport(
        model=settings.talkies_model,
        source_audio_seconds=fixture.duration_seconds,
        native_elapsed_seconds=native_elapsed_seconds,
        native_streams=native_streams,
        draft_elapsed_seconds=draft_elapsed_seconds,
        batch_json_elapsed_seconds=batch_json_elapsed_seconds,
        batch_json_word_error_rate=_optional_word_error_rate(
            reference_text,
            batch_json_result.text,
        ),
        batch_verbose_json_elapsed_seconds=batch_verbose_json_elapsed_seconds,
        batch_verbose_json_word_error_rate=_optional_word_error_rate(
            reference_text,
            verbose_result.text,
        ),
        verbose_segment_count=verbose_result.segment_count,
        verbose_word_count=verbose_result.word_count,
    )


async def _run_concurrent_native_and_draft(
    client: TalkiesClient,
    fixture: WavFixture,
    draft_provider: DraftProvider | None,
    reference_text: str | None,
) -> tuple[tuple[NativeStreamBenchmark, ...], float, float | None]:
    if draft_provider is None:
        native_streams, native_elapsed_seconds = await _run_native_streams(
            client,
            fixture,
            reference_text,
        )
        return native_streams, native_elapsed_seconds, None

    async with asyncio.TaskGroup() as task_group:
        native_task = task_group.create_task(
            _run_native_streams(client, fixture, reference_text),
            context=contextvars.copy_context(),
        )
        draft_task = task_group.create_task(
            _run_draft_probe(draft_provider),
            context=contextvars.copy_context(),
        )
    native_streams, native_elapsed_seconds = native_task.result()
    return native_streams, native_elapsed_seconds, draft_task.result()


async def _run_draft_probe(draft_provider: DraftProvider) -> float:
    request = DraftRequest(
        generation_id=_BENCHMARK_DRAFT_GENERATION_ID,
        trigger_turn_id=_BENCHMARK_DRAFT_TURN_ID,
        context_revision=_BENCHMARK_DRAFT_REVISION,
        transcript=TranscriptSnapshot(
            revision=_BENCHMARK_DRAFT_REVISION,
            lines=(
                TranscriptLine(
                    stream_id=_BENCHMARK_REMOTE_STREAM_ID,
                    speaker_role=SpeakerRole.REMOTE,
                    revision=_BENCHMARK_DRAFT_REVISION,
                    text=_BENCHMARK_DRAFT_TEXT,
                    is_final=True,
                ),
            ),
        ),
        deadline_seconds=DEFAULT_PROVIDER_GENERATION_DEADLINE.total_seconds(),
    )
    started_at = time.monotonic()
    result = await draft_provider.draft(request)
    elapsed_seconds = time.monotonic() - started_at
    validate_benchmark_draft_result(request, result)
    return elapsed_seconds


def validate_benchmark_draft_result(
    request: DraftRequest,
    result: DraftResult,
) -> None:
    """Reject a provider result that cannot prove this fixed probe completed."""
    if result.generation_id != request.generation_id:
        raise ProtocolError("AIGate benchmark draft changed the generation identifier")
    if result.context_revision != request.context_revision:
        raise ProtocolError("AIGate benchmark draft changed the context revision")
    if result.status is not GenerationStatus.COMPLETED or not result.text:
        raise ProtocolError("AIGate benchmark draft did not complete with visible text")


async def _run_native_streams(
    client: TalkiesClient,
    fixture: WavFixture,
    reference_text: str | None,
) -> tuple[tuple[NativeStreamBenchmark, ...], float]:
    started_at = time.monotonic()
    streams = await asyncio.gather(
        *(
            _run_native_stream(
                client,
                fixture,
                stream_id,
                speaker_role,
                reference_text,
            )
            for stream_id, speaker_role in _BENCHMARK_STREAMS
        )
    )
    return tuple(streams), time.monotonic() - started_at


async def _run_native_stream(
    client: TalkiesClient,
    fixture: WavFixture,
    stream_id: str,
    speaker_role: SpeakerRole,
    reference_text: str | None,
) -> NativeStreamBenchmark:
    events = tuple(
        [
            event
            async for event in client.transcribe(
                session_id=_BENCHMARK_SESSION_ID,
                stream_id=stream_id,
                speaker_role=speaker_role,
                frames=_fixture_frames(fixture, stream_id, speaker_role),
            )
        ]
    )
    terminal_stats = _require_terminal_stats(events)
    event_types = tuple(
        event.source_event_type.value
        for event in events
        if isinstance(event, TranscriptEvent)
    )
    final_events = tuple(
        event
        for event in events
        if isinstance(event, TranscriptEvent)
        and event.source_event_type.value == "final"
    )
    if not final_events:
        raise ProtocolError("Talkies native stream did not emit a final transcript")
    if terminal_stats.canceled:
        raise ProtocolError("Talkies native stream unexpectedly reported cancellation")
    expected_frame_count = _frame_count(fixture.pcm16le)
    if terminal_stats.frames != expected_frame_count:
        raise ProtocolError("Talkies native stats frame count did not match sent PCM")
    return NativeStreamBenchmark(
        stream_id=stream_id,
        speaker_role=speaker_role,
        event_types=event_types,
        frames=terminal_stats.frames,
        audio_seconds=terminal_stats.audio_seconds,
        word_error_rate=_optional_word_error_rate(
            reference_text, final_events[-1].text
        ),
    )


async def _run_batch_request(
    client: TalkiesClient,
    fixture: WavFixture,
    response_format: BatchResponseFormat,
) -> tuple[BatchTranscription, float]:
    started_at = time.monotonic()
    result = await client.transcribe_file(fixture, response_format)
    return result, time.monotonic() - started_at


async def _run_verbose_request(
    client: TalkiesClient,
    fixture: WavFixture,
) -> tuple[BatchTranscription, float]:
    started_at = time.monotonic()
    result = await client.transcribe_file(fixture, BatchResponseFormat.VERBOSE_JSON)
    return result, time.monotonic() - started_at


def _require_terminal_stats(
    events: tuple[TranscriptEvent | ASRStreamStats, ...],
) -> ASRStreamStats:
    statistics = tuple(event for event in events if isinstance(event, ASRStreamStats))
    if len(statistics) != 1:
        raise ProtocolError("Talkies native stream must emit exactly one stats event")
    return statistics[0]


async def _fixture_frames(
    fixture: WavFixture,
    stream_id: str,
    speaker_role: SpeakerRole,
) -> AsyncIterator[AudioFrame]:
    frame_count = _frame_count(fixture.pcm16le)
    padded_pcm = fixture.pcm16le.ljust(frame_count * DEFAULT_FRAME_BYTES, b"\x00")
    for sequence in range(frame_count):
        offset = sequence * DEFAULT_FRAME_BYTES
        yield AudioFrame(
            session_id=_BENCHMARK_SESSION_ID,
            stream_id=stream_id,
            speaker_role=speaker_role,
            sequence=sequence,
            captured_at_monotonic=time.monotonic(),
            sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
            channels=DEFAULT_CHANNELS,
            samples=padded_pcm[offset : offset + DEFAULT_FRAME_BYTES],
        )
        await asyncio.sleep(DEFAULT_FRAME_DURATION_MS / 1_000)


def _frame_count(pcm16le: bytes) -> int:
    return (len(pcm16le) + DEFAULT_FRAME_BYTES - 1) // DEFAULT_FRAME_BYTES


def _optional_word_error_rate(
    reference_text: str | None,
    transcription_text: str,
) -> float | None:
    if reference_text is None:
        return None
    return word_error_rate(reference_text, transcription_text)


def word_error_rate(reference_text: str, transcription_text: str) -> float:
    """Return normalized word error rate without retaining transcript text."""
    reference_words = _evaluation_words(reference_text)
    transcription_words = _evaluation_words(transcription_text)
    if not reference_words:
        raise ProtocolError("benchmark reference did not contain evaluation words")
    if len(reference_words) > MAX_ASR_EVALUATION_WORDS:
        raise ProtocolError("benchmark reference exceeds the evaluation word limit")
    if len(transcription_words) > MAX_ASR_EVALUATION_WORDS:
        raise ProtocolError("Talkies transcript exceeds the evaluation word limit")

    prior_row = list(range(len(transcription_words) + 1))
    for reference_index, reference_word in enumerate(reference_words, start=1):
        current_row = [reference_index]
        for transcription_index, transcription_word in enumerate(
            transcription_words,
            start=1,
        ):
            substitution_cost = 0 if reference_word == transcription_word else 1
            current_row.append(
                min(
                    prior_row[transcription_index] + 1,
                    current_row[transcription_index - 1] + 1,
                    prior_row[transcription_index - 1] + substitution_cost,
                )
            )
        prior_row = current_row
    return prior_row[-1] / len(reference_words)


def _evaluation_words(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return tuple(
        "".join(
            character if character.isalnum() else " " for character in normalized
        ).split()
    )
