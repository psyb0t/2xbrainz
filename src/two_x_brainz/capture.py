"""Linux PipeWire PCM capture run inside the application container."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass

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
    PIPEWIRE_OPTION_RATE,
    PIPEWIRE_OPTION_TARGET,
    PIPEWIRE_OUTPUT_TARGET,
    PIPEWIRE_RECORD_COMMAND,
    SPEECH_ACTIVITY_SAMPLE_THRESHOLD,
    SPEECH_TURN_SILENCE_FRAME_COUNT,
)
from two_x_brainz.contracts import AudioFrame, SpeakerRole
from two_x_brainz.errors import CaptureError
from two_x_brainz.json_support import (
    decode_json,
    require_json_array,
    require_json_object,
)

logger = logging.getLogger(__name__)

_NODE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_PIPEWIRE_DUMP_COMMAND = "pw-dump"
_MAX_DEVICE_OUTPUT_BYTES = 1_000_000
_EMPTY_CAPTURE_ERROR = "PipeWire capture ended without PCM frames"


@dataclass(frozen=True, slots=True)
class PipeWireSource:
    """A single PipeWire node captured as bounded PCM16LE frames."""

    node_id: str

    def __post_init__(self) -> None:
        if not _NODE_ID.fullmatch(self.node_id):
            raise CaptureError("PipeWire node must be a short node name or serial")

    async def frames(self) -> AsyncIterator[bytes]:
        """Yield 20 ms PCM frames and terminate the child process on exit."""
        command = [
            PIPEWIRE_RECORD_COMMAND,
            PIPEWIRE_OPTION_TARGET,
            self.node_id,
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

        logger.info("PipeWire capture started", extra={"node_id": self.node_id})
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
            logger.info("PipeWire capture stopped", extra={"node_id": self.node_id})


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
    """Rotate the ASR transport after audible speech is followed by silence."""

    def __init__(self, frames: AsyncIterable[AudioFrame]) -> None:
        self._frames = aiter(frames)
        self._capture_ended = False
        self._last_boundary: str | None = None

    @property
    def capture_ended(self) -> bool:
        """Whether the underlying capture source has ended."""
        return self._capture_ended

    @property
    def last_boundary(self) -> str | None:
        """Return why the latest ASR segment ended."""
        return self._last_boundary

    async def next_speech_frame(self) -> AudioFrame | None:
        """Wait for audible input before opening another native ASR transport."""
        if self._capture_ended:
            return None
        async for frame in self._frames:
            if _frame_has_speech(frame.samples):
                return frame
        self._capture_ended = True
        self._last_boundary = "capture_ended"
        return None

    async def next_segment(
        self,
        first_speech_frame: AudioFrame | None = None,
    ) -> AsyncIterator[AudioFrame]:
        """Yield a continuous frame slice ending after sustained silence."""
        if self._capture_ended:
            return
        self._last_boundary = None
        saw_speech = first_speech_frame is not None
        consecutive_silence_frames = 0
        if first_speech_frame is not None:
            yield first_speech_frame
        async for frame in self._frames:
            yield frame
            if _frame_has_speech(frame.samples):
                saw_speech = True
                consecutive_silence_frames = 0
                continue
            if not saw_speech:
                continue
            consecutive_silence_frames += 1
            if consecutive_silence_frames >= SPEECH_TURN_SILENCE_FRAME_COUNT:
                self._last_boundary = "silence"
                return
        self._capture_ended = True
        self._last_boundary = "capture_ended"


def _frame_has_speech(samples: bytes) -> bool:
    """Return whether a PCM16LE frame exceeds the local activity floor."""
    for offset in range(0, len(samples), PCM_S16LE_SAMPLE_BYTES):
        sample = int.from_bytes(
            samples[offset : offset + PCM_S16LE_SAMPLE_BYTES],
            PCM_S16LE_BYTEORDER,
            signed=True,
        )
        if abs(sample) >= SPEECH_ACTIVITY_SAMPLE_THRESHOLD:
            return True
    return False


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
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        raise CaptureError("start pw-dump") from error
    if process.stdout is None or process.stderr is None:
        raise CaptureError("pw-dump did not expose standard streams")

    raw = await process.stdout.read(_MAX_DEVICE_OUTPUT_BYTES + 1)
    exit_code = await process.wait()
    if len(raw) > _MAX_DEVICE_OUTPUT_BYTES:
        raise CaptureError("pw-dump output exceeds the configured size limit")
    if exit_code != 0:
        stderr = (await process.stderr.read()).decode("utf-8", errors="replace")
        raise CaptureError(f"pw-dump exited with code {exit_code}: {stderr}")
    try:
        objects = require_json_array(decode_json(raw))
    except ValueError as error:
        raise CaptureError("pw-dump returned invalid JSON") from error

    nodes: list[dict[str, str]] = []
    for item in objects:
        try:
            node = require_json_object(item)
        except ValueError:
            continue
        if node.get("type") != "PipeWire:Interface:Node":
            continue
        node_id = node.get("id")
        info = node.get("info")
        if not isinstance(node_id, int):
            continue
        try:
            info_object = require_json_object(info)
        except ValueError:
            continue
        try:
            props = require_json_object(info_object.get("props"))
        except ValueError:
            continue
        name = props.get("node.name")
        media_class = props.get("media.class")
        if isinstance(name, str) and isinstance(media_class, str):
            nodes.append({"id": str(node_id), "name": name, "media_class": media_class})
    return nodes
