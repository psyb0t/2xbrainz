"""Opt-in proof that AIGate accepts two concurrent Talkies ASR streams."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

from two_x_brainz.audio import WavFixture, load_wav_fixture
from two_x_brainz.config import Settings
from two_x_brainz.constants import (
    DEFAULT_CHANNELS,
    DEFAULT_FRAME_BYTES,
    DEFAULT_FRAME_DURATION_MS,
    DEFAULT_SAMPLE_RATE_HZ,
)
from two_x_brainz.contracts import (
    ASRStreamStats,
    AudioFrame,
    SpeakerRole,
    TranscriptEvent,
    TranscriptEventType,
)
from two_x_brainz.errors import ProtocolError, TwoXBrainzError
from two_x_brainz.fixture_trace import FixtureTrace, FixtureTraceError
from two_x_brainz.talkies import TalkiesClient, TalkiesStreamConfig

_AUDIO_PATH_ENV = "TWOXBRAINZ_CONCURRENCY_AUDIO"
_TRACE_DIRECTORY_ENV = "TWOXBRAINZ_FIXTURE_TRACE_DIR"
_TALKIES_MODEL_ENV = "TWOXBRAINZ_FIXTURE_TALKIES_MODEL"
_TRACE_LABEL = "real-talkies-concurrency"
_SESSION_ID = "real-talkies-concurrency"
_EXPECTED_CONCURRENT_STREAMS = 2
_READY_BARRIER_TIMEOUT_SECONDS = 60.0
_TOTAL_TIMEOUT_SECONDS = 180.0
_STREAMS = (
    ("real-concurrency-user", SpeakerRole.USER),
    ("real-concurrency-remote", SpeakerRole.REMOTE),
)


class TalkiesConcurrencyFixtureError(RuntimeError):
    """The real Talkies concurrency proof did not meet its contract."""


class ReadyBarrier:
    """Release frame producers only after every server has replied ready."""

    def __init__(
        self,
        expected_streams: int,
        trace: FixtureTrace,
    ) -> None:
        self._expected_streams = expected_streams
        self._trace = trace
        self._arrived = 0
        self._lock = asyncio.Lock()
        self._release = asyncio.Event()

    async def wait(self, stream_id: str) -> None:
        async with self._lock:
            self._arrived += 1
            self._trace.event(
                "stream_ready",
                stream_id=stream_id,
                ready_streams=self._arrived,
            )
            if self._arrived == self._expected_streams:
                self._release.set()
        await asyncio.wait_for(
            self._release.wait(),
            timeout=_READY_BARRIER_TIMEOUT_SECONDS,
        )


def main() -> int:
    try:
        result = asyncio.run(_run())
    except (
        FixtureTraceError,
        TalkiesConcurrencyFixtureError,
        TimeoutError,
        TwoXBrainzError,
    ) as error:
        print(
            "error: real AIGate Talkies concurrency check failed: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


async def _run() -> dict[str, object]:
    settings = Settings.from_environment()
    settings = replace(
        settings,
        talkies_model=os.environ.get(_TALKIES_MODEL_ENV, settings.talkies_model),
    )
    trace = FixtureTrace(
        _trace_directory(),
        _TRACE_LABEL,
        secret_values=(settings.aigate_token or "",),
    )
    try:
        result = await _run_with_trace(settings, trace)
    except (
        TalkiesConcurrencyFixtureError,
        TimeoutError,
        TwoXBrainzError,
    ) as error:
        trace.failure(error)
        raise
    finally:
        trace.close()
    return result


async def _run_with_trace(
    settings: Settings,
    trace: FixtureTrace,
) -> dict[str, object]:
    if settings.talkies_token is None:
        raise TalkiesConcurrencyFixtureError("AIGate token is required")
    fixture = load_wav_fixture(_audio_path())
    client = TalkiesClient(
        TalkiesStreamConfig(
            url=settings.talkies_ws_url,
            model=settings.talkies_model,
            token=settings.talkies_token,
        )
    )
    trace.event(
        "fixture_started",
        model=settings.talkies_model,
        stream_count=_EXPECTED_CONCURRENT_STREAMS,
    )
    max_concurrency = await client.configured_model_max_concurrency()
    trace.event(
        "model_concurrency_verified",
        model=settings.talkies_model,
        max_concurrency=max_concurrency,
    )
    if max_concurrency < _EXPECTED_CONCURRENT_STREAMS:
        raise TalkiesConcurrencyFixtureError(
            "configured Talkies model does not advertise two concurrent requests"
        )

    barrier = ReadyBarrier(_EXPECTED_CONCURRENT_STREAMS, trace)
    async with asyncio.timeout(_TOTAL_TIMEOUT_SECONDS):
        results = await asyncio.gather(
            *(
                _run_stream(
                    client,
                    fixture,
                    stream_id,
                    speaker_role,
                    barrier,
                    trace,
                )
                for stream_id, speaker_role in _STREAMS
            )
        )
    trace.event(
        "fixture_passed",
        model=settings.talkies_model,
        max_concurrency=max_concurrency,
        completed_streams=len(results),
    )
    return {
        "kind": "real_talkies_concurrency",
        "result": "passed",
        "model": settings.talkies_model,
        "max_concurrency": max_concurrency,
        "completed_streams": len(results),
        "trace_file": str(trace.path),
    }


async def _run_stream(
    client: TalkiesClient,
    fixture: WavFixture,
    stream_id: str,
    speaker_role: SpeakerRole,
    barrier: ReadyBarrier,
    trace: FixtureTrace,
) -> ASRStreamStats:
    events = tuple(
        [
            event
            async for event in client.transcribe(
                session_id=_SESSION_ID,
                stream_id=stream_id,
                speaker_role=speaker_role,
                frames=_fixture_frames(
                    fixture,
                    stream_id,
                    speaker_role,
                    barrier,
                ),
            )
        ]
    )
    final_events = tuple(
        event
        for event in events
        if isinstance(event, TranscriptEvent)
        and event.source_event_type is TranscriptEventType.FINAL
        and event.text.strip()
    )
    if not final_events:
        raise ProtocolError("Talkies concurrent stream did not emit a usable final")
    statistics = tuple(event for event in events if isinstance(event, ASRStreamStats))
    if len(statistics) != 1:
        raise ProtocolError(
            "Talkies concurrent stream must emit exactly one stats event"
        )
    terminal_stats = statistics[0]
    if terminal_stats.canceled:
        raise ProtocolError("Talkies concurrent stream reported cancellation")
    expected_frames = _frame_count(fixture.pcm16le)
    if terminal_stats.frames != expected_frames:
        raise ProtocolError("Talkies concurrent stream reported the wrong frame count")
    trace.event(
        "stream_completed",
        stream_id=stream_id,
        speaker_role=speaker_role.value,
        frames=terminal_stats.frames,
        audio_seconds=terminal_stats.audio_seconds,
        final_characters=len(final_events[-1].text),
    )
    return terminal_stats


async def _fixture_frames(
    fixture: WavFixture,
    stream_id: str,
    speaker_role: SpeakerRole,
    barrier: ReadyBarrier,
) -> AsyncIterator[AudioFrame]:
    await barrier.wait(stream_id)
    frame_count = _frame_count(fixture.pcm16le)
    padded_pcm = fixture.pcm16le.ljust(frame_count * DEFAULT_FRAME_BYTES, b"\x00")
    for sequence in range(frame_count):
        offset = sequence * DEFAULT_FRAME_BYTES
        yield AudioFrame(
            session_id=_SESSION_ID,
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


def _audio_path() -> Path:
    value = os.environ.get(_AUDIO_PATH_ENV, "").strip()
    if not value:
        raise TalkiesConcurrencyFixtureError(f"{_AUDIO_PATH_ENV} is required")
    return Path(value)


def _trace_directory() -> Path:
    value = os.environ.get(_TRACE_DIRECTORY_ENV, "").strip()
    if not value:
        raise TalkiesConcurrencyFixtureError(f"{_TRACE_DIRECTORY_ENV} is required")
    return Path(value)


if __name__ == "__main__":
    raise SystemExit(main())
