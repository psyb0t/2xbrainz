"""Linux PipeWire PCM capture run inside the application container."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from dataclasses import dataclass
from typing import cast

from two_x_brainz.constants import (
    DEFAULT_CHANNELS,
    DEFAULT_FRAME_BYTES,
    DEFAULT_SAMPLE_RATE_HZ,
    MAX_CAPTURE_FRAME_GAP_SECONDS,
    MAX_INTER_STREAM_DRIFT_PENDING_FRAMES,
    PCM_S16LE_BYTEORDER,
    PCM_S16LE_BYTES_PER_SECOND,
    PCM_S16LE_SAMPLE_BYTES,
    PIPEWIRE_FORMAT_S16,
    PIPEWIRE_LATENCY,
    PIPEWIRE_OPTION_CHANNELS,
    PIPEWIRE_OPTION_FORMAT,
    PIPEWIRE_OPTION_LATENCY,
    PIPEWIRE_OPTION_PROPERTIES,
    PIPEWIRE_OPTION_RATE,
    PIPEWIRE_OPTION_TARGET,
    PIPEWIRE_OUTPUT_TARGET,
    PIPEWIRE_RECORD_COMMAND,
    PIPEWIRE_SINK_CAPTURE_PROPERTIES,
    VAD_MAX_SEGMENT_AUDIO_BYTES,
    VAD_PRE_ROLL_FRAME_COUNT,
    VAD_SILENCE_WINDOW_COUNT,
    VAD_SPEECH_START_PROBABILITY,
    VAD_SPEECH_START_WINDOW_COUNT,
    VAD_SPEECH_STOP_PROBABILITY,
)
from two_x_brainz.contracts import AudioFrame, SpeakerRole
from two_x_brainz.errors import CaptureError
from two_x_brainz.vad import StreamingVoiceActivityDetector

logger = logging.getLogger(__name__)

_NODE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_PIPEWIRE_DUMP_COMMAND = "pw-dump"
_PIPEWIRE_DUMP_OPTION_NO_COLORS = "--no-colors"
_MAX_DEVICE_OUTPUT_BYTES = 1_000_000
_MAX_DEVICE_ERROR_BYTES = 65_536
_PIPEWIRE_DISCOVERY_READ_BYTES = 65_536
_PIPEWIRE_DISCOVERY_TIMEOUT_SECONDS = 10
_PIPEWIRE_TERMINATION_TIMEOUT_SECONDS = 1
_EMPTY_CAPTURE_ERROR = "PipeWire capture ended without PCM frames"
_PIPEWIRE_NODE_PATTERN = re.compile(
    r'^  \{\n    "id": (?P<id>\d+),\n'
    r'    "type": "PipeWire:Interface:Node",(?P<body>.*?)(?=^  \{|^\])',
    re.MULTILINE | re.DOTALL,
)
_PIPEWIRE_NODE_PROPERTY_PATTERN = re.compile(
    r'"(?P<name>node\.name|media\.class|node\.description|node\.nick|'
    r'device\.description|device\.product\.name)":\s*'
    r'(?P<value>"(?:\\.|[^"\\])*")'
)
_PIPEWIRE_METADATA_INTERFACE = "PipeWire:Interface:Metadata"
_PIPEWIRE_DEFAULT_METADATA_NAME = "default"
_PIPEWIRE_METADATA_NAME_PROPERTY = "metadata.name"
_PIPEWIRE_METADATA_ITEMS_FIELD = "metadata"
_PIPEWIRE_DEFAULT_AUDIO_SOURCE_KEY = "default.audio.source"
_PIPEWIRE_DEFAULT_AUDIO_SINK_KEY = "default.audio.sink"
_PIPEWIRE_DEFAULT_VALUE_NAME_KEY = "name"
_PIPEWIRE_DEFAULT_VALUE_ID_KEY = "id"
_PIPEWIRE_NODE_DESCRIPTION_PROPERTIES = (
    "node.description",
    "node.nick",
    "device.description",
    "device.product.name",
)
_DEFAULT_SOURCE_ROLE = "source"
_DEFAULT_SINK_ROLE = "sink"
_PCM_S16LE_MAX_ABSOLUTE_SAMPLE = 32_768
_AUDIO_LEVEL_PERCENT_MAXIMUM = 100


@dataclass(frozen=True, slots=True)
class PipeWireSource:
    """A single PipeWire node captured as bounded PCM16LE frames."""

    node_id: str
    capture_sink: bool = False

    def __post_init__(self) -> None:
        validate_pipewire_node_identifier(self.node_id)

    async def frames(self) -> AsyncIterator[bytes]:
        """Yield 20 ms PCM frames and terminate the child process on exit."""
        command = [
            PIPEWIRE_RECORD_COMMAND,
            PIPEWIRE_OPTION_TARGET,
            self.node_id,
        ]
        if self.capture_sink:
            command.extend(
                [
                    PIPEWIRE_OPTION_PROPERTIES,
                    PIPEWIRE_SINK_CAPTURE_PROPERTIES,
                ]
            )
        command.extend(
            [
                PIPEWIRE_OPTION_RATE,
                str(DEFAULT_SAMPLE_RATE_HZ),
                PIPEWIRE_OPTION_CHANNELS,
                str(DEFAULT_CHANNELS),
                PIPEWIRE_OPTION_FORMAT,
                PIPEWIRE_FORMAT_S16,
                PIPEWIRE_OPTION_LATENCY,
                PIPEWIRE_LATENCY,
                PIPEWIRE_OUTPUT_TARGET,
            ]
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise CaptureError("start PipeWire capture") from error

        if process.stdout is None or process.stderr is None:
            raise CaptureError("PipeWire capture did not expose standard streams")

        logger.info("PipeWire capture started")
        try:
            frame_count = 0
            chunks = _read_pipewire_chunks(process.stdout)
            async for frame in normalize_pcm_frames(chunks):
                frame_count += 1
                yield frame
            exit_code = await process.wait()
            if exit_code != 0:
                stderr = (await process.stderr.read()).decode("utf-8", errors="replace")
                raise CaptureError(
                    f"PipeWire capture exited with code {exit_code}: {stderr}"
                )
            if frame_count == 0:
                raise CaptureError(_EMPTY_CAPTURE_ERROR)
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=1)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            logger.info("PipeWire capture stopped")


@dataclass(frozen=True, slots=True)
class CaptureStats:
    """Privacy-safe aggregate timing for one capture stream."""

    speaker_role: SpeakerRole
    frame_count: int
    audio_seconds: float
    gap_count: int
    max_gap_seconds: float


@dataclass(frozen=True, slots=True)
class InterStreamDriftStats:
    """Privacy-safe aggregate relative timing diagnostics for both streams."""

    comparison_count: int
    max_abs_drift_seconds: float
    unmatched_frame_count: int


class InterStreamDriftMonitor:
    """Measure relative capture-clock drift without retaining PCM or identities."""

    def __init__(self) -> None:
        self._pending: dict[SpeakerRole, dict[int, float]] = {
            SpeakerRole.USER: {},
            SpeakerRole.REMOTE: {},
        }
        self._baseline_offset_seconds: float | None = None
        self._comparison_count = 0
        self._max_abs_drift_seconds = 0.0

    def observe(self, frame: AudioFrame) -> None:
        """Compare matching stream-local frame sequences using monotonic time."""
        pending = self._pending[frame.speaker_role]
        pending[frame.sequence] = frame.captured_at_monotonic
        other_role = _other_speaker_role(frame.speaker_role)
        other_pending = self._pending[other_role]
        other_timestamp = other_pending.pop(frame.sequence, None)
        if other_timestamp is not None:
            pending.pop(frame.sequence)
            user_timestamp, remote_timestamp = _ordered_role_timestamps(
                frame.speaker_role,
                frame.captured_at_monotonic,
                other_timestamp,
            )
            self._record_comparison(user_timestamp - remote_timestamp)
        _trim_pending_frames(pending)

    def stats(self) -> InterStreamDriftStats:
        """Return aggregate timing only; never expose stream IDs or PCM."""
        return InterStreamDriftStats(
            comparison_count=self._comparison_count,
            max_abs_drift_seconds=self._max_abs_drift_seconds,
            unmatched_frame_count=sum(
                len(pending) for pending in self._pending.values()
            ),
        )

    def _record_comparison(self, offset_seconds: float) -> None:
        if self._baseline_offset_seconds is None:
            self._baseline_offset_seconds = offset_seconds
        else:
            drift_seconds = abs(offset_seconds - self._baseline_offset_seconds)
            self._max_abs_drift_seconds = max(
                self._max_abs_drift_seconds,
                drift_seconds,
            )
        self._comparison_count += 1


class CaptureFrameMonitor:
    """Owns per-stream frame identity and bounded aggregate capture telemetry."""

    def __init__(
        self,
        *,
        session_id: str,
        stream_id: str,
        speaker_role: SpeakerRole,
        drift_monitor: InterStreamDriftMonitor | None = None,
    ) -> None:
        self._session_id = session_id
        self._stream_id = stream_id
        self._speaker_role = speaker_role
        self._drift_monitor = drift_monitor
        self._sequence = 0
        self._last_capture_at_monotonic: float | None = None
        self._frame_count = 0
        self._audio_seconds = 0.0
        self._gap_count = 0
        self._max_gap_seconds = 0.0

    async def annotate(
        self,
        frames: AsyncIterable[bytes],
    ) -> AsyncIterator[AudioFrame]:
        """Attach runtime metadata without retaining or logging PCM samples."""
        async for samples in frames:
            yield self.observe(samples, time.monotonic())

    def observe(self, samples: bytes, captured_at_monotonic: float) -> AudioFrame:
        """Record one capture callback and return its immutable typed frame."""
        if len(samples) % PCM_S16LE_SAMPLE_BYTES:
            raise CaptureError("capture frame has an odd PCM16LE byte length")
        if not math.isfinite(captured_at_monotonic):
            raise CaptureError("capture timestamp must be finite")
        if (
            self._last_capture_at_monotonic is not None
            and captured_at_monotonic < self._last_capture_at_monotonic
        ):
            raise CaptureError("capture timestamps must be monotonic")

        gap_seconds = self._capture_gap_seconds(captured_at_monotonic)
        if gap_seconds > MAX_CAPTURE_FRAME_GAP_SECONDS:
            self._gap_count += 1
            self._max_gap_seconds = max(self._max_gap_seconds, gap_seconds)

        frame = AudioFrame(
            session_id=self._session_id,
            stream_id=self._stream_id,
            speaker_role=self._speaker_role,
            sequence=self._sequence,
            captured_at_monotonic=captured_at_monotonic,
            sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
            channels=DEFAULT_CHANNELS,
            samples=samples,
        )
        self._sequence += 1
        self._last_capture_at_monotonic = captured_at_monotonic
        self._frame_count += 1
        self._audio_seconds += len(samples) / PCM_S16LE_BYTES_PER_SECOND
        if self._drift_monitor is not None:
            self._drift_monitor.observe(frame)
        return frame

    def stats(self) -> CaptureStats:
        """Return aggregates only; frame payloads and identities are not retained."""
        return CaptureStats(
            speaker_role=self._speaker_role,
            frame_count=self._frame_count,
            audio_seconds=self._audio_seconds,
            gap_count=self._gap_count,
            max_gap_seconds=self._max_gap_seconds,
        )

    def _capture_gap_seconds(self, captured_at_monotonic: float) -> float:
        if self._last_capture_at_monotonic is None:
            return 0.0
        return captured_at_monotonic - self._last_capture_at_monotonic


class SilenceTurnSegmenter:
    """Rotate ASR transports using neural speech probability and hysteresis."""

    def __init__(
        self,
        frames: AsyncIterable[AudioFrame],
        detector: StreamingVoiceActivityDetector | None = None,
    ) -> None:
        self._frames = aiter(frames)
        self._detector = detector or StreamingVoiceActivityDetector()
        self._capture_ended = False
        self._last_boundary: str | None = None
        self._speech_start_windows = 0
        self._silence_windows = 0
        self._pre_roll: deque[AudioFrame] = deque(maxlen=VAD_PRE_ROLL_FRAME_COUNT)
        self._pending_start_frames: deque[AudioFrame] = deque()

    @property
    def capture_ended(self) -> bool:
        """Whether the underlying capture source has ended."""
        return self._capture_ended

    @property
    def last_boundary(self) -> str | None:
        """Return why the latest ASR segment ended."""
        return self._last_boundary

    async def next_speech_frame(self) -> AudioFrame | None:
        """Wait for sustained neural speech evidence and retain bounded pre-roll."""
        if self._capture_ended:
            return None
        async for frame in self._frames:
            self._pre_roll.append(frame)
            probabilities = self._detector.observe(frame.samples)
            if not self._speech_started(probabilities):
                continue
            self._pending_start_frames.extend(self._pre_roll)
            self._pre_roll.clear()
            self._speech_start_windows = 0
            self._silence_windows = 0
            return self._pending_start_frames.popleft()
        self._capture_ended = True
        self._last_boundary = "capture_ended"
        return None

    async def next_segment(
        self,
        first_speech_frame: AudioFrame | None = None,
    ) -> AsyncIterator[AudioFrame]:
        """Yield one bounded utterance ending after silence or a safety limit."""
        if self._capture_ended:
            return
        self._last_boundary = None
        if first_speech_frame is None:
            first_speech_frame = await self.next_speech_frame()
        if first_speech_frame is None:
            return
        segment_audio_bytes = len(first_speech_frame.samples)
        yield first_speech_frame
        while self._pending_start_frames:
            pending_frame = self._pending_start_frames.popleft()
            segment_audio_bytes += len(pending_frame.samples)
            yield pending_frame
        async for frame in self._frames:
            yield frame
            segment_audio_bytes += len(frame.samples)
            probabilities = self._detector.observe(frame.samples)
            self._update_silence(probabilities)
            if self._silence_windows >= VAD_SILENCE_WINDOW_COUNT:
                self._last_boundary = "silence"
                self._silence_windows = 0
                return
            if segment_audio_bytes >= VAD_MAX_SEGMENT_AUDIO_BYTES:
                self._last_boundary = "max_duration"
                self._silence_windows = 0
                return
        self._capture_ended = True
        self._last_boundary = "capture_ended"

    def _speech_started(self, probabilities: tuple[float, ...]) -> bool:
        for probability in probabilities:
            if probability < VAD_SPEECH_START_PROBABILITY:
                self._speech_start_windows = 0
                continue
            self._speech_start_windows += 1
            if self._speech_start_windows >= VAD_SPEECH_START_WINDOW_COUNT:
                return True
        return False

    def _update_silence(self, probabilities: tuple[float, ...]) -> None:
        for probability in probabilities:
            if probability >= VAD_SPEECH_STOP_PROBABILITY:
                self._silence_windows = 0
                continue
            self._silence_windows += 1


def _other_speaker_role(speaker_role: SpeakerRole) -> SpeakerRole:
    if speaker_role is SpeakerRole.USER:
        return SpeakerRole.REMOTE
    return SpeakerRole.USER


def _ordered_role_timestamps(
    speaker_role: SpeakerRole,
    timestamp: float,
    other_timestamp: float,
) -> tuple[float, float]:
    if speaker_role is SpeakerRole.USER:
        return timestamp, other_timestamp
    return other_timestamp, timestamp


def _trim_pending_frames(pending: dict[int, float]) -> None:
    while len(pending) > MAX_INTER_STREAM_DRIFT_PENDING_FRAMES:
        pending.pop(next(iter(pending)))


async def normalize_pcm_frames(chunks: AsyncIterable[bytes]) -> AsyncIterator[bytes]:
    """Assemble arbitrary PCM16LE reads into exact configured stream frames."""
    remainder = bytearray()
    async for chunk in chunks:
        if len(chunk) % PCM_S16LE_SAMPLE_BYTES:
            raise CaptureError("PipeWire emitted an odd-length PCM chunk")
        remainder.extend(chunk)
        while len(remainder) >= DEFAULT_FRAME_BYTES:
            frame = bytes(remainder[:DEFAULT_FRAME_BYTES])
            del remainder[:DEFAULT_FRAME_BYTES]
            yield frame
    if remainder:
        raise CaptureError("PipeWire capture ended with an incomplete PCM frame")


async def _read_pipewire_chunks(reader: asyncio.StreamReader) -> AsyncIterator[bytes]:
    """Bound subprocess reads before PCM frame normalization."""
    while chunk := await reader.read(DEFAULT_FRAME_BYTES):
        yield chunk


async def list_pipewire_nodes() -> list[dict[str, str]]:
    """Return a safe subset of PipeWire node metadata for device selection."""
    try:
        process = await asyncio.create_subprocess_exec(
            _PIPEWIRE_DUMP_COMMAND,
            _PIPEWIRE_DUMP_OPTION_NO_COLORS,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        raise CaptureError("start pw-dump") from error
    if process.stdout is None or process.stderr is None:
        raise CaptureError("pw-dump did not expose standard streams")

    output_task = asyncio.create_task(
        _read_bounded_pipewire_stream(process.stdout, _MAX_DEVICE_OUTPUT_BYTES)
    )
    error_task = asyncio.create_task(
        _read_bounded_pipewire_stream(process.stderr, _MAX_DEVICE_ERROR_BYTES)
    )
    try:
        (
            exit_code,
            (raw, output_truncated),
            (stderr, error_truncated),
        ) = await asyncio.wait_for(
            asyncio.gather(process.wait(), output_task, error_task),
            timeout=_PIPEWIRE_DISCOVERY_TIMEOUT_SECONDS,
        )
    except TimeoutError as error:
        await _terminate_pipewire_process(process)
        raise CaptureError("pw-dump discovery timed out") from error
    except BaseException:
        await _terminate_pipewire_process(process)
        raise

    if output_truncated:
        raise CaptureError("pw-dump output exceeds the configured size limit")
    if exit_code != 0:
        if error_truncated:
            raise CaptureError(f"pw-dump exited with code {exit_code}")
        stderr_message = stderr.decode("utf-8", errors="replace")
        raise CaptureError(f"pw-dump exited with code {exit_code}: {stderr_message}")
    return _extract_pipewire_nodes(raw)


def _extract_pipewire_nodes(raw: bytes) -> list[dict[str, str]]:
    """Extract capture-safe Node metadata from PipeWire's JSON-like output."""
    text = raw.decode("utf-8", errors="replace")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return _extract_json_like_pipewire_nodes(text)
    if not isinstance(decoded, list):
        return []
    return _extract_json_pipewire_nodes(cast(list[object], decoded))


def _extract_json_pipewire_nodes(decoded: list[object]) -> list[dict[str, str]]:
    """Extract Node metadata from a strict JSON PipeWire document."""
    defaults = _extract_default_pipewire_nodes(decoded)
    nodes: list[dict[str, str]] = []
    for item in decoded:
        if not isinstance(item, dict):
            continue
        node = cast(dict[str, object], item)
        if node.get("type") != "PipeWire:Interface:Node":
            continue
        node_id = node.get("id")
        info = node.get("info")
        if not isinstance(node_id, int) or not isinstance(info, dict):
            continue
        info_object = cast(dict[str, object], info)
        properties = info_object.get("props")
        if not isinstance(properties, dict):
            continue
        property_map = cast(dict[str, object], properties)
        name = property_map.get("node.name")
        media_class = property_map.get("media.class")
        if not isinstance(name, str) or not isinstance(media_class, str):
            continue
        nodes.append(
            _pipewire_node_record(
                node_id=str(node_id),
                name=name,
                media_class=media_class,
                properties=property_map,
                defaults=defaults,
            )
        )
    return nodes


def _extract_json_like_pipewire_nodes(text: str) -> list[dict[str, str]]:
    """Extract Node metadata from PipeWire documents containing SPA pod fragments."""
    nodes: list[dict[str, str]] = []
    for match in _PIPEWIRE_NODE_PATTERN.finditer(text):
        properties = _extract_pipewire_node_properties(match.group("body"))
        name = properties.get("node.name")
        media_class = properties.get("media.class")
        if name is None or media_class is None:
            continue
        nodes.append(
            _pipewire_node_record(
                node_id=match.group("id"),
                name=name,
                media_class=media_class,
                properties=properties,
                defaults={},
            )
        )
    return nodes


def _extract_pipewire_node_properties(node_body: str) -> dict[str, str]:
    """Decode only quoted PipeWire node properties used for capture selection."""
    properties: dict[str, str] = {}
    for match in _PIPEWIRE_NODE_PROPERTY_PATTERN.finditer(node_body):
        try:
            value = json.loads(match.group("value"))
        except json.JSONDecodeError:
            continue
        if isinstance(value, str):
            properties[match.group("name")] = value
    return properties


def _extract_default_pipewire_nodes(decoded: list[object]) -> dict[str, str]:
    """Read current default identifiers without trusting metadata as targets."""
    defaults: dict[str, str] = {}
    for item in decoded:
        if not isinstance(item, dict):
            continue
        metadata = cast(dict[str, object], item)
        if metadata.get("type") != _PIPEWIRE_METADATA_INTERFACE:
            continue
        info = metadata.get("info")
        if not isinstance(info, dict):
            continue
        info_map = cast(dict[str, object], info)
        properties = info_map.get("props")
        if not isinstance(properties, dict):
            continue
        property_map = cast(dict[str, object], properties)
        if (
            property_map.get(_PIPEWIRE_METADATA_NAME_PROPERTY)
            != _PIPEWIRE_DEFAULT_METADATA_NAME
        ):
            continue
        entries = metadata.get(_PIPEWIRE_METADATA_ITEMS_FIELD)
        if not isinstance(entries, list):
            continue
        for raw_entry in cast(list[object], entries):
            if not isinstance(raw_entry, dict):
                continue
            entry = cast(dict[str, object], raw_entry)
            key = entry.get("key")
            value = _pipewire_default_value(entry.get("value"))
            if (
                key
                in {
                    _PIPEWIRE_DEFAULT_AUDIO_SOURCE_KEY,
                    _PIPEWIRE_DEFAULT_AUDIO_SINK_KEY,
                }
                and value is not None
            ):
                defaults[cast(str, key)] = value
    return defaults


def _pipewire_default_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(decoded, str):
        return decoded
    if not isinstance(decoded, dict):
        return None
    decoded_map = cast(dict[str, object], decoded)
    for key in (_PIPEWIRE_DEFAULT_VALUE_NAME_KEY, _PIPEWIRE_DEFAULT_VALUE_ID_KEY):
        identifier = decoded_map.get(key)
        if isinstance(identifier, str):
            return identifier
        if isinstance(identifier, int):
            return str(identifier)
    return None


def _pipewire_node_record(
    *,
    node_id: str,
    name: str,
    media_class: str,
    properties: Mapping[str, object],
    defaults: Mapping[str, str],
) -> dict[str, str]:
    record = {
        "id": node_id,
        "name": name,
        "media_class": media_class,
    }
    description = _pipewire_node_description(properties)
    if description:
        record["description"] = description
    default_role = _default_role_for_node(node_id, name, defaults)
    if default_role is not None:
        record["default_role"] = default_role
    return record


def _pipewire_node_description(properties: Mapping[str, object]) -> str | None:
    for key in _PIPEWIRE_NODE_DESCRIPTION_PROPERTIES:
        value = properties.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _default_role_for_node(
    node_id: str,
    name: str,
    defaults: Mapping[str, str],
) -> str | None:
    default_source = defaults.get(_PIPEWIRE_DEFAULT_AUDIO_SOURCE_KEY)
    if default_source in {node_id, name}:
        return _DEFAULT_SOURCE_ROLE
    default_sink = defaults.get(_PIPEWIRE_DEFAULT_AUDIO_SINK_KEY)
    if default_sink is None:
        return None
    if default_sink in {node_id, name} or name == f"{default_sink}.monitor":
        return _DEFAULT_SINK_ROLE
    return None


def audio_level_percent(samples: bytes) -> int:
    """Reduce one PCM frame to a bounded in-memory display level."""
    if not samples:
        return 0
    maximum = 0
    for offset in range(0, len(samples), PCM_S16LE_SAMPLE_BYTES):
        sample = int.from_bytes(
            samples[offset : offset + PCM_S16LE_SAMPLE_BYTES],
            PCM_S16LE_BYTEORDER,
            signed=True,
        )
        maximum = max(maximum, abs(sample))
    return min(
        _AUDIO_LEVEL_PERCENT_MAXIMUM,
        round(maximum * _AUDIO_LEVEL_PERCENT_MAXIMUM / _PCM_S16LE_MAX_ABSOLUTE_SAMPLE),
    )


async def _read_bounded_pipewire_stream(
    reader: asyncio.StreamReader,
    maximum_bytes: int,
) -> tuple[bytes, bool]:
    """Drain a PipeWire subprocess stream while retaining only its bounded prefix."""
    retained = bytearray()
    truncated = False
    while chunk := await reader.read(_PIPEWIRE_DISCOVERY_READ_BYTES):
        remaining = maximum_bytes - len(retained)
        if remaining <= 0:
            truncated = True
            continue
        retained.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
    return bytes(retained), truncated


async def _terminate_pipewire_process(
    process: asyncio.subprocess.Process,
) -> None:
    """Stop a timed-out PipeWire subprocess without leaving a child behind."""
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(
            process.wait(),
            timeout=_PIPEWIRE_TERMINATION_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        process.kill()
        await process.wait()


def validate_pipewire_node_identifier(node_id: str) -> None:
    """Reject identifiers that cannot safely become a PipeWire subprocess argument."""
    if not _NODE_ID.fullmatch(node_id):
        raise CaptureError("PipeWire node must be a short node name or serial")
