from __future__ import annotations

import hashlib
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from two_x_brainz.audio import load_reference_text, load_wav_fixture
from two_x_brainz.constants import MAX_ASR_EVALUATION_WORDS
from two_x_brainz.errors import AudioFixtureError

_FIXTURE_PATH = Path("tests/fixtures/commons-audio-cc0.wav")
_FIXTURE_SHA256 = "8640a316bbbd623fc678252dcc797165c8606a4643f5834cd8f03456d2cbc72e"


class WavFixtureTests(unittest.TestCase):
    def test_cc0_speech_fixture_normalizes_to_native_pcm(self) -> None:
        fixture = load_wav_fixture(_FIXTURE_PATH)

        self.assertEqual(hashlib.sha256(fixture.wav_bytes).hexdigest(), _FIXTURE_SHA256)
        self.assertEqual(len(fixture.pcm16le), 76_800)
        self.assertAlmostEqual(fixture.duration_seconds, 2.4)

    def test_rejects_non_pcm_or_unsupported_channel_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "three-channel.wav"
            _write_wav(path, channels=3, sample_rate=16_000)

            with self.assertRaisesRegex(AudioFixtureError, "one or two channels"):
                load_wav_fixture(path)

    def test_rejects_empty_wav(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.wav"
            _write_wav(path, channels=1, sample_rate=16_000, frames=b"")

            with self.assertRaisesRegex(AudioFixtureError, "at least one frame"):
                load_wav_fixture(path)

    def test_rejects_invalid_wav_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.wav"
            path.write_bytes(b"not-a-wav")

            with self.assertRaisesRegex(AudioFixtureError, "valid PCM WAV"):
                load_wav_fixture(path)

    def test_rejects_non_16_bit_pcm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "eight-bit.wav"
            _write_wav(
                path,
                channels=1,
                sample_rate=16_000,
                sample_width=1,
                frames=b"\x80" * 10,
            )

            with self.assertRaisesRegex(AudioFixtureError, "signed 16-bit"):
                load_wav_fixture(path)

    def test_rejects_fixture_larger_than_configured_limit(self) -> None:
        with (
            patch(
                "two_x_brainz.audio.MAX_AUDIO_FIXTURE_BYTES",
                1,
            ),
            self.assertRaisesRegex(AudioFixtureError, "size limit"),
        ):
            load_wav_fixture(_FIXTURE_PATH)


class ReferenceTextTests(unittest.TestCase):
    def test_loads_regular_non_empty_utf8_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reference.txt"
            path.write_text("example reference", encoding="utf-8")

            self.assertEqual(load_reference_text(path), "example reference")

    def test_rejects_invalid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reference.txt"
            path.write_bytes(b"\xff")

            with self.assertRaisesRegex(AudioFixtureError, "valid UTF-8"):
                load_reference_text(path)

    def test_rejects_blank_or_oversized_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blank_path = Path(directory) / "blank.txt"
            blank_path.write_text(" \n", encoding="utf-8")
            oversized_path = Path(directory) / "oversized.txt"
            oversized_path.write_text("reference", encoding="utf-8")

            with self.assertRaisesRegex(AudioFixtureError, "contain text"):
                load_reference_text(blank_path)
            with (
                patch("two_x_brainz.audio.MAX_REFERENCE_TEXT_BYTES", 1),
                self.assertRaisesRegex(AudioFixtureError, "size limit"),
            ):
                load_reference_text(oversized_path)

    def test_rejects_a_symlink_or_excessive_word_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reference_path = Path(directory) / "reference.txt"
            reference_path.write_text("reference text", encoding="utf-8")
            symlink_path = Path(directory) / "reference-link.txt"
            symlink_path.symlink_to(reference_path)
            word_limit_path = Path(directory) / "word-limit.txt"
            word_limit_path.write_text(
                "word " * (MAX_ASR_EVALUATION_WORDS + 1),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(AudioFixtureError, "regular text file"):
                load_reference_text(symlink_path)
            with self.assertRaisesRegex(AudioFixtureError, "evaluation word limit"):
                load_reference_text(word_limit_path)


def _write_wav(
    path: Path,
    *,
    channels: int,
    sample_rate: int,
    sample_width: int = 2,
    frames: bytes = b"\x00\x00" * 10,
) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(sample_rate)
        writer.writeframes(frames)
