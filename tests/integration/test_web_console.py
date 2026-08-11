from __future__ import annotations

import asyncio
import io
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.typing import Origin

from two_x_brainz.audio_selection import (
    AudioDevice,
    AudioSelection,
    AudioSelectionSetup,
)
from two_x_brainz.provider_selection import (
    ProviderAssignment,
    ProviderSelection,
)
from two_x_brainz.terminal import LiveTerminal
from two_x_brainz.web import WebConsole, WebRuntimeSettings


class WebConsoleIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_static_root_and_hashed_asset_are_served(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            static_directory = _static_app(Path(temporary_directory))
            console = WebConsole(
                _state_with_selection(),
                port=_free_port(),
                static_directory=static_directory,
            )
            selection = await console.open()
            try:
                async with httpx.AsyncClient() as client:
                    root = await client.get(_require_url(console))
                    asset = await client.get(f"{_require_url(console)}assets/app.js")
                    missing = await client.get(
                        f"{_require_url(console)}assets/missing.js"
                    )
            finally:
                await console.close()

        self.assertEqual(selection, AudioSelection("mic", "system"))
        self.assertEqual(root.status_code, 200)
        self.assertIn("2xbrainz-svelte-shell", root.text)
        self.assertIn("default-src 'self'", root.headers["content-security-policy"])
        self.assertEqual(root.headers["x-content-type-options"], "nosniff")
        self.assertEqual(root.headers["x-frame-options"], "DENY")
        self.assertEqual(root.headers["referrer-policy"], "no-referrer")
        self.assertEqual(
            root.headers["permissions-policy"],
            "camera=(), microphone=(), geolocation=()",
        )
        self.assertEqual(asset.status_code, 200)
        self.assertIn("javascript", asset.headers["content-type"])
        self.assertEqual(missing.status_code, 404)

    async def test_websocket_streams_state_and_routes_controls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            console = WebConsole(
                _state_with_selection(),
                port=_free_port(),
                static_directory=_static_app(Path(temporary_directory)),
            )
            console.configure_runtime_settings(
                models=("model-a",),
                talkies_models=("asr-a",),
                talkies_model="asr-a",
                session_brief=None,
                web_research_enabled=True,
                auto_dispatch_enabled=True,
                selection=ProviderSelection.uniform("model-a", "none"),
                callback=AsyncMock(return_value=True),
                dispatch_callback=AsyncMock(return_value=True),
                dispatch_state_callback=lambda: (True, False),
            )
            await console.open()
            try:
                async with connect(
                    _websocket_url(console),
                    origin=_origin(console),
                    proxy=None,
                ) as websocket:
                    snapshot = json.loads(await websocket.recv())
                    await websocket.send(
                        json.dumps({"type": "control", "command": "pause"})
                    )
                    await websocket.send(
                        json.dumps({"type": "control", "command": "resume"})
                    )
                    self.assertEqual(snapshot["type"], "snapshot")
                    self.assertIn("activeAudio", snapshot)
                    self.assertIn("audioSetup", snapshot)
                    self.assertEqual(await anext(console.control_lines()), "pause")
                    self.assertEqual(await anext(console.control_lines()), "resume")
            finally:
                await console.close()

    async def test_websocket_routes_manual_dispatch_and_publishes_pending_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            dispatch = AsyncMock(return_value=True)
            console = WebConsole(
                _state_with_selection(),
                port=_free_port(),
                static_directory=_static_app(Path(temporary_directory)),
            )
            console.configure_runtime_settings(
                models=("model-a",),
                talkies_models=("asr-a",),
                talkies_model="asr-a",
                session_brief=None,
                web_research_enabled=True,
                auto_dispatch_enabled=False,
                selection=ProviderSelection.uniform("model-a", "none"),
                callback=AsyncMock(return_value=True),
                dispatch_callback=dispatch,
                dispatch_state_callback=lambda: (False, True),
            )
            await console.open()
            try:
                async with connect(
                    _websocket_url(console),
                    origin=_origin(console),
                    proxy=None,
                ) as websocket:
                    snapshot = json.loads(await websocket.recv())
                    await websocket.send(json.dumps({"type": "dispatch"}))
                    await asyncio.sleep(0.05)
            finally:
                await console.close()

        self.assertEqual(
            snapshot["dispatch"],
            {"autoEnabled": False, "hasUnsentTranscript": True},
        )
        dispatch.assert_awaited_once_with()

    async def test_frontend_stream_diagnostics_are_validated_and_logged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            console = WebConsole(
                _state_with_selection(),
                port=_free_port(),
                static_directory=_static_app(Path(temporary_directory)),
            )
            await console.open()
            try:
                with self.assertLogs("two_x_brainz.web", level="DEBUG") as captured:
                    async with connect(
                        _websocket_url(console),
                        origin=_origin(console),
                        proxy=None,
                    ) as websocket:
                        await websocket.recv()
                        await websocket.send(
                            json.dumps(
                                {
                                    "type": "client_debug",
                                    "event": "provider_feed_rendered",
                                    "output_kind": "draft",
                                    "item_count": 5,
                                    "text_characters": 81,
                                }
                            )
                        )
                        await asyncio.sleep(0.05)
            finally:
                await console.close()

        diagnostic = next(
            record
            for record in captured.records
            if record.getMessage() == "frontend stream diagnostic received"
        )
        self.assertEqual(
            diagnostic.__dict__["frontend_event"], "provider_feed_rendered"
        )
        self.assertEqual(diagnostic.__dict__["output_kind"], "draft")
        self.assertEqual(diagnostic.__dict__["item_count"], 5)
        self.assertEqual(diagnostic.__dict__["text_characters"], 81)

    async def test_websocket_rejects_cross_origin_and_malformed_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            console = WebConsole(
                _state_with_selection(),
                port=_free_port(),
                static_directory=_static_app(Path(temporary_directory)),
            )
            await console.open()
            try:
                with self.assertRaises(InvalidStatus):
                    async with connect(
                        _websocket_url(console),
                        origin=Origin("https://attacker.invalid"),
                        proxy=None,
                    ):
                        pass
                async with connect(
                    _websocket_url(console),
                    origin=_origin(console),
                    proxy=None,
                ) as websocket:
                    await websocket.recv()
                    await websocket.send('{"type":"control","command":"stop"}')
                    with self.assertRaises(ConnectionClosedError) as closed:
                        await websocket.recv()
                    assert closed.exception.rcvd is not None
                    self.assertEqual(closed.exception.rcvd.code, 1008)
                for invalid_settings in (
                    {**_runtime_settings_payload(), "token": "not-allowed"},
                    {
                        **_runtime_settings_payload(),
                        "session_brief": "x" * 4_001,
                    },
                ):
                    async with connect(
                        _websocket_url(console),
                        origin=_origin(console),
                        proxy=None,
                    ) as websocket:
                        await websocket.recv()
                        await websocket.send(json.dumps(invalid_settings))
                        with self.assertRaises(ConnectionClosedError) as closed:
                            await websocket.recv()
                        assert closed.exception.rcvd is not None
                        self.assertEqual(closed.exception.rcvd.code, 1008)
                async with connect(
                    _websocket_url(console),
                    origin=_origin(console),
                    proxy=None,
                ) as websocket:
                    await websocket.recv()
                    await websocket.send("x" * 20_000)
                    with self.assertRaises(ConnectionClosedError) as closed:
                        await websocket.recv()
                    assert closed.exception.rcvd is not None
                    self.assertEqual(closed.exception.rcvd.code, 1009)
                async with connect(
                    _websocket_url(console),
                    origin=_origin(console),
                    proxy=None,
                ) as websocket:
                    await websocket.recv()
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "client_debug",
                                "event": "inject_arbitrary_log_message",
                                "message": "untrusted text",
                            }
                        )
                    )
                    with self.assertRaises(ConnectionClosedError) as closed:
                        await websocket.recv()
                    assert closed.exception.rcvd is not None
                    self.assertEqual(closed.exception.rcvd.code, 1008)
                async with connect(
                    _websocket_url(console),
                    origin=_origin(console),
                    proxy=None,
                ) as websocket:
                    await websocket.recv()
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "audio_selection",
                                "microphone_index": 99,
                                "system_index": 99,
                            }
                        )
                    )
                    await asyncio.sleep(0.05)
                self.assertEqual(
                    console.state.current_audio_selection,
                    AudioSelection("mic", "system"),
                )
            finally:
                await console.close()

    async def test_audio_selection_is_applied_through_runtime_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            setup = _audio_setup()
            setup.select(0, 0)
            state = LiveTerminal(
                log_file="/tmp/2xbrainz-web.log",
                audio_setup=setup,
                stream=io.StringIO(),
                _setup_preview_enabled=False,
            )
            console = WebConsole(
                state,
                port=_free_port(),
                static_directory=_static_app(Path(temporary_directory)),
            )
            console.configure_runtime_settings(
                models=("model-a",),
                talkies_models=("asr-a",),
                talkies_model="asr-a",
                session_brief=None,
                web_research_enabled=True,
                auto_dispatch_enabled=True,
                selection=ProviderSelection.uniform("model-a", "none"),
                callback=AsyncMock(return_value=True),
                dispatch_callback=AsyncMock(return_value=True),
                dispatch_state_callback=lambda: (True, False),
            )
            await console.open()
            try:
                async with connect(
                    _websocket_url(console),
                    origin=_origin(console),
                    proxy=None,
                ) as websocket:
                    await websocket.recv()
                    await websocket.send(
                        json.dumps(
                            {
                                **_runtime_settings_payload(),
                                "microphone_node": "backup-mic",
                                "system_node": "backup-system",
                            }
                        )
                    )
                    await asyncio.sleep(0.05)
            finally:
                await console.close()

        selection = state.current_audio_selection
        assert selection is not None
        self.assertEqual(selection.mic_node, "backup-mic")
        self.assertEqual(selection.system_node, "backup-system")

    async def test_websocket_updates_provider_and_redetects_audio_devices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = _state_with_selection()
            console = WebConsole(
                state,
                port=_free_port(),
                static_directory=_static_app(Path(temporary_directory)),
            )
            callback = AsyncMock()
            console.configure_runtime_settings(
                models=("model-a", "model-b"),
                talkies_models=("asr-a",),
                talkies_model="asr-a",
                session_brief=None,
                web_research_enabled=True,
                auto_dispatch_enabled=True,
                selection=ProviderSelection.uniform("model-a", "none"),
                callback=callback,
                dispatch_callback=AsyncMock(return_value=True),
                dispatch_state_callback=lambda: (True, False),
            )
            with patch(
                "two_x_brainz.web.list_pipewire_nodes",
                new_callable=AsyncMock,
                return_value=[],
            ) as discover:
                await console.open()
                try:
                    async with connect(
                        _websocket_url(console),
                        origin=_origin(console),
                        proxy=None,
                    ) as websocket:
                        await websocket.recv()
                        await websocket.send(
                            json.dumps(
                                {
                                    **_runtime_settings_payload(
                                        summary_model="model-not-in-inventory"
                                    ),
                                }
                            )
                        )
                        await websocket.send(
                            json.dumps(
                                {
                                    **_runtime_settings_payload(
                                        summary_model="model-b"
                                    ),
                                }
                            )
                        )
                        await websocket.send(json.dumps({"type": "audio_rescan"}))
                        await asyncio.sleep(0.05)
                finally:
                    await console.close()

        callback.assert_awaited_once()
        callback_call = callback.await_args
        assert callback_call is not None
        applied = callback_call.args[0]
        self.assertIsInstance(applied, WebRuntimeSettings)
        self.assertEqual(
            applied.providers.summary, ProviderAssignment("model-b", "high")
        )
        self.assertEqual(
            console.snapshot().provider_selection,
            ProviderSelection(
                draft=ProviderAssignment("model-a", "none"),
                commentary=ProviderAssignment("model-a", "none"),
                summary=ProviderAssignment("model-b", "high"),
                research=ProviderAssignment("model-a", "high"),
            ),
        )
        discover.assert_awaited_once_with()
        assert state.audio_setup is not None
        self.assertEqual(state.audio_setup.microphones, ())
        self.assertTrue(console.snapshot().requires_audio_setup)


def _audio_setup() -> AudioSelectionSetup:
    return AudioSelectionSetup(
        microphones=(
            AudioDevice("1", "mic", "Audio/Source", "Microphone", True),
            AudioDevice("3", "backup-mic", "Audio/Source", "Backup microphone", False),
        ),
        system_monitors=(
            AudioDevice("2", "system", "Audio/Source", "System monitor", True),
            AudioDevice(
                "4",
                "backup-system",
                "Audio/Source",
                "Backup system monitor",
                False,
            ),
        ),
        selection=None,
    )


def _state_with_selection() -> LiveTerminal:
    setup = _audio_setup()
    setup.select(0, 0)
    return LiveTerminal(
        log_file="/tmp/2xbrainz-web.log",
        audio_setup=setup,
        stream=io.StringIO(),
        _setup_preview_enabled=False,
    )


def _runtime_settings_payload(
    *,
    summary_model: str = "model-a",
) -> dict[str, object]:
    return {
        "type": "runtime_settings",
        "schema_version": 2,
        "providers": {
            "draft": {"model": "model-a", "reasoning_effort": "none"},
            "commentary": {"model": "model-a", "reasoning_effort": "none"},
            "summary": {"model": summary_model, "reasoning_effort": "high"},
            "research": {"model": "model-a", "reasoning_effort": "high"},
        },
        "talkies_model": "asr-a",
        "session_brief": "",
        "web_research_enabled": True,
        "auto_dispatch_enabled": True,
        "microphone_node": "mic",
        "system_node": "system",
    }


def _static_app(directory: Path) -> Path:
    static_directory = directory / "dist"
    assets_directory = static_directory / "assets"
    assets_directory.mkdir(parents=True)
    (static_directory / "index.html").write_text(
        '<!doctype html><main id="2xbrainz-svelte-shell">2xbrainz</main>',
        encoding="utf-8",
    )
    (assets_directory / "app.js").write_text("export {};", encoding="utf-8")
    return static_directory


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _require_url(console: WebConsole) -> str:
    if console.url is None:
        raise AssertionError("web console did not publish a loopback URL")
    return console.url


def _websocket_url(console: WebConsole) -> str:
    return _require_url(console).replace("http://", "ws://") + "ws"


def _origin(console: WebConsole) -> Origin:
    return Origin(_require_url(console).removesuffix("/"))
