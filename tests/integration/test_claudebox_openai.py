from __future__ import annotations

import asyncio
import json
import threading
import unittest
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.parse import urlsplit

from two_x_brainz.claudebox import ClaudeboxReplyClient
from two_x_brainz.contracts import (
    DraftRequest,
    SpeakerRole,
    TranscriptLine,
    TranscriptSnapshot,
)
from two_x_brainz.errors import RemoteServiceError
from two_x_brainz.json_support import require_json_object


class ClaudeboxOpenAIIntegrationTests(unittest.TestCase):
    def test_openai_stream_uses_native_tools_and_continues_workspace(self) -> None:
        _OpenAIHandler.reset()
        activities: list[Mapping[str, object]] = []
        server, server_thread = _start_server()
        client = _client(server, activities)

        async def exercise() -> tuple[str, str, str]:
            workspace = await client.start_session()
            first = await client.draft(_request("generation-1"))
            second = await client.draft(_request("generation-2"))
            return workspace, first.text, second.text

        try:
            workspace, first, second = asyncio.run(exercise())
        finally:
            _stop_server(server, server_thread)

        self.assertEqual(first, "AIGate unifies AI providers behind one API.")
        self.assertEqual(second, "It also routes agentic and media services.")
        self.assertEqual(
            _OpenAIHandler.paths,
            ["/claudebox/openai/v1/chat/completions"] * 2,
        )
        self.assertEqual(_OpenAIHandler.auth, ["Bearer fixture-token"] * 2)
        self.assertEqual(_OpenAIHandler.workspaces, [workspace] * 2)
        self.assertEqual(_OpenAIHandler.native_tools, ["0", "0"])
        self.assertTrue(
            all("native" in value.lower() for value in _OpenAIHandler.instructions)
        )
        self.assertEqual(_OpenAIHandler.continuations, [None, "true"])
        for payload in _OpenAIHandler.payloads:
            self.assertIs(payload["stream"], True)
            self.assertEqual(payload["model"], "sonnet")
            self.assertEqual(payload["reasoning_effort"], "high")
            self.assertNotIn("tools", payload)
            self.assertNotIn("tool_choice", payload)
            self.assertNotIn("response_format", payload)
        output_events = [
            event for event in activities if event["phase"] == "output_streaming"
        ]
        self.assertGreaterEqual(len(output_events), 4)
        self.assertEqual(activities[-1]["phase"], "request_completed")

    def test_openai_stream_surfaces_missing_authentication(self) -> None:
        _OpenAIHandler.reset()
        server, server_thread = _start_server()
        client = _client(server, [], token=None)

        async def exercise() -> None:
            await client.start_session()
            await client.draft(_request("unauthenticated-generation"))

        try:
            with self.assertRaisesRegex(RemoteServiceError, "HTTP 401"):
                asyncio.run(exercise())
        finally:
            _stop_server(server, server_thread)

        self.assertEqual(_OpenAIHandler.auth, [None])
        self.assertEqual(_OpenAIHandler.payloads, [])


class _OpenAIHandler(BaseHTTPRequestHandler):
    paths: ClassVar[list[str]] = []
    auth: ClassVar[list[str | None]] = []
    workspaces: ClassVar[list[str | None]] = []
    native_tools: ClassVar[list[str | None]] = []
    instructions: ClassVar[list[str]] = []
    continuations: ClassVar[list[str | None]] = []
    payloads: ClassVar[list[dict[str, Any]]] = []

    @classmethod
    def reset(cls) -> None:
        cls.paths = []
        cls.auth = []
        cls.workspaces = []
        cls.native_tools = []
        cls.instructions = []
        cls.continuations = []
        cls.payloads = []

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        self.__class__.paths.append(path)
        self.__class__.auth.append(self.headers.get("Authorization"))
        self.__class__.workspaces.append(self.headers.get("X-Aicodebox-Workspace"))
        self.__class__.native_tools.append(self.headers.get("X-Aicodebox-No-Tools"))
        self.__class__.instructions.append(
            self.headers.get("X-Aicodebox-Append-System-Prompt", "")
        )
        self.__class__.continuations.append(self.headers.get("X-Aicodebox-Continue"))
        if path != "/claudebox/openai/v1/chat/completions":
            self.send_error(404)
            return
        if self.headers.get("Authorization") != "Bearer fixture-token":
            self.send_error(401)
            return
        payload = self._read_json()
        self.__class__.payloads.append(payload)
        index = len(self.__class__.payloads) - 1
        responses = (
            ("AIGate unifies AI ", "providers behind one API."),
            ("It also routes agentic ", "and media services."),
        )
        self._write_sse(responses[index])

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        return require_json_object(json.loads(self.rfile.read(content_length)))

    def _write_sse(self, parts: tuple[str, str]) -> None:
        events = [
            _chunk(parts[0]),
            _chunk(parts[1]),
            _chunk("", finish_reason="stop"),
        ]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        body += "data: [DONE]\n\n"
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _chunk(text: str, finish_reason: str | None = None) -> dict[str, object]:
    return {
        "choices": [
            {
                "delta": {"content": text},
                "finish_reason": finish_reason,
            }
        ]
    }


def _start_server() -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    return server, server_thread


def _stop_server(
    server: ThreadingHTTPServer,
    server_thread: threading.Thread,
) -> None:
    server.shutdown()
    server.server_close()
    server_thread.join(timeout=2)


def _client(
    server: ThreadingHTTPServer,
    activities: list[Mapping[str, object]],
    *,
    token: str | None = "fixture-token",
) -> ClaudeboxReplyClient:
    def collect(event: Mapping[str, object]) -> None:
        activities.append(dict(event))

    return ClaudeboxReplyClient(
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        model="claudebox-sonnet",
        token=token,
        activity_sink=collect,
    )


def _request(generation_id: str) -> DraftRequest:
    transcript = TranscriptSnapshot(
        revision=1,
        lines=(
            TranscriptLine(
                stream_id="remote",
                speaker_role=SpeakerRole.REMOTE,
                revision=1,
                text="What should I say next about our gateway architecture?",
                is_final=True,
            ),
        ),
    )
    return DraftRequest(
        generation_id=generation_id,
        trigger_turn_id="turn-1",
        context_revision=1,
        transcript=transcript,
        deadline_seconds=60,
    )
