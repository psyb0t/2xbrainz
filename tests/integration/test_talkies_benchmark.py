from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar, cast
from unittest.mock import patch

from websockets.asyncio.server import ServerConnection, serve

from two_x_brainz.aigate import DraftProvider
from two_x_brainz.benchmark import run_asr_benchmark
from two_x_brainz.config import AIGateMode, Settings
from two_x_brainz.constants import DEFAULT_CHANNELS, DEFAULT_SAMPLE_RATE_HZ
from two_x_brainz.contracts import (
    DraftRequest,
    DraftResult,
    GenerationStatus,
    SpeakerRole,
)
from two_x_brainz.errors import RemoteServiceError
from two_x_brainz.json_support import decode_json, require_json_object

_FIXTURE_PATH = Path("tests/fixtures/commons-audio-cc0.wav")
_STREAM_PATH = "/v1/audio/transcriptions/stream"
_EXPECTED_CONCURRENT_STREAMS = 2
_STREAM_START_TIMEOUT_SECONDS = 2.0


class TalkiesBenchmarkIntegrationTests(unittest.TestCase):
    def test_runs_native_and_both_file_contracts_on_one_fixture(self) -> None:
        asyncio.run(self._assert_benchmark_contract())

    async def _assert_benchmark_contract(self) -> None:
        with _BatchServer() as batch_server:
            stream_audit = _StreamAudit()
            async with serve(stream_audit.handle, "127.0.0.1", 0) as stream_server:
                listener = next(iter(stream_server.sockets), None)
                assert listener is not None
                address = cast(tuple[object, ...], listener.getsockname())
                port = address[1]
                assert isinstance(port, int)
                settings = Settings(
                    talkies_ws_url=f"ws://127.0.0.1:{port}{_STREAM_PATH}",
                    talkies_model="fixture-model",
                    talkies_token=None,
                    aigate_url="http://127.0.0.1:4000/v1",
                    aigate_mode=AIGateMode.LOCAL,
                    aigate_model=None,
                    aigate_token=None,
                    log_level="INFO",
                    log_file=Path("/tmp/2xbrainz-test.log"),
                )
                with (
                    patch(
                        "two_x_brainz.talkies.batch_url",
                        return_value=batch_server.url,
                    ),
                    patch(
                        "two_x_brainz.talkies.models_url",
                        return_value=batch_server.models_url,
                    ),
                ):
                    report = await run_asr_benchmark(settings, _FIXTURE_PATH)

        self.assertEqual(report.model, "fixture-model")
        self.assertAlmostEqual(report.source_audio_seconds, 2.4)
        self.assertEqual(len(report.native_streams), _EXPECTED_CONCURRENT_STREAMS)
        self.assertEqual(
            tuple(stream.speaker_role for stream in report.native_streams),
            (SpeakerRole.USER, SpeakerRole.REMOTE),
        )
        self.assertTrue(
            all(
                stream.event_types == ("partial", "final")
                and stream.frames == 120
                and stream.audio_seconds == 2.4
                for stream in report.native_streams
            )
        )
        self.assertEqual(
            stream_audit.maximum_active_streams,
            _EXPECTED_CONCURRENT_STREAMS,
        )
        self.assertEqual(stream_audit.warmup_streams, 1)
        self.assertEqual(stream_audit.completed_streams, _EXPECTED_CONCURRENT_STREAMS)
        self.assertEqual(report.verbose_segment_count, 1)
        self.assertEqual(report.verbose_word_count, 1)
        self.assertEqual(
            batch_server.response_formats,
            ["json", "verbose_json"],
        )
        self.assertEqual(batch_server.model_inventory_requests, 1)

    def test_runs_a_text_only_draft_probe_with_native_streams(self) -> None:
        asyncio.run(self._assert_draft_probe_contract())

    def test_reports_aggregate_word_error_rates_with_a_local_reference(self) -> None:
        asyncio.run(self._assert_reference_contract())

    async def _assert_reference_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reference_path = Path(directory) / "reference.txt"
            reference_path.write_text("fixture transcript", encoding="utf-8")
            with _BatchServer() as batch_server:
                stream_audit = _StreamAudit()
                async with serve(
                    stream_audit.handle,
                    "127.0.0.1",
                    0,
                ) as stream_server:
                    listener = next(iter(stream_server.sockets), None)
                    assert listener is not None
                    address = cast(tuple[object, ...], listener.getsockname())
                    port = address[1]
                    assert isinstance(port, int)
                    settings = Settings(
                        talkies_ws_url=f"ws://127.0.0.1:{port}{_STREAM_PATH}",
                        talkies_model="fixture-model",
                        talkies_token=None,
                        aigate_url="http://127.0.0.1:4000/v1",
                        aigate_mode=AIGateMode.LOCAL,
                        aigate_model=None,
                        aigate_token=None,
                        log_level="INFO",
                        log_file=Path("/tmp/2xbrainz-test.log"),
                    )
                    with (
                        patch(
                            "two_x_brainz.talkies.batch_url",
                            return_value=batch_server.url,
                        ),
                        patch(
                            "two_x_brainz.talkies.models_url",
                            return_value=batch_server.models_url,
                        ),
                    ):
                        report = await run_asr_benchmark(
                            settings,
                            _FIXTURE_PATH,
                            reference_path=reference_path,
                        )

        self.assertEqual(
            tuple(stream.word_error_rate for stream in report.native_streams),
            (0.0, 0.0),
        )
        self.assertEqual(report.batch_json_word_error_rate, 0.0)
        self.assertEqual(report.batch_verbose_json_word_error_rate, 0.0)

    async def _assert_draft_probe_contract(self) -> None:
        with _BatchServer() as batch_server:
            stream_audit = _StreamAudit()
            draft_audit = _DraftAudit(stream_audit)
            async with serve(stream_audit.handle, "127.0.0.1", 0) as stream_server:
                listener = next(iter(stream_server.sockets), None)
                assert listener is not None
                address = cast(tuple[object, ...], listener.getsockname())
                port = address[1]
                assert isinstance(port, int)
                settings = Settings(
                    talkies_ws_url=f"ws://127.0.0.1:{port}{_STREAM_PATH}",
                    talkies_model="fixture-model",
                    talkies_token=None,
                    aigate_url="http://127.0.0.1:4000/v1",
                    aigate_mode=AIGateMode.LOCAL,
                    aigate_model="draft-model",
                    aigate_token=None,
                    log_level="INFO",
                    log_file=Path("/tmp/2xbrainz-test.log"),
                )
                with (
                    patch(
                        "two_x_brainz.talkies.batch_url",
                        return_value=batch_server.url,
                    ),
                    patch(
                        "two_x_brainz.talkies.models_url",
                        return_value=batch_server.models_url,
                    ),
                ):
                    report = await run_asr_benchmark(
                        settings,
                        _FIXTURE_PATH,
                        draft_audit,
                    )

        self.assertIsNotNone(report.draft_elapsed_seconds)
        self.assertTrue(draft_audit.completed_during_native_streams)
        self.assertEqual(len(draft_audit.requests), 1)
        self.assertNotIn("fixture", draft_audit.requests[0].transcript.lines[0].text)

    def test_rejects_unavailable_model_before_opening_asr_streams(self) -> None:
        asyncio.run(self._assert_unavailable_model_aborts_early())

    async def _assert_unavailable_model_aborts_early(self) -> None:
        with _BatchServer(model_ids=("other-model",)) as batch_server:
            stream_audit = _StreamAudit()
            async with serve(stream_audit.handle, "127.0.0.1", 0) as stream_server:
                listener = next(iter(stream_server.sockets), None)
                assert listener is not None
                address = cast(tuple[object, ...], listener.getsockname())
                port = address[1]
                assert isinstance(port, int)
                settings = Settings(
                    talkies_ws_url=f"ws://127.0.0.1:{port}{_STREAM_PATH}",
                    talkies_model="fixture-model",
                    talkies_token=None,
                    aigate_url="http://127.0.0.1:4000/v1",
                    aigate_mode=AIGateMode.LOCAL,
                    aigate_model=None,
                    aigate_token=None,
                    log_level="INFO",
                    log_file=Path("/tmp/2xbrainz-test.log"),
                )
                with (
                    patch(
                        "two_x_brainz.talkies.batch_url",
                        return_value=batch_server.url,
                    ),
                    patch(
                        "two_x_brainz.talkies.models_url",
                        return_value=batch_server.models_url,
                    ),
                    self.assertRaisesRegex(RemoteServiceError, "not available"),
                ):
                    await run_asr_benchmark(settings, _FIXTURE_PATH)

        self.assertEqual(stream_audit.maximum_active_streams, 0)
        self.assertEqual(batch_server.response_formats, [])
        self.assertEqual(batch_server.model_inventory_requests, 1)


class _StreamAudit:
    def __init__(self) -> None:
        self._all_streams_started = asyncio.Event()
        self.maximum_active_streams = 0
        self.active_streams = 0
        self.completed_streams = 0
        self.warmup_streams = 0

    async def wait_until_both_streams_started(self) -> None:
        await asyncio.wait_for(
            self._all_streams_started.wait(),
            timeout=_STREAM_START_TIMEOUT_SECONDS,
        )

    async def handle(self, connection: ServerConnection) -> None:
        start = await connection.recv()
        assert isinstance(start, str)
        assert "fixture transcript" not in start
        start_payload = require_json_object(decode_json(start))
        model = start_payload.get("model")
        sample_rate = start_payload.get("sample_rate")
        channels = start_payload.get("channels")
        assert isinstance(model, str)
        assert sample_rate == DEFAULT_SAMPLE_RATE_HZ
        assert channels == DEFAULT_CHANNELS
        if self.warmup_streams == 0:
            self.warmup_streams += 1
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
            frame_count = await self._receive_frames(connection)
            assert frame_count == 1
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
            return
        self.active_streams += 1
        self.maximum_active_streams = max(
            self.maximum_active_streams,
            self.active_streams,
        )
        if self.active_streams == _EXPECTED_CONCURRENT_STREAMS:
            self._all_streams_started.set()
        try:
            await asyncio.wait_for(
                self.wait_until_both_streams_started(),
                timeout=_STREAM_START_TIMEOUT_SECONDS,
            )
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
            frame_count = await self._receive_frames(connection)
            await connection.send(
                json.dumps(
                    {
                        "type": "partial",
                        "revision": 1,
                        "text": "fixture",
                        "words": [],
                        "audio_seconds": 2.4,
                        "is_final": False,
                    }
                )
            )
            await connection.send(
                json.dumps(
                    {
                        "type": "final",
                        "revision": 2,
                        "text": "fixture transcript",
                        "words": [],
                        "audio_seconds": 2.4,
                        "is_final": True,
                    }
                )
            )
            await connection.send(
                json.dumps(
                    {
                        "type": "stats",
                        "audio_seconds": 2.4,
                        "frames": frame_count,
                        "canceled": False,
                    }
                )
            )
        finally:
            self.active_streams -= 1
            self.completed_streams += 1

    async def _receive_frames(self, connection: ServerConnection) -> int:
        frame_count = 0
        while True:
            message = await connection.recv()
            if isinstance(message, bytes):
                frame_count += 1
                continue
            assert json.loads(message) == {"type": "end"}
            return frame_count


class _DraftAudit(DraftProvider):
    def __init__(self, stream_audit: _StreamAudit) -> None:
        self._stream_audit = stream_audit
        self.completed_during_native_streams = False
        self.requests: list[DraftRequest] = []

    async def draft(self, request: DraftRequest) -> DraftResult:
        self.requests.append(request)
        await self._stream_audit.wait_until_both_streams_started()
        self.completed_during_native_streams = (
            self._stream_audit.active_streams == _EXPECTED_CONCURRENT_STREAMS
        )
        return DraftResult(
            generation_id=request.generation_id,
            trigger_turn_id=request.trigger_turn_id,
            context_revision=request.context_revision,
            status=GenerationStatus.COMPLETED,
            text="synthetic draft",
        )


class _BatchServer:
    def __init__(self, model_ids: tuple[str, ...] = ("fixture-model",)) -> None:
        _BatchRequestHandler.response_formats = []
        _BatchRequestHandler.model_ids = model_ids
        _BatchRequestHandler.model_inventory_requests = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _BatchRequestHandler)
        self._thread = threading.Thread(target=self._server.serve_forever)

    @property
    def response_formats(self) -> list[str]:
        return _BatchRequestHandler.response_formats

    @property
    def url(self) -> str:
        host, port = cast(tuple[str, int], self._server.server_address)
        return f"http://{host}:{port}/v1/audio/transcriptions"

    @property
    def models_url(self) -> str:
        host, port = cast(tuple[str, int], self._server.server_address)
        return f"http://{host}:{port}/v1/models"

    @property
    def model_inventory_requests(self) -> int:
        return _BatchRequestHandler.model_inventory_requests

    def __enter__(self) -> _BatchServer:
        self._thread.start()
        return self

    def __exit__(self, *arguments: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


class _BatchRequestHandler(BaseHTTPRequestHandler):
    response_formats: ClassVar[list[str]]
    model_ids: ClassVar[tuple[str, ...]]
    model_inventory_requests: ClassVar[int]

    def do_GET(self) -> None:
        if self.path != "/v1/models":
            self.send_error(404)
            return
        type(self).model_inventory_requests += 1
        self._write_json({"data": [{"id": model_id} for model_id in self.model_ids]})

    def do_POST(self) -> None:
        content_length = self.headers.get("Content-Length")
        assert content_length is not None
        body = self.rfile.read(int(content_length))
        assert b'name="file"; filename="fixture.wav"' in body
        assert b"fixture transcript" not in body
        response_format = _response_format_from_multipart(body)
        self.response_formats.append(response_format)
        response = _batch_response(response_format)
        self._write_json(response)

    def _write_json(self, response: dict[str, object]) -> None:
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *arguments: object) -> None:
        return None


def _response_format_from_multipart(body: bytes) -> str:
    if b'name="response_format"\r\n\r\njson\r\n' in body:
        return "json"
    if b'name="response_format"\r\n\r\nverbose_json\r\n' in body:
        return "verbose_json"
    raise AssertionError("response_format field is missing from multipart body")


def _batch_response(response_format: str) -> dict[str, object]:
    if response_format == "json":
        return {"text": "fixture transcript"}
    return {
        "task": "transcribe",
        "language": "en",
        "duration": 2.4,
        "text": "fixture transcript",
        "segments": [{"start": 0.0, "end": 2.4, "text": "fixture transcript"}],
        "words": [{"start": 0.0, "end": 2.4, "word": "fixture"}],
    }
