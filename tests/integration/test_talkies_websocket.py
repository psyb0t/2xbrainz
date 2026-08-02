from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import AsyncIterator
from functools import partial
from typing import cast

from websockets.asyncio.server import Server, ServerConnection, serve

from two_x_brainz.constants import (
    DEFAULT_CHANNELS,
    DEFAULT_FRAME_BYTES,
    DEFAULT_SAMPLE_RATE_HZ,
)
from two_x_brainz.contracts import (
    ASRStreamStats,
    AudioFrame,
    SpeakerRole,
    TranscriptEventType,
)
from two_x_brainz.errors import CaptureError, ProtocolError, RemoteServiceError
from two_x_brainz.json_support import decode_json, require_json_object
from two_x_brainz.talkies import TalkiesClient, TalkiesStreamConfig

_SENDER_FAILURE_TIMEOUT_SECONDS = 1
_FIXTURE_CAPTURE_FAILURE_MESSAGE = "fixture capture failed"


class TalkiesWebSocketIntegrationTests(unittest.TestCase):
    def test_client_streams_pcm_and_receives_ordered_events(self) -> None:
        asyncio.run(self._assert_stream_contract())

    def test_client_propagates_capture_failure_before_server_close(self) -> None:
        asyncio.run(self._assert_capture_failure_preempts_server_close())

    def test_client_warms_a_model_with_a_silent_stream(self) -> None:
        asyncio.run(self._assert_warmup_contract())

    def test_client_rejects_invalid_warmup_frame_counts(self) -> None:
        asyncio.run(self._assert_invalid_warmup_frame_counts_are_rejected())

    def test_client_rejects_a_warmup_without_terminal_statistics(self) -> None:
        asyncio.run(self._assert_missing_warmup_statistics_are_rejected())

    def test_client_rejects_a_cancelled_warmup(self) -> None:
        asyncio.run(self._assert_cancelled_warmup_is_rejected())

    async def _assert_stream_contract(self) -> None:
        async with serve(_talkies_handler, "127.0.0.1", 0) as server:
            listener = next(iter(server.sockets), None)
            assert listener is not None
            raw_address = listener.getsockname()
            assert isinstance(raw_address, tuple)
            address = cast(tuple[object, ...], raw_address)
            assert len(address) > 1
            port_value = address[1]
            assert isinstance(port_value, int)
            port = port_value
            client = TalkiesClient(
                TalkiesStreamConfig(
                    url=f"ws://127.0.0.1:{port}/v1/audio/transcriptions/stream",
                    model="nemotron-3.5-asr-0.6b",
                    token=None,
                )
            )
            events = [
                event
                async for event in client.transcribe(
                    session_id="session",
                    stream_id="remote-stream",
                    speaker_role=SpeakerRole.REMOTE,
                    frames=_frames(),
                )
            ]

        transcripts = [
            event for event in events if not isinstance(event, ASRStreamStats)
        ]
        statistics = [event for event in events if isinstance(event, ASRStreamStats)]
        self.assertEqual(
            [event.source_event_type for event in transcripts],
            [TranscriptEventType.PARTIAL, TranscriptEventType.FINAL],
        )
        self.assertEqual(transcripts[-1].text, "synthetic fixture")
        self.assertEqual(
            tuple(word.word for word in transcripts[-1].words),
            ("synthetic", "fixture"),
        )
        self.assertEqual(len(statistics), 1)
        self.assertEqual(statistics[0].frames, 1)
        self.assertFalse(statistics[0].canceled)

    async def _assert_capture_failure_preempts_server_close(self) -> None:
        async with serve(_talkies_waiting_handler, "127.0.0.1", 0) as server:
            port = _server_port(server)
            client = TalkiesClient(
                TalkiesStreamConfig(
                    url=f"ws://127.0.0.1:{port}/v1/audio/transcriptions/stream",
                    model="nemotron-3.5-asr-0.6b",
                    token=None,
                )
            )
            with self.assertRaisesRegex(CaptureError, _FIXTURE_CAPTURE_FAILURE_MESSAGE):
                async with asyncio.timeout(_SENDER_FAILURE_TIMEOUT_SECONDS):
                    async for _ in client.transcribe(
                        session_id="session",
                        stream_id="remote-stream",
                        speaker_role=SpeakerRole.REMOTE,
                        frames=_frames(raise_capture_error=True),
                    ):
                        pass

    async def _assert_warmup_contract(self) -> None:
        async with serve(_talkies_warmup_handler, "127.0.0.1", 0) as server:
            port = _server_port(server)
            client = TalkiesClient(
                TalkiesStreamConfig(
                    url=f"ws://127.0.0.1:{port}/v1/audio/transcriptions/stream",
                    model="nemotron-3.5-asr-0.6b",
                    token=None,
                )
            )
            await client.warm_configured_model()

    async def _assert_invalid_warmup_frame_counts_are_rejected(self) -> None:
        for invalid_frame_count in (0, 2):
            with self.subTest(frame_count=invalid_frame_count):
                handler = partial(
                    _talkies_invalid_warmup_handler,
                    frame_count=invalid_frame_count,
                )
                async with serve(handler, "127.0.0.1", 0) as server:
                    client = _client_for_server(server)
                    with self.assertRaisesRegex(ProtocolError, "frame count"):
                        await client.warm_configured_model()

    async def _assert_missing_warmup_statistics_are_rejected(self) -> None:
        async with serve(
            _talkies_missing_warmup_stats_handler, "127.0.0.1", 0
        ) as server:
            client = _client_for_server(server)
            with self.assertRaisesRegex(
                RemoteServiceError, "without terminal statistics"
            ):
                await client.warm_configured_model()

    async def _assert_cancelled_warmup_is_rejected(self) -> None:
        async with serve(_talkies_cancelled_warmup_handler, "127.0.0.1", 0) as server:
            client = _client_for_server(server)
            with self.assertRaisesRegex(ProtocolError, "canceled"):
                await client.warm_configured_model()


async def _talkies_handler(connection: ServerConnection) -> None:
    start = await connection.recv()
    assert isinstance(start, str)
    message = require_json_object(decode_json(start))
    model = message.get("model")
    sample_rate = message.get("sample_rate")
    channels = message.get("channels")
    assert isinstance(model, str)
    assert isinstance(sample_rate, int)
    assert isinstance(channels, int)
    await connection.send(
        json.dumps(
            {
                "type": "ready",
                "model": model,
                "sample_rate": sample_rate,
                "channels": channels,
            }
        )
    )
    frame = await connection.recv()
    assert isinstance(frame, bytes)
    await connection.send(
        json.dumps(
            {
                "type": "partial",
                "revision": 1,
                "text": "synthetic",
                "words": [{"word": "synthetic", "start": 0.0, "end": 0.02}],
                "audio_seconds": 0.02,
                "is_final": False,
            }
        )
    )
    end = await connection.recv()
    assert json.loads(end) == {"type": "end"}
    await connection.send(
        json.dumps(
            {
                "type": "final",
                "revision": 2,
                "text": "fixture",
                "words": [{"word": "fixture", "start": 0.02, "end": 0.04}],
                "audio_seconds": 0.02,
                "is_final": True,
            }
        )
    )
    await connection.send(
        json.dumps(
            {
                "type": "stats",
                "audio_seconds": 0.02,
                "frames": 1,
                "canceled": False,
            }
        )
    )


async def _talkies_waiting_handler(connection: ServerConnection) -> None:
    start = await connection.recv()
    assert isinstance(start, str)
    message = require_json_object(decode_json(start))
    model = message.get("model")
    sample_rate = message.get("sample_rate")
    channels = message.get("channels")
    assert isinstance(model, str)
    assert isinstance(sample_rate, int)
    assert isinstance(channels, int)
    await connection.send(
        json.dumps(
            {
                "type": "ready",
                "model": model,
                "sample_rate": sample_rate,
                "channels": channels,
            }
        )
    )
    await connection.wait_closed()


async def _talkies_warmup_handler(connection: ServerConnection) -> None:
    model, sample_rate, channels = await _receive_start(connection)
    await connection.send(
        json.dumps(
            {
                "type": "ready",
                "model": model,
                "sample_rate": sample_rate,
                "channels": channels,
            }
        )
    )
    frame = await connection.recv()
    assert isinstance(frame, bytes)
    assert frame == bytes(DEFAULT_FRAME_BYTES)
    end = await connection.recv()
    assert isinstance(end, str)
    assert json.loads(end) == {"type": "end"}
    await connection.send(
        json.dumps(
            {
                "type": "stats",
                "audio_seconds": 0.02,
                "frames": 1,
                "canceled": False,
            }
        )
    )


async def _talkies_invalid_warmup_handler(
    connection: ServerConnection,
    frame_count: int,
) -> None:
    model, sample_rate, channels = await _receive_start(connection)
    await connection.send(
        json.dumps(
            {
                "type": "ready",
                "model": model,
                "sample_rate": sample_rate,
                "channels": channels,
            }
        )
    )
    frame = await connection.recv()
    assert isinstance(frame, bytes)
    assert frame == bytes(DEFAULT_FRAME_BYTES)
    end = await connection.recv()
    assert isinstance(end, str)
    assert json.loads(end) == {"type": "end"}
    await connection.send(
        json.dumps(
            {
                "type": "stats",
                "audio_seconds": 0.02,
                "frames": frame_count,
                "canceled": False,
            }
        )
    )


async def _talkies_missing_warmup_stats_handler(connection: ServerConnection) -> None:
    model, sample_rate, channels = await _receive_start(connection)
    await connection.send(
        json.dumps(
            {
                "type": "ready",
                "model": model,
                "sample_rate": sample_rate,
                "channels": channels,
            }
        )
    )
    frame = await connection.recv()
    assert isinstance(frame, bytes)
    assert frame == bytes(DEFAULT_FRAME_BYTES)
    end = await connection.recv()
    assert isinstance(end, str)
    assert json.loads(end) == {"type": "end"}


async def _talkies_cancelled_warmup_handler(connection: ServerConnection) -> None:
    model, sample_rate, channels = await _receive_start(connection)
    await connection.send(
        json.dumps(
            {
                "type": "ready",
                "model": model,
                "sample_rate": sample_rate,
                "channels": channels,
            }
        )
    )
    frame = await connection.recv()
    assert isinstance(frame, bytes)
    assert frame == bytes(DEFAULT_FRAME_BYTES)
    end = await connection.recv()
    assert isinstance(end, str)
    assert json.loads(end) == {"type": "end"}
    await connection.send(
        json.dumps(
            {
                "type": "stats",
                "audio_seconds": 0.0,
                "frames": 0,
                "canceled": True,
            }
        )
    )


async def _receive_start(connection: ServerConnection) -> tuple[str, int, int]:
    start = await connection.recv()
    assert isinstance(start, str)
    message = require_json_object(decode_json(start))
    model = message.get("model")
    sample_rate = message.get("sample_rate")
    channels = message.get("channels")
    assert isinstance(model, str)
    assert isinstance(sample_rate, int)
    assert isinstance(channels, int)
    return model, sample_rate, channels


def _client_for_server(server: Server) -> TalkiesClient:
    port = _server_port(server)
    return TalkiesClient(
        TalkiesStreamConfig(
            url=f"ws://127.0.0.1:{port}/v1/audio/transcriptions/stream",
            model="nemotron-3.5-asr-0.6b",
            token=None,
        )
    )


def _server_port(server: Server) -> int:
    listener = next(iter(server.sockets), None)
    assert listener is not None
    raw_address = listener.getsockname()
    assert isinstance(raw_address, tuple)
    address = cast(tuple[object, ...], raw_address)
    assert len(address) > 1
    port = address[1]
    assert isinstance(port, int)
    return port


async def _frames(raise_capture_error: bool = False) -> AsyncIterator[AudioFrame]:
    if raise_capture_error:
        raise CaptureError(_FIXTURE_CAPTURE_FAILURE_MESSAGE)
    yield AudioFrame(
        session_id="session",
        stream_id="remote-stream",
        speaker_role=SpeakerRole.REMOTE,
        sequence=0,
        captured_at_monotonic=1.0,
        sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
        channels=DEFAULT_CHANNELS,
        samples=b"\x00\x00" * 320,
    )
