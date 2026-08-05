from __future__ import annotations

import asyncio
import unittest
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import two_x_brainz.capture as capture
from two_x_brainz.capture import (
    CaptureFrameMonitor,
    InterStreamDriftMonitor,
    PipeWireSource,
    SilenceTurnSegmenter,
    audio_level_percent,
    list_pipewire_nodes,
    normalize_pcm_frames,
)
from two_x_brainz.constants import (
    DEFAULT_FRAME_BYTES,
    MAX_CAPTURE_FRAME_GAP_SECONDS,
    MAX_INTER_STREAM_DRIFT_PENDING_FRAMES,
    PCM_S16LE_SAMPLE_BYTES,
    VAD_MAX_SEGMENT_AUDIO_BYTES,
    VAD_SILENCE_WINDOW_COUNT,
)
from two_x_brainz.contracts import AudioFrame, SpeakerRole
from two_x_brainz.errors import CaptureError
from two_x_brainz.vad import StreamingVoiceActivityDetector


class CaptureFrameNormalizationTests(unittest.TestCase):
    def test_short_chunks_assemble_one_exact_frame(self) -> None:
        first_chunk = b"\x01\x00" * 80
        second_chunk = b"\x02\x00" * 240

        frames = asyncio.run(_collect_frames((first_chunk, second_chunk)))

        self.assertEqual(frames, (first_chunk + second_chunk,))
        self.assertEqual(len(frames[0]), DEFAULT_FRAME_BYTES)

    def test_coalesced_chunk_splits_into_exact_frames(self) -> None:
        samples_per_frame = DEFAULT_FRAME_BYTES // PCM_S16LE_SAMPLE_BYTES
        chunk = b"\x03\x00" * (samples_per_frame * 2)

        frames = asyncio.run(_collect_frames((chunk,)))

        expected_frames = (
            chunk[:DEFAULT_FRAME_BYTES],
            chunk[DEFAULT_FRAME_BYTES:],
        )
        self.assertEqual(frames, expected_frames)

    def test_empty_stream_emits_no_frames(self) -> None:
        frames = asyncio.run(_collect_frames(()))

        self.assertEqual(frames, ())

    def test_odd_length_chunk_is_rejected(self) -> None:
        with self.assertRaisesRegex(CaptureError, "odd-length"):
            asyncio.run(_collect_frames((b"\x00",)))

    def test_incomplete_trailing_frame_is_rejected(self) -> None:
        samples_per_frame = DEFAULT_FRAME_BYTES // PCM_S16LE_SAMPLE_BYTES
        incomplete_chunk = b"\x00\x00" * (samples_per_frame - 1)

        with self.assertRaisesRegex(CaptureError, "incomplete"):
            asyncio.run(_collect_frames((incomplete_chunk,)))


class AudioLevelTests(unittest.TestCase):
    def test_reduces_pcm_to_a_bounded_peak_percentage(self) -> None:
        self.assertEqual(audio_level_percent(b""), 0)
        self.assertEqual(audio_level_percent(b"\x00\x00"), 0)
        self.assertEqual(audio_level_percent(b"\x00\x80"), 100)
        self.assertGreater(audio_level_percent(b"\x00\x40"), 0)


class PipeWireSourceTests(unittest.TestCase):
    def test_only_system_capture_requests_sink_monitor_ports(self) -> None:
        microphone_command = self._capture_command(PipeWireSource("microphone"))
        system_command = self._capture_command(
            PipeWireSource("speakers", capture_sink=True)
        )

        self.assertNotIn("--properties", microphone_command)
        properties_index = system_command.index("--properties")
        self.assertEqual(
            system_command[properties_index + 1],
            "{ stream.capture.sink = true node.dont-fallback = true }",
        )

    def _capture_command(self, source: PipeWireSource) -> tuple[object, ...]:
        process = _FakePipeWireProcess(
            stdout_chunks=(b"\x00\x00" * 320, b""),
            stderr_chunks=(b"",),
            exit_code=0,
        )
        constructor = AsyncMock(return_value=process)
        with patch.object(capture.asyncio, "create_subprocess_exec", new=constructor):
            asyncio.run(_collect_source_frames(source))
        arguments = constructor.await_args
        self.assertIsNotNone(arguments)
        assert arguments is not None
        return arguments.args


class PipeWireDiscoveryTests(unittest.TestCase):
    def test_drains_chunked_output_before_waiting_for_exit(self) -> None:
        payload = b"""[
  {
    "id": 7,
    "type": "PipeWire:Interface:Node",
    "info": {
      "props": {
        "node.name": "microphone",
        "media.class": "Audio/Source"
      },
      "params": {
        [
        ]
      }
    }
  }
]
"""
        process = _FakePipeWireProcess(
            stdout_chunks=(payload[:40], payload[40:], b""),
            stderr_chunks=(b"",),
            exit_code=0,
        )

        with patch.object(
            capture.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            nodes = asyncio.run(list_pipewire_nodes())

        self.assertEqual(
            nodes,
            [{"id": "7", "name": "microphone", "media_class": "Audio/Source"}],
        )
        self.assertEqual(process.stdout.read_count, 3)

    def test_discards_excess_output_before_reporting_the_size_error(self) -> None:
        process = _FakePipeWireProcess(
            stdout_chunks=(b"123", b"456", b""),
            stderr_chunks=(b"",),
            exit_code=0,
        )

        with (
            patch.object(capture, "_MAX_DEVICE_OUTPUT_BYTES", 5),
            patch.object(
                capture.asyncio,
                "create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ),
            self.assertRaisesRegex(CaptureError, "exceeds"),
        ):
            asyncio.run(list_pipewire_nodes())

        self.assertEqual(process.stdout.read_count, 3)

    def test_marks_current_default_source_and_sink_monitor(self) -> None:
        payload = b"""[
  {
    "id": 7,
    "type": "PipeWire:Interface:Node",
    "info": {
      "props": {
        "node.name": "mic",
        "media.class": "Audio/Source",
        "node.description": "Desk mic"
      }
    }
  },
  {
    "id": 8,
    "type": "PipeWire:Interface:Node",
    "info": {
      "props": {
        "node.name": "speakers.monitor",
        "media.class": "Audio/Source",
        "node.description": "Speakers monitor"
      }
    }
  },
  {
    "id": 1,
    "type": "PipeWire:Interface:Metadata",
    "info": {"props": {"metadata.name": "default"}},
    "metadata": [
      {"key": "default.audio.source", "value": "{\\"name\\":\\"mic\\"}"},
      {"key": "default.audio.sink", "value": "{\\"name\\":\\"speakers\\"}"}
    ]
  }
]"""
        process = _FakePipeWireProcess(
            stdout_chunks=(payload, b""),
            stderr_chunks=(b"",),
            exit_code=0,
        )

        with patch.object(
            capture.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            nodes = asyncio.run(list_pipewire_nodes())

        self.assertEqual(
            nodes,
            [
                {
                    "id": "7",
                    "name": "mic",
                    "media_class": "Audio/Source",
                    "description": "Desk mic",
                    "default_role": "source",
                },
                {
                    "id": "8",
                    "name": "speakers.monitor",
                    "media_class": "Audio/Source",
                    "description": "Speakers monitor",
                    "default_role": "sink",
                },
            ],
        )

    def test_marks_a_direct_default_sink(self) -> None:
        payload = b"""[
  {
    "id": 8,
    "type": "PipeWire:Interface:Node",
    "info": {"props": {"node.name": "headphones", "media.class": "Audio/Sink"}}
  },
  {
    "id": 1,
    "type": "PipeWire:Interface:Metadata",
    "info": {"props": {"metadata.name": "default"}},
    "metadata": [
      {"key": "default.audio.sink", "value": "{\\"name\\":\\"headphones\\"}"}
    ]
  }
]"""
        process = _FakePipeWireProcess(
            stdout_chunks=(payload, b""),
            stderr_chunks=(b"",),
            exit_code=0,
        )

        with patch.object(
            capture.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            nodes = asyncio.run(list_pipewire_nodes())

        self.assertEqual(nodes[0]["default_role"], "sink")


class CaptureFrameMonitorTests(unittest.TestCase):
    def test_assigns_identity_sequence_and_duration(self) -> None:
        monitor = _new_monitor()
        first = monitor.observe(b"\x00\x00" * 320, 10.0)
        second = monitor.observe(b"\x00\x00" * 320, 10.02)

        self.assertEqual(first.sequence, 0)
        self.assertEqual(second.sequence, 1)
        self.assertEqual(first.session_id, "session")
        self.assertEqual(first.stream_id, "microphone")
        self.assertEqual(first.speaker_role, SpeakerRole.USER)
        self.assertAlmostEqual(monitor.stats().audio_seconds, 0.04)
        self.assertEqual(monitor.stats().gap_count, 0)

    def test_records_gap_above_configured_limit(self) -> None:
        monitor = _new_monitor()
        monitor.observe(b"\x00\x00" * 320, 10.0)
        monitor.observe(
            b"\x00\x00" * 320,
            10.0 + MAX_CAPTURE_FRAME_GAP_SECONDS + 0.01,
        )

        stats = monitor.stats()
        self.assertEqual(stats.gap_count, 1)
        self.assertAlmostEqual(
            stats.max_gap_seconds,
            MAX_CAPTURE_FRAME_GAP_SECONDS + 0.01,
        )

    def test_empty_monitor_has_zero_aggregates(self) -> None:
        stats = _new_monitor().stats()

        self.assertEqual(stats.frame_count, 0)
        self.assertEqual(stats.audio_seconds, 0.0)
        self.assertEqual(stats.gap_count, 0)
        self.assertEqual(stats.max_gap_seconds, 0.0)

    def test_rejects_decreasing_timestamps(self) -> None:
        monitor = _new_monitor()
        monitor.observe(b"\x00\x00" * 320, 10.0)

        with self.assertRaisesRegex(CaptureError, "monotonic"):
            monitor.observe(b"\x00\x00" * 320, 9.99)


class InterStreamDriftMonitorTests(unittest.TestCase):
    def test_stable_startup_offset_has_no_drift(self) -> None:
        drift_monitor = InterStreamDriftMonitor()
        user_monitor = _new_monitor(drift_monitor=drift_monitor)
        remote_monitor = _new_remote_monitor(drift_monitor=drift_monitor)

        user_monitor.observe(b"\x00\x00" * 320, 10.10)
        remote_monitor.observe(b"\x00\x00" * 320, 10.00)
        user_monitor.observe(b"\x00\x00" * 320, 10.12)
        remote_monitor.observe(b"\x00\x00" * 320, 10.02)

        stats = drift_monitor.stats()
        self.assertEqual(stats.comparison_count, 2)
        self.assertEqual(stats.max_abs_drift_seconds, 0.0)
        self.assertEqual(stats.unmatched_frame_count, 0)

    def test_records_maximum_relative_drift_after_startup(self) -> None:
        drift_monitor = InterStreamDriftMonitor()
        user_monitor = _new_monitor(drift_monitor=drift_monitor)
        remote_monitor = _new_remote_monitor(drift_monitor=drift_monitor)

        user_monitor.observe(b"\x00\x00" * 320, 10.10)
        remote_monitor.observe(b"\x00\x00" * 320, 10.00)
        user_monitor.observe(b"\x00\x00" * 320, 10.15)
        remote_monitor.observe(b"\x00\x00" * 320, 10.02)

        stats = drift_monitor.stats()
        self.assertEqual(stats.comparison_count, 2)
        self.assertAlmostEqual(stats.max_abs_drift_seconds, 0.03)

    def test_unmatched_frames_are_bounded(self) -> None:
        drift_monitor = InterStreamDriftMonitor()
        user_monitor = _new_monitor(drift_monitor=drift_monitor)

        for sequence in range(MAX_INTER_STREAM_DRIFT_PENDING_FRAMES + 1):
            user_monitor.observe(b"\x00\x00" * 320, 10.0 + sequence * 0.02)

        stats = drift_monitor.stats()
        self.assertEqual(stats.comparison_count, 0)
        self.assertEqual(
            stats.unmatched_frame_count,
            MAX_INTER_STREAM_DRIFT_PENDING_FRAMES,
        )


class SilenceTurnSegmenterTests(unittest.TestCase):
    def test_splits_after_sustained_silence_and_keeps_followup_frames(self) -> None:
        probabilities = (0.0, 0.9, 0.9) + (0.0,) * VAD_SILENCE_WINDOW_COUNT + (0.9, 0.9)
        segmenter = _probability_segmenter(probabilities)

        first, second, first_boundary = asyncio.run(_collect_two_segments(segmenter))

        self.assertEqual(len(first), VAD_SILENCE_WINDOW_COUNT + 3)
        self.assertEqual(first_boundary, "silence")
        self.assertEqual(len(second), 2)
        self.assertEqual(segmenter.last_boundary, "capture_ended")
        self.assertTrue(segmenter.capture_ended)

    def test_background_noise_does_not_open_a_segment(self) -> None:
        noisy_frames = (_audible_frame(),) * 40
        detector = _probability_detector((0.01,) * len(noisy_frames))
        segmenter = SilenceTurnSegmenter(
            _audio_frames(noisy_frames),
            detector,
        )

        frames = asyncio.run(_collect_segment(segmenter))

        self.assertEqual(frames, ())
        self.assertEqual(segmenter.last_boundary, "capture_ended")
        self.assertTrue(segmenter.capture_ended)

    def test_retains_pre_roll_when_sustained_speech_opens_a_segment(self) -> None:
        probabilities = (0.0, 0.0, 0.9, 0.9) + (0.0,) * VAD_SILENCE_WINDOW_COUNT
        segmenter = _probability_segmenter(probabilities)

        first_frame, segment = asyncio.run(_collect_after_first_speech(segmenter))

        self.assertIsNotNone(first_frame)
        assert first_frame is not None
        self.assertEqual(first_frame.sequence, 0)
        self.assertEqual(len(segment), VAD_SILENCE_WINDOW_COUNT + 4)
        self.assertEqual(segmenter.last_boundary, "silence")
        self.assertFalse(segmenter.capture_ended)

    def test_isolated_positive_window_does_not_open_a_segment(self) -> None:
        segmenter = _probability_segmenter((0.0, 0.9, 0.0, 0.9, 0.0))

        frames = asyncio.run(_collect_segment(segmenter))

        self.assertEqual(frames, ())
        self.assertEqual(segmenter.last_boundary, "capture_ended")

    def test_continuous_speech_rotates_at_maximum_duration(self) -> None:
        maximum_frames = VAD_MAX_SEGMENT_AUDIO_BYTES // DEFAULT_FRAME_BYTES
        probabilities = (0.9,) * (maximum_frames + 2)
        segmenter = _probability_segmenter(probabilities)

        first, second, first_boundary = asyncio.run(_collect_two_segments(segmenter))

        self.assertEqual(len(first), maximum_frames)
        self.assertEqual(first_boundary, "max_duration")
        self.assertEqual(len(second), 2)
        self.assertEqual(segmenter.last_boundary, "capture_ended")


def _new_monitor(
    *,
    drift_monitor: InterStreamDriftMonitor | None = None,
) -> CaptureFrameMonitor:
    return CaptureFrameMonitor(
        session_id="session",
        stream_id="microphone",
        speaker_role=SpeakerRole.USER,
        drift_monitor=drift_monitor,
    )


def _new_remote_monitor(
    *,
    drift_monitor: InterStreamDriftMonitor,
) -> CaptureFrameMonitor:
    return CaptureFrameMonitor(
        session_id="session",
        stream_id="system",
        speaker_role=SpeakerRole.REMOTE,
        drift_monitor=drift_monitor,
    )


async def _collect_frames(chunks: tuple[bytes, ...]) -> tuple[bytes, ...]:
    frames = [frame async for frame in normalize_pcm_frames(_chunks(chunks))]
    return tuple(frames)


async def _collect_source_frames(source: PipeWireSource) -> tuple[bytes, ...]:
    return tuple([frame async for frame in source.frames()])


async def _chunks(chunks: tuple[bytes, ...]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def _audible_frame() -> bytes:
    return b"\x00\x01" * 320


def _probability_segmenter(
    probabilities: tuple[float, ...],
) -> SilenceTurnSegmenter:
    samples = (b"\x00\x00" * 320,) * len(probabilities)
    return SilenceTurnSegmenter(
        _audio_frames(samples),
        _probability_detector(probabilities),
    )


def _probability_detector(
    probabilities: tuple[float, ...],
) -> StreamingVoiceActivityDetector:
    return StreamingVoiceActivityDetector(
        _ProbabilityModel(probabilities),
    )


async def _audio_frames(samples: tuple[bytes, ...]) -> AsyncIterator[AudioFrame]:
    for sequence, frame_samples in enumerate(samples):
        yield AudioFrame(
            session_id="session",
            stream_id="microphone",
            speaker_role=SpeakerRole.USER,
            sequence=sequence,
            captured_at_monotonic=float(sequence),
            sample_rate_hz=16_000,
            channels=1,
            samples=frame_samples,
        )


async def _collect_segment(
    segmenter: SilenceTurnSegmenter,
) -> tuple[AudioFrame, ...]:
    frames = [frame async for frame in segmenter.next_segment()]
    return tuple(frames)


async def _collect_two_segments(
    segmenter: SilenceTurnSegmenter,
) -> tuple[tuple[AudioFrame, ...], tuple[AudioFrame, ...], str | None]:
    first = await _collect_segment(segmenter)
    first_boundary = segmenter.last_boundary
    second = await _collect_segment(segmenter)
    return first, second, first_boundary


async def _collect_after_first_speech(
    segmenter: SilenceTurnSegmenter,
) -> tuple[AudioFrame | None, tuple[AudioFrame, ...]]:
    first_frame = await segmenter.next_speech_frame()
    if first_frame is None:
        return None, ()
    frames = [frame async for frame in segmenter.next_segment(first_frame)]
    segment = tuple(frames)
    return first_frame, segment


class _ProbabilityModel:
    def __init__(self, probabilities: tuple[float, ...]) -> None:
        self._probabilities = iter(probabilities)

    def chunk_bytes(self) -> int:
        return DEFAULT_FRAME_BYTES

    def process_chunk(self, audio: bytes | bytearray | memoryview) -> float:
        self.last_chunk = bytes(audio)
        return next(self._probabilities)


class _FakePipeWireProcess:
    def __init__(
        self,
        *,
        stdout_chunks: tuple[bytes, ...],
        stderr_chunks: tuple[bytes, ...],
        exit_code: int,
    ) -> None:
        self.stdout = _ChunkedReader(stdout_chunks)
        self.stderr = _ChunkedReader(stderr_chunks)
        self.returncode = exit_code
        self._exit_code = exit_code

    async def wait(self) -> int:
        return self._exit_code


class _ChunkedReader:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = iter(chunks)
        self.read_count = 0

    async def read(self, maximum_bytes: int) -> bytes:
        del maximum_bytes
        self.read_count += 1
        return next(self._chunks)
