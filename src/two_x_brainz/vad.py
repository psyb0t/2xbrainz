"""Stateful Silero voice-activity inference for streaming PCM capture."""

from __future__ import annotations

import math
from typing import Protocol

from pysilero_vad import SileroVoiceActivityDetector

from two_x_brainz.errors import CaptureError


class SpeechProbabilityModel(Protocol):
    """The narrow stateful model surface required by the capture pipeline."""

    def chunk_bytes(self) -> int: ...

    def process_chunk(self, audio: bytes | bytearray | memoryview) -> float: ...


class StreamingVoiceActivityDetector:
    """Buffer arbitrary PCM frames into exact Silero inference windows."""

    def __init__(self, model: SpeechProbabilityModel | None = None) -> None:
        try:
            self._model: SpeechProbabilityModel = model or SileroVoiceActivityDetector()
            self._chunk_bytes = self._model.chunk_bytes()
        except (OSError, RuntimeError, ValueError) as error:
            raise CaptureError("initialize Silero voice activity detector") from error
        if self._chunk_bytes <= 0 or self._chunk_bytes % 2:
            raise CaptureError("Silero VAD reported an invalid PCM chunk size")
        self._buffer = bytearray()

    @property
    def pending_bytes(self) -> int:
        """Return PCM bytes not yet large enough for one inference window."""
        return len(self._buffer)

    def observe(self, samples: bytes) -> tuple[float, ...]:
        """Return validated probabilities for every complete buffered window."""
        if len(samples) % 2:
            raise CaptureError("voice activity input has an odd PCM16LE byte length")
        self._buffer.extend(samples)
        probabilities: list[float] = []
        while len(self._buffer) >= self._chunk_bytes:
            chunk = bytes(self._buffer[: self._chunk_bytes])
            del self._buffer[: self._chunk_bytes]
            probabilities.append(self._process_chunk(chunk))
        return tuple(probabilities)

    def _process_chunk(self, chunk: bytes) -> float:
        try:
            probability = float(self._model.process_chunk(chunk))
        except (OSError, RuntimeError, ValueError) as error:
            raise CaptureError("run Silero voice activity inference") from error
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise CaptureError("Silero VAD returned an invalid speech probability")
        return probability
