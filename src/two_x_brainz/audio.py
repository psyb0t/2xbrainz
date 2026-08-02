"""Bounded PCM WAV loading and normalization for finite ASR evaluation."""

from __future__ import annotations

import sys
import unicodedata
import wave
from array import array
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from two_x_brainz.constants import (
    DEFAULT_CHANNELS,
    DEFAULT_SAMPLE_RATE_HZ,
    MAX_ASR_EVALUATION_WORDS,
    MAX_AUDIO_FIXTURE_BYTES,
    MAX_AUDIO_FIXTURE_DURATION_SECONDS,
    MAX_REFERENCE_TEXT_BYTES,
    PCM_S16LE_SAMPLE_BYTES,
)
from two_x_brainz.errors import AudioFixtureError


@dataclass(frozen=True, slots=True)
class WavFixture:
    """One bounded source WAV and its Talkies-native PCM representation."""

    wav_bytes: bytes
    pcm16le: bytes
    duration_seconds: float


def load_reference_text(path: Path) -> str:
    """Load one bounded, non-empty UTF-8 reference transcript."""
    if path.is_symlink() or not path.is_file():
        raise AudioFixtureError("benchmark reference must be a regular text file")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise AudioFixtureError("inspect benchmark reference") from error
    if size <= 0 or size > MAX_REFERENCE_TEXT_BYTES:
        raise AudioFixtureError("benchmark reference exceeds the configured size limit")
    try:
        encoded = path.read_bytes()
    except OSError as error:
        raise AudioFixtureError("read benchmark reference") from error
    if len(encoded) != size:
        raise AudioFixtureError("benchmark reference changed while being read")
    try:
        reference_text = encoded.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AudioFixtureError("benchmark reference must be valid UTF-8") from error
    if not reference_text.strip():
        raise AudioFixtureError("benchmark reference must contain text")
    if _reference_word_count(reference_text) > MAX_ASR_EVALUATION_WORDS:
        raise AudioFixtureError("benchmark reference exceeds the evaluation word limit")
    return reference_text


def _reference_word_count(text: str) -> int:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return len(
        "".join(
            character if character.isalnum() else " " for character in normalized
        ).split()
    )


def load_wav_fixture(path: Path) -> WavFixture:
    """Load a regular PCM WAV and normalize it to 16 kHz mono PCM16LE."""
    if path.is_symlink() or not path.is_file():
        raise AudioFixtureError("benchmark audio must be a regular WAV file")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise AudioFixtureError("inspect benchmark audio") from error
    if size <= 0 or size > MAX_AUDIO_FIXTURE_BYTES:
        raise AudioFixtureError("benchmark audio exceeds the configured size limit")
    try:
        wav_bytes = path.read_bytes()
    except OSError as error:
        raise AudioFixtureError("read benchmark audio") from error
    if len(wav_bytes) != size:
        raise AudioFixtureError("benchmark audio changed while being read")

    try:
        with wave.open(BytesIO(wav_bytes), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            frame_count = reader.getnframes()
            compression = reader.getcomptype()
            _validate_wav_metadata(
                channels=channels,
                sample_width=sample_width,
                sample_rate=sample_rate,
                frame_count=frame_count,
                compression=compression,
            )
            samples = reader.readframes(frame_count)
    except (EOFError, OSError, wave.Error) as error:
        raise AudioFixtureError(
            "benchmark audio is not a valid PCM WAV file"
        ) from error

    expected_bytes = frame_count * channels * PCM_S16LE_SAMPLE_BYTES
    if len(samples) != expected_bytes:
        raise AudioFixtureError("benchmark audio has incomplete PCM samples")
    duration_seconds = frame_count / sample_rate
    if duration_seconds > MAX_AUDIO_FIXTURE_DURATION_SECONDS:
        raise AudioFixtureError("benchmark audio exceeds the configured duration limit")
    pcm16le = _normalize_pcm(
        samples=samples,
        channels=channels,
        sample_rate=sample_rate,
    )
    if not pcm16le:
        raise AudioFixtureError("benchmark audio must contain at least one frame")
    return WavFixture(
        wav_bytes=wav_bytes,
        pcm16le=pcm16le,
        duration_seconds=duration_seconds,
    )


def _validate_wav_metadata(
    *,
    channels: int,
    sample_width: int,
    sample_rate: int,
    frame_count: int,
    compression: str,
) -> None:
    if channels not in {DEFAULT_CHANNELS, 2}:
        raise AudioFixtureError("benchmark WAV must have one or two channels")
    if sample_width != PCM_S16LE_SAMPLE_BYTES:
        raise AudioFixtureError("benchmark WAV must use signed 16-bit PCM samples")
    if sample_rate <= 0:
        raise AudioFixtureError("benchmark WAV must have a positive sample rate")
    if frame_count <= 0:
        raise AudioFixtureError("benchmark WAV must contain at least one frame")
    if compression != "NONE":
        raise AudioFixtureError("benchmark WAV must use uncompressed PCM")


def _normalize_pcm(
    *,
    samples: bytes,
    channels: int,
    sample_rate: int,
) -> bytes:
    normalized_samples = _decode_samples(samples)
    if channels == 2:
        normalized_samples = _downmix_stereo(normalized_samples)
    if sample_rate != DEFAULT_SAMPLE_RATE_HZ:
        normalized_samples = _resample(
            samples=normalized_samples,
            source_rate=sample_rate,
        )
    return _encode_samples(normalized_samples)


def _decode_samples(samples: bytes) -> array[int]:
    decoded = array("h")
    if decoded.itemsize != PCM_S16LE_SAMPLE_BYTES:
        raise AudioFixtureError("platform does not support signed 16-bit PCM")
    decoded.frombytes(samples)
    if sys.byteorder != "little":
        decoded.byteswap()
    return decoded


def _downmix_stereo(samples: array[int]) -> array[int]:
    return array(
        "h",
        (
            (int(samples[index]) + int(samples[index + 1])) // 2
            for index in range(0, len(samples), 2)
        ),
    )


def _resample(*, samples: array[int], source_rate: int) -> array[int]:
    target_frame_count = round(len(samples) * DEFAULT_SAMPLE_RATE_HZ / source_rate)
    output = array("h")
    for target_index in range(target_frame_count):
        source_position = target_index * source_rate
        source_index, remainder = divmod(source_position, DEFAULT_SAMPLE_RATE_HZ)
        next_index = min(source_index + 1, len(samples) - 1)
        first = int(samples[source_index])
        second = int(samples[next_index])
        output.append(first + (second - first) * remainder // DEFAULT_SAMPLE_RATE_HZ)
    return output


def _encode_samples(samples: array[int]) -> bytes:
    if sys.byteorder == "little":
        return samples.tobytes()
    encoded = array("h", samples)
    encoded.byteswap()
    return encoded.tobytes()
