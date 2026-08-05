from __future__ import annotations

import math
import unittest
from pathlib import Path

from two_x_brainz.audio import load_wav_fixture
from two_x_brainz.errors import CaptureError
from two_x_brainz.vad import StreamingVoiceActivityDetector


class StreamingVoiceActivityDetectorTests(unittest.TestCase):
    def test_buffers_capture_frames_into_exact_model_chunks(self) -> None:
        model = _ProbabilityModel((0.25, 0.75), chunk_bytes=1_024)
        detector = StreamingVoiceActivityDetector(model)

        first = detector.observe(bytes(640))
        second = detector.observe(bytes(640))
        third = detector.observe(bytes(768))

        self.assertEqual(first, ())
        self.assertEqual(second, (0.25,))
        self.assertEqual(third, (0.75,))
        self.assertEqual(detector.pending_bytes, 0)
        self.assertEqual(model.chunk_lengths, [1_024, 1_024])

    def test_rejects_odd_pcm_input(self) -> None:
        detector = StreamingVoiceActivityDetector(
            _ProbabilityModel((0.0,), chunk_bytes=2)
        )

        with self.assertRaisesRegex(CaptureError, "odd PCM16LE"):
            detector.observe(b"\x00")

    def test_rejects_invalid_model_chunk_size(self) -> None:
        with self.assertRaisesRegex(CaptureError, "invalid PCM chunk size"):
            StreamingVoiceActivityDetector(_ProbabilityModel((0.0,), chunk_bytes=3))

    def test_wraps_model_inference_failure(self) -> None:
        detector = StreamingVoiceActivityDetector(_FailingModel())

        with self.assertRaisesRegex(CaptureError, "run Silero"):
            detector.observe(bytes(2))

    def test_rejects_non_finite_and_out_of_range_probabilities(self) -> None:
        for probability in (math.nan, -0.1, 1.1):
            with self.subTest(probability=probability):
                detector = StreamingVoiceActivityDetector(
                    _ProbabilityModel((probability,), chunk_bytes=2)
                )
                with self.assertRaisesRegex(CaptureError, "invalid speech probability"):
                    detector.observe(bytes(2))

    def test_bundled_silero_model_detects_fixture_speech_and_silence(self) -> None:
        fixture_path = Path(__file__).parents[1] / "fixtures" / "commons-audio-cc0.wav"
        fixture = load_wav_fixture(fixture_path)
        detector = StreamingVoiceActivityDetector()

        speech_probabilities = detector.observe(fixture.pcm16le)
        silence_probabilities = detector.observe(bytes(16_000 * 2))

        self.assertGreater(max(speech_probabilities), 0.5)
        self.assertLess(min(silence_probabilities[-10:]), 0.35)


class _ProbabilityModel:
    def __init__(
        self,
        probabilities: tuple[float, ...],
        chunk_bytes: int,
    ) -> None:
        self._probabilities = iter(probabilities)
        self._chunk_bytes = chunk_bytes
        self.chunk_lengths: list[int] = []

    def chunk_bytes(self) -> int:
        return self._chunk_bytes

    def process_chunk(self, audio: bytes | bytearray | memoryview) -> float:
        self.chunk_lengths.append(len(audio))
        return next(self._probabilities)


class _FailingModel:
    def chunk_bytes(self) -> int:
        return 2

    def process_chunk(self, audio: bytes | bytearray | memoryview) -> float:
        del audio
        raise RuntimeError("model failure")
