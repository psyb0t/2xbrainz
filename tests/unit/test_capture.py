from __future__ import annotations

import asyncio
import unittest
from collections.abc import AsyncIterator

from two_x_brainz.capture import (
    CaptureFrameMonitor,
    InterStreamDriftMonitor,
    SilenceTurnSegmenter,
    normalize_pcm_frames,
)
from two_x_brainz.constants import (
    DEFAULT_FRAME_BYTES,
    MAX_CAPTURE_FRAME_GAP_SECONDS,
    MAX_INTER_STREAM_DRIFT_PENDING_FRAMES,
    PCM_S16LE_SAMPLE_BYTES,
    SPEECH_TURN_SILENCE_FRAME_COUNT,
)
from two_x_brainz.contracts import AudioFrame, SpeakerRole
from two_x_brainz.errors import CaptureError


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
        frames = _audio_frames(
            (b"\x00\x00" * 320, _audible_frame())
            + (b"\x00\x00" * 320,) * SPEECH_TURN_SILENCE_FRAME_COUNT
            + (_audible_frame(),)
        )
        segmenter = SilenceTurnSegmenter(frames)

        first, second, first_boundary = asyncio.run(_collect_two_segments(segmenter))

        self.assertEqual(len(first), SPEECH_TURN_SILENCE_FRAME_COUNT + 2)
        self.assertEqual(first_boundary, "silence")
        self.assertEqual(len(second), 1)
        self.assertEqual(segmenter.last_boundary, "capture_ended")
        self.assertTrue(segmenter.capture_ended)

    def test_silence_alone_does_not_rotate_an_open_segment(self) -> None:
        segmenter = SilenceTurnSegmenter(_audio_frames((b"\x00\x00" * 320,) * 3))

        frames = asyncio.run(_collect_segment(segmenter))

        self.assertEqual(len(frames), 3)
        self.assertEqual(segmenter.last_boundary, "capture_ended")
        self.assertTrue(segmenter.capture_ended)

    def test_waits_for_speech_before_starting_a_followup_segment(self) -> None:
        frames = _audio_frames(
            (b"\x00\x00" * 320,) * 2
            + (_audible_frame(),)
            + (b"\x00\x00" * 320,) * SPEECH_TURN_SILENCE_FRAME_COUNT
        )
        segmenter = SilenceTurnSegmenter(frames)

        first_frame, segment = asyncio.run(_collect_after_first_speech(segmenter))

        self.assertIsNotNone(first_frame)
        assert first_frame is not None
        self.assertEqual(first_frame.sequence, 2)
        self.assertEqual(len(segment), SPEECH_TURN_SILENCE_FRAME_COUNT + 1)
        self.assertEqual(segmenter.last_boundary, "silence")
        self.assertFalse(segmenter.capture_ended)


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


async def _chunks(chunks: tuple[bytes, ...]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def _audible_frame() -> bytes:
    return b"\x00\x01" * 320


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
