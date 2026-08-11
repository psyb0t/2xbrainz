"""Deterministic live console fixture for the real-browser smoke test."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import signal
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

from two_x_brainz.aigate import AIGateClient
from two_x_brainz.audio_selection import (
    AudioDevice,
    AudioSelectionSetup,
)
from two_x_brainz.constants import MAX_WEB_CONSOLE_PORT, MIN_WEB_CONSOLE_PORT
from two_x_brainz.contracts import (
    DraftRequest,
    SpeakerRole,
    TranscriptLine,
    TranscriptSnapshot,
)
from two_x_brainz.logging_config import configure_logging
from two_x_brainz.provider_selection import ProviderAssignment, ProviderSelection
from two_x_brainz.terminal import LiveTerminal
from two_x_brainz.web import WebConsole, WebRuntimeSettings

_FIXTURE_MODEL = "claudebox-provider-example-model-087"
_SSE_DELAY_SECONDS = 0.03

logger = logging.getLogger(__name__)


class _AIGateFixtureHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(content_length)
        if self.path == "/v1/chat/completions":
            self._chat_completion()
            return
        if self.path == "/mcp/":
            self._mcp_result()
            return
        self.send_error(404)

    def log_message(self, format: str, *arguments: object) -> None:
        del format, arguments

    def _chat_completion(self) -> None:
        fixture_server = cast(_AIGateFixtureServer, self.server)
        with fixture_server.request_lock:
            request_index = fixture_server.chat_request_count
            fixture_server.chat_request_count += 1
        events = _tool_events() if request_index == 0 else _reply_events()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        logger.debug(
            "fake AIGate SSE response started",
            extra={"request_index": request_index, "event_count": len(events)},
        )
        for event in events:
            self.wfile.write(event)
            self.wfile.flush()
            time.sleep(_SSE_DELAY_SECONDS)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        logger.debug(
            "fake AIGate SSE response completed",
            extra={"request_index": request_index},
        )

    def _mcp_result(self) -> None:
        result_text = json.dumps(
            {
                "results": [
                    {
                        "title": "Unrelated public reference",
                        "url": "https://example.com/reference",
                        "snippet": "No clearly matching subject was found.",
                    }
                ]
            },
            separators=(",", ":"),
        )
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": result_text}]},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        logger.debug(
            "fake AIGate MCP result returned",
            extra={"tool": "research_web"},
        )


class _AIGateFixtureServer(ThreadingHTTPServer):
    chat_request_count: int
    request_lock: threading.Lock

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _AIGateFixtureHandler)
        self.chat_request_count = 0
        self.request_lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/v1"


def _sse_event(delta: dict[str, object]) -> bytes:
    payload = json.dumps({"choices": [{"delta": delta}]}, separators=(",", ":"))
    return f"data: {payload}\n\n".encode()


def _tool_events() -> tuple[bytes, ...]:
    return (
        _sse_event({"reasoning_content": "Checking the request flow."}),
        _sse_event(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "fixture-",
                        "function": {
                            "name": "research_",
                            "arguments": '{"query":"example ',
                        },
                    }
                ]
            }
        ),
        _sse_event(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "search",
                        "function": {
                            "name": "web",
                            "arguments": 'gateway request flow"}',
                        },
                    }
                ]
            }
        ),
    )


def _reply_events() -> tuple[bytes, ...]:
    return (
        _sse_event({"reasoning_content": "Using the bounded search result."}),
        _sse_event({"content": "Start at the gateway, "}),
        _sse_event({"reasoning_content": "Checking the final sequence."}),
        _sse_event({"content": "then follow validation and routing."}),
    )


def _port(value: str) -> int:
    port = int(value)
    if port < MIN_WEB_CONSOLE_PORT or port > MAX_WEB_CONSOLE_PORT:
        raise argparse.ArgumentTypeError("port is outside the supported range")
    return port


def _state(directory: Path) -> LiveTerminal:
    setup = AudioSelectionSetup(
        microphones=(
            AudioDevice(
                "1",
                "fixture-mic",
                "Audio/Source",
                "Fixture microphone",
                True,
            ),
        ),
        system_monitors=(
            AudioDevice(
                "2",
                "fixture-system",
                "Audio/Source",
                "Fixture system monitor",
                True,
            ),
        ),
        selection=None,
    )
    setup.select(0, 0)
    return LiveTerminal(
        log_file=str(directory / "browser-fixture.log"),
        audio_setup=setup,
        stream=io.StringIO(),
        _setup_preview_enabled=False,
    )


async def _accept_provider_settings(
    settings: WebRuntimeSettings,
) -> bool:
    del settings
    return True


async def _accept_manual_dispatch() -> bool:
    logger.info("manual browser dispatch accepted")
    return True


def _seed_console(console: WebConsole) -> None:
    console.configure_runtime_settings(
        models=tuple(
            f"claudebox-provider-example-model-{index:03d}" for index in range(120)
        ),
        talkies_models=("fixture-asr",),
        talkies_model="fixture-asr",
        session_brief=None,
        web_research_enabled=True,
        auto_dispatch_enabled=False,
        selection=ProviderSelection(
            draft=ProviderAssignment(_FIXTURE_MODEL, "high"),
            commentary=ProviderAssignment(_FIXTURE_MODEL, "medium"),
            summary=ProviderAssignment(_FIXTURE_MODEL, "medium"),
            research=ProviderAssignment(_FIXTURE_MODEL, "high"),
        ),
        callback=_accept_provider_settings,
        dispatch_callback=_accept_manual_dispatch,
        dispatch_state_callback=lambda: (False, True),
    )
    console.consume(
        {
            "kind": "session",
            "state": "running",
            "action": "browser fixture ready",
        }
    )
    console.consume(
        {
            "kind": "timeline",
            "turn_id": "fixture-turn",
            "speaker_role": "remote",
            "transcript_revision": 1,
            "text": "Could you walk through the request flow?",
        }
    )
    for activity in (
        {
            "flow_id": "fixture-coach",
            "output_kind": "commentary",
            "phase": "request_started",
        },
        {
            "flow_id": "fixture-coach",
            "output_kind": "commentary",
            "phase": "output_streaming",
            "output": "",
        },
        {
            "flow_id": "fixture-coach",
            "output_kind": "commentary",
            "phase": "request_cancelled",
        },
        {
            "flow_id": "fixture-story-completed",
            "output_kind": "summary",
            "phase": "request_started",
        },
        {
            "flow_id": "fixture-story-completed",
            "output_kind": "summary",
            "phase": "output_streaming",
            "output": "The discussion reached the gateway.",
        },
        {
            "flow_id": "fixture-story-completed",
            "output_kind": "summary",
            "phase": "reasoning_streaming",
            "reasoning": "Checking whether the summary is complete.",
        },
        {
            "flow_id": "fixture-story-completed",
            "output_kind": "summary",
            "phase": "request_completed",
            "output": "The discussion reached the gateway and routing stage.",
        },
        {
            "flow_id": "fixture-story-failed",
            "output_kind": "summary",
            "phase": "request_started",
        },
        {
            "flow_id": "fixture-story-failed",
            "output_kind": "summary",
            "phase": "output_streaming",
            "output": "A later summary started but did not finish.",
        },
        {
            "flow_id": "fixture-story-failed",
            "output_kind": "summary",
            "phase": "reasoning_streaming",
            "reasoning": "Checking the later summary.",
        },
        {
            "flow_id": "fixture-story-failed",
            "output_kind": "summary",
            "phase": "request_failed",
            "error_type": "TimeoutError",
            "error_message": "fixture provider deadline exceeded",
        },
        {
            "flow_id": "fixture-research",
            "output_kind": "research",
            "phase": "request_started",
        },
        {
            "flow_id": "fixture-research",
            "output_kind": "research",
            "phase": "reasoning_streaming",
            "reasoning": "Inspecting the primary project documentation.",
        },
        {
            "flow_id": "fixture-research",
            "output_kind": "research",
            "phase": "tool_started",
            "tool": "WebFetch",
            "tool_input": {"url": "https://example.com/project"},
        },
        {
            "flow_id": "fixture-research",
            "output_kind": "research",
            "phase": "tool_completed",
            "tool": "WebFetch",
            "tool_result": "Primary documentation describes the gateway flow.",
        },
        {
            "flow_id": "fixture-research",
            "output_kind": "research",
            "phase": "output_streaming",
            "output": "Verified the gateway flow from primary documentation.",
        },
        {
            "flow_id": "fixture-research",
            "output_kind": "research",
            "phase": "request_completed",
            "output": "Verified the gateway flow from primary documentation.",
        },
    ):
        console.record_provider_activity(
            {
                "kind": "provider_activity",
                "model": _FIXTURE_MODEL,
                "reasoning_exposed": False,
                "tools_enabled": False,
                **activity,
            }
        )


async def _stream_reply(console: WebConsole, base_url: str) -> None:
    await console.wait_for_client()
    client = AIGateClient(
        base_url=base_url,
        model=_FIXTURE_MODEL,
        token=None,
        web_research_enabled=True,
        activity_sink=console.record_provider_activity,
        streaming_enabled=True,
    )
    result = await client.draft(
        DraftRequest(
            generation_id="fixture-generation",
            trigger_turn_id="fixture-turn",
            context_revision=1,
            transcript=TranscriptSnapshot(
                revision=1,
                lines=(
                    TranscriptLine(
                        stream_id="fixture-remote",
                        speaker_role=SpeakerRole.REMOTE,
                        revision=1,
                        text="Could you walk through the request flow?",
                        is_final=True,
                    ),
                ),
            ),
            deadline_seconds=15,
        )
    )
    logger.info(
        "fake AIGate browser flow completed",
        extra={"output_characters": len(result.text)},
    )


async def _run(port: int, log_file: Path) -> None:
    configure_logging("DEBUG", log_file)
    with tempfile.TemporaryDirectory(prefix="2xbrainz-browser-fixture-") as temporary:
        console = WebConsole(_state(Path(temporary)), port=port)
        _seed_console(console)
        aigate = _AIGateFixtureServer()
        aigate_thread = threading.Thread(
            target=aigate.serve_forever,
            name="fake-aigate",
            daemon=True,
        )
        aigate_thread.start()
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for handled_signal in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(handled_signal, stopped.set)
        await console.open()
        generation = asyncio.create_task(
            _stream_reply(console, aigate.base_url),
            name="fake-aigate-browser-stream",
        )
        try:
            await stopped.wait()
        finally:
            if not generation.done():
                generation.cancel()
            await asyncio.gather(generation, return_exceptions=True)
            await console.close()
            aigate.shutdown()
            aigate.server_close()
            aigate_thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=_port, required=True)
    parser.add_argument("--log-file", type=Path, required=True)
    arguments = parser.parse_args()
    asyncio.run(_run(arguments.port, arguments.log_file))


if __name__ == "__main__":
    main()
