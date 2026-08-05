"""Loopback web server for the compiled Svelte operator console."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Mapping
from dataclasses import dataclass, field
from json import JSONDecodeError
from pathlib import Path
from typing import Annotated, Literal

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from two_x_brainz.audio_selection import AudioDevice, AudioSelection
from two_x_brainz.capture import list_pipewire_nodes
from two_x_brainz.constants import (
    DEFAULT_WEB_CONSOLE_PORT,
    MAX_WEB_CONSOLE_PORT,
    MIN_WEB_CONSOLE_PORT,
    WEB_CONSOLE_HOST,
)
from two_x_brainz.errors import CaptureError, WebConsoleError
from two_x_brainz.terminal import LiveTerminal

_SNAPSHOT_INTERVAL_SECONDS = 0.25
_SERVER_START_TIMEOUT_SECONDS = 5
_SERVER_CLOSE_TIMEOUT_SECONDS = 5
_MAX_CLIENT_MESSAGE_BYTES = 4_096
_WEB_TITLE = "2xbrainz operator console"
_WEB_URL_MESSAGE = "web operator console ready"
_STATIC_INDEX_FILENAME = "index.html"
_USER_ROLE = "user"
_REMOTE_ROLE = "remote"
_CONTROL_COMMANDS = frozenset({"pause", "resume"})
_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high"})
_MAX_MODEL_ID_CHARACTERS = 200
_MAX_PROVIDER_ACTIVITY_ENTRIES = 80
_POLICY_VIOLATION_CLOSE_CODE = 1008
_MESSAGE_TOO_LARGE_CLOSE_CODE = 1009
_DEFAULT_STATIC_DIRECTORY = Path(__file__).resolve().parents[2] / "web" / "dist"
_SECURITY_HEADERS = (
    (
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' ws://127.0.0.1:* ws://localhost:*; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
)

logger = logging.getLogger(__name__)


class _ControlMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["control"]
    command: Literal["pause", "resume"]


class _AudioSelectionMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["audio_selection"]
    microphone_index: int = Field(ge=0)
    system_index: int = Field(ge=0)


class _AudioMeteringMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["audio_metering"]
    enabled: bool


class _AudioRescanMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["audio_rescan"]


class _ProviderSettingsMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["provider_settings"]
    model: str = Field(min_length=1, max_length=_MAX_MODEL_ID_CHARACTERS)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"]


_ClientMessage = Annotated[
    _ControlMessage
    | _AudioSelectionMessage
    | _AudioMeteringMessage
    | _AudioRescanMessage
    | _ProviderSettingsMessage,
    Field(discriminator="type"),
]
_CLIENT_MESSAGE_ADAPTER: TypeAdapter[_ClientMessage] = TypeAdapter(_ClientMessage)


@dataclass(frozen=True, slots=True)
class WebAudioMeter:
    """One safe, structured audio candidate rendered by Svelte."""

    index: int
    node_id: str
    node_name: str
    label: str
    is_default: bool
    is_selected: bool
    level: int
    is_available: bool

    def payload(self) -> dict[str, object]:
        return {
            "index": self.index,
            "nodeId": self.node_id,
            "nodeName": self.node_name,
            "label": self.label,
            "isDefault": self.is_default,
            "isSelected": self.is_selected,
            "level": self.level,
            "isAvailable": self.is_available,
        }


@dataclass(frozen=True, slots=True)
class WebSnapshot:
    """Sanitized session state sent to browser clients."""

    status: str
    notice: str
    conversation: str
    reply: str
    coach: str
    story: str
    requires_audio_setup: bool
    microphone_label: str
    microphone_node: str
    microphone_level: int
    system_label: str
    system_node: str
    system_level: int
    microphone_state: str
    system_state: str
    microphones: tuple[WebAudioMeter, ...]
    system_monitors: tuple[WebAudioMeter, ...]
    session_state: str = "starting"
    models: tuple[str, ...] = ()
    active_model: str = ""
    reasoning_effort: str = "none"
    provider_activity: tuple[dict[str, object], ...] = ()

    def payload(self) -> dict[str, object]:
        return {
            "type": "snapshot",
            "status": self.status,
            "notice": self.notice,
            "conversation": self.conversation,
            "reply": self.reply,
            "coach": self.coach,
            "story": self.story,
            "requiresAudioSetup": self.requires_audio_setup,
            "sessionState": self.session_state,
            "provider": {
                "models": list(self.models),
                "activeModel": self.active_model,
                "reasoningEffort": self.reasoning_effort,
                "activity": list(self.provider_activity),
            },
            "activeAudio": {
                "microphone": {
                    "label": self.microphone_label,
                    "nodeName": self.microphone_node,
                    "level": self.microphone_level,
                    "state": self.microphone_state,
                },
                "system": {
                    "label": self.system_label,
                    "nodeName": self.system_node,
                    "level": self.system_level,
                    "state": self.system_state,
                },
            },
            "audioSetup": {
                "microphones": [meter.payload() for meter in self.microphones],
                "systemMonitors": [meter.payload() for meter in self.system_monitors],
            },
        }


class _EmbeddedServer(uvicorn.Server):
    """Keep Uvicorn from replacing the owning CLI's signal handlers."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply a locked-down browser policy to every static response."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS:
            response.headers[name] = value
        return response


@dataclass(slots=True)
class WebConsole:
    """Serve one local Svelte view over a shared ``LiveTerminal`` state."""

    state: LiveTerminal
    port: int = DEFAULT_WEB_CONSOLE_PORT
    static_directory: Path = _DEFAULT_STATIC_DIRECTORY
    _app: FastAPI | None = field(default=None, init=False)
    _server: _EmbeddedServer | None = field(default=None, init=False)
    _server_task: asyncio.Task[None] | None = field(default=None, init=False)
    _url: str | None = field(default=None, init=False)
    _models: tuple[str, ...] = field(default=(), init=False)
    _active_model: str = field(default="", init=False)
    _reasoning_effort: str = field(default="none", init=False)
    _provider_activity: deque[dict[str, object]] = field(
        default_factory=lambda: deque(maxlen=_MAX_PROVIDER_ACTIVITY_ENTRIES),
        init=False,
    )
    _provider_settings_callback: Callable[[str, str], Awaitable[None]] | None = field(
        default=None, init=False
    )

    @property
    def interactive(self) -> bool:
        """Whether the local HTTP server owns the runtime control channel."""
        return self._server is not None and self._server.started

    @property
    def url(self) -> str | None:
        """Return the loopback URL once the static server is ready."""
        return self._url

    async def open(self) -> AudioSelection | None:
        """Start the static server without starting or blocking on capture."""
        self._validate_startup()
        if self.state.current_audio_selection is not None:
            self.state.apply_audio_selection(self.state.current_audio_selection)
        self.state.activate_presentation()
        self._app = self._build_app()
        config = uvicorn.Config(
            self._app,
            host=WEB_CONSOLE_HOST,
            port=self.port,
            access_log=False,
            log_config=None,
            lifespan="off",
            server_header=False,
            ws_max_size=_MAX_CLIENT_MESSAGE_BYTES,
            timeout_graceful_shutdown=_SERVER_CLOSE_TIMEOUT_SECONDS,
        )
        self._server = _EmbeddedServer(config)
        self._server_task = asyncio.create_task(
            self._server.serve(),
            name="svelte-web-console",
            context=contextvars.copy_context(),
        )
        await self._wait_until_started()
        self._url = f"http://{WEB_CONSOLE_HOST}:{self.port}/"
        logger.info(_WEB_URL_MESSAGE, extra={"url": self._url})
        print(f"2xbrainz web console: {self._url}", flush=True)
        return self.state.current_audio_selection

    async def close(self) -> None:
        """Stop browser clients, capture previews, and the embedded server."""
        await self.state.close()
        server = self._server
        task = self._server_task
        self._server = None
        self._server_task = None
        self._app = None
        self._url = None
        if server is None or task is None:
            return
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=_SERVER_CLOSE_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning(
                "web console shutdown exceeded deadline",
                extra={"reason": "web_console_shutdown_deadline_exceeded"},
            )
            server.force_exit = True
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def consume(self, record: Mapping[str, object]) -> None:
        """Apply one sanitized runtime record to shared presentation state."""
        if record.get("kind") == "provider_activity":
            self.record_provider_activity(record)
        self.state.consume(record)

    def set_audio_level(self, speaker_role: str, percent: int) -> None:
        """Keep the shared presentation-only active meters current."""
        self.state.set_audio_level(speaker_role, percent)

    async def control_lines(self) -> AsyncIterator[str]:
        """Expose the strict runtime control queue."""
        async for line in self.state.control_lines():
            yield line

    def snapshot(self) -> WebSnapshot:
        """Build one structured browser update from bounded console state."""
        return WebSnapshot(
            status=self.state.status_text().plain,
            notice=self.state.notice_text().plain,
            conversation=self.state.conversation_text().plain,
            reply=self.state.reply_text().plain,
            coach=self.state.coach_text().plain,
            story=self.state.story_text().plain,
            requires_audio_setup=self.state.requires_audio_setup,
            microphone_label=self.state.microphone_label or "",
            microphone_node=self.state.microphone_node,
            microphone_level=self.state.audio_level(_USER_ROLE),
            system_label=self.state.system_label or "",
            system_node=self.state.system_node,
            system_level=self.state.audio_level(_REMOTE_ROLE),
            microphone_state=self.state.audio_channel_state(_USER_ROLE),
            system_state=self.state.audio_channel_state(_REMOTE_ROLE),
            microphones=self._audio_meters(_USER_ROLE, self._microphones()),
            system_monitors=self._audio_meters(
                _REMOTE_ROLE,
                self._system_monitors(),
            ),
            session_state=self.state.session_state,
            models=self._models,
            active_model=self._active_model,
            reasoning_effort=self._reasoning_effort,
            provider_activity=tuple(self._provider_activity),
        )

    def configure_provider(
        self,
        *,
        models: tuple[str, ...],
        active_model: str,
        reasoning_effort: str,
        callback: Callable[[str, str], Awaitable[None]],
    ) -> None:
        """Publish validated provider options and install the runtime updater."""
        self._models = models
        self._active_model = active_model
        self._reasoning_effort = reasoning_effort
        self._provider_settings_callback = callback

    def record_provider_activity(self, activity: Mapping[str, object]) -> None:
        """Retain a bounded, already-sanitized provider activity timeline."""
        self._provider_activity.append(dict(activity))

    def start_audio_metering(self) -> None:
        """Start all candidate probes while the source modal is visible."""
        self.state.start_setup_audio_metering(
            self._microphones(),
            self._system_monitors(),
        )

    def stop_audio_metering(self) -> None:
        """Release setup-only probes while the source modal is closed."""
        self.state.cancel_setup_audio_preview()

    def pause(self) -> None:
        self._submit_control("pause")

    def resume(self) -> None:
        self._submit_control("resume")

    async def handle_websocket(self, websocket: WebSocket) -> None:
        """Validate one same-origin client and bridge state plus controls."""
        if websocket.headers.get("origin") not in self._allowed_origins():
            logger.warning(
                "web console websocket rejected",
                extra={"reason": "websocket_origin_rejected"},
            )
            await websocket.close(code=_POLICY_VIOLATION_CLOSE_CODE)
            return
        await websocket.accept()
        sender = asyncio.create_task(
            self._send_snapshots(websocket),
            name="web-console-snapshot-sender",
            context=contextvars.copy_context(),
        )
        receiver = asyncio.create_task(
            self._receive_commands(websocket),
            name="web-console-command-receiver",
            context=contextvars.copy_context(),
        )
        done, pending = await asyncio.wait(
            {sender, receiver},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in (*done, *pending):
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                await task

    def _build_app(self) -> FastAPI:
        app = FastAPI(
            title=_WEB_TITLE,
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )
        app.add_middleware(_SecurityHeadersMiddleware)

        async def websocket_endpoint(websocket: WebSocket) -> None:
            await self.handle_websocket(websocket)

        app.add_api_websocket_route("/ws", websocket_endpoint)
        app.mount(
            "/",
            StaticFiles(directory=self.static_directory, html=True),
            name="svelte-static",
        )
        return app

    async def _send_snapshots(self, websocket: WebSocket) -> None:
        while True:
            await websocket.send_json(self.snapshot().payload())
            await asyncio.sleep(_SNAPSHOT_INTERVAL_SECONDS)

    async def _receive_commands(self, websocket: WebSocket) -> None:
        while True:
            raw = await websocket.receive_text()
            if len(raw.encode("utf-8")) > _MAX_CLIENT_MESSAGE_BYTES:
                await websocket.close(code=_MESSAGE_TOO_LARGE_CLOSE_CODE)
                return
            try:
                payload = json.loads(raw)
                message = _CLIENT_MESSAGE_ADAPTER.validate_python(payload)
            except (JSONDecodeError, ValidationError):
                logger.warning(
                    "web console command rejected",
                    extra={"reason": "websocket_message_invalid"},
                )
                await websocket.close(code=_POLICY_VIOLATION_CLOSE_CODE)
                return
            if isinstance(message, _ControlMessage):
                self._submit_control(message.command)
                continue
            if isinstance(message, _AudioMeteringMessage):
                if message.enabled:
                    self.start_audio_metering()
                else:
                    self.stop_audio_metering()
                continue
            if isinstance(message, _AudioRescanMessage):
                await self._rescan_audio()
                continue
            if isinstance(message, _ProviderSettingsMessage):
                await self._set_provider_settings(
                    message.model,
                    message.reasoning_effort,
                )
                continue
            self._select_audio(message.microphone_index, message.system_index)

    async def _rescan_audio(self) -> None:
        setup = self.state.audio_setup
        if setup is None:
            return
        try:
            setup.refresh(await list_pipewire_nodes())
        except CaptureError:
            logger.warning(
                "audio device rescan failed",
                extra={"reason": "audio_device_rescan_failed"},
            )
            return
        self.stop_audio_metering()
        self.start_audio_metering()

    async def _set_provider_settings(self, model: str, reasoning_effort: str) -> None:
        callback = self._provider_settings_callback
        if (
            callback is None
            or model not in self._models
            or reasoning_effort not in _REASONING_EFFORTS
        ):
            logger.warning(
                "provider settings rejected",
                extra={"reason": "provider_settings_invalid"},
            )
            return
        await callback(model, reasoning_effort)
        self._active_model = model
        self._reasoning_effort = reasoning_effort

    def _select_audio(self, microphone_index: int, system_index: int) -> None:
        microphones = self._microphones()
        system_monitors = self._system_monitors()
        if microphone_index >= len(microphones) or system_index >= len(system_monitors):
            logger.warning(
                "web console audio selection rejected",
                extra={"reason": "audio_selection_index_out_of_range"},
            )
            return
        self.state.select_audio_setup(microphone_index, system_index)

    def _submit_control(self, command: str) -> None:
        if command not in _CONTROL_COMMANDS:
            return
        self.state.submit_control(command)

    def _audio_meters(
        self,
        speaker_role: str,
        devices: tuple[AudioDevice, ...],
    ) -> tuple[WebAudioMeter, ...]:
        selection = self.state.current_audio_selection
        selected_node = (
            ""
            if selection is None
            else selection.mic_node
            if speaker_role == _USER_ROLE
            else selection.system_node
        )
        meters: list[WebAudioMeter] = []
        for index, device in enumerate(devices):
            level, available = self.state.setup_audio_meter(
                speaker_role,
                device.name,
            )
            meters.append(
                WebAudioMeter(
                    index=index,
                    node_id=device.node_id,
                    node_name=device.name,
                    label=device.label,
                    is_default=device.is_default,
                    is_selected=selected_node in {device.name, device.node_id},
                    level=level,
                    is_available=available,
                )
            )
        return tuple(meters)

    def _microphones(self) -> tuple[AudioDevice, ...]:
        setup = self.state.audio_setup
        return () if setup is None else setup.microphones

    def _system_monitors(self) -> tuple[AudioDevice, ...]:
        setup = self.state.audio_setup
        return () if setup is None else setup.system_monitors

    def _allowed_origins(self) -> frozenset[str]:
        return frozenset(
            {
                f"http://{WEB_CONSOLE_HOST}:{self.port}",
                f"http://localhost:{self.port}",
            }
        )

    def _validate_startup(self) -> None:
        if not MIN_WEB_CONSOLE_PORT <= self.port <= MAX_WEB_CONSOLE_PORT:
            raise ValueError("web console port is outside the allowed range")
        index_path = self.static_directory / _STATIC_INDEX_FILENAME
        if not index_path.is_file():
            raise WebConsoleError("compiled Svelte web console is missing")

    async def _wait_until_started(self) -> None:
        server = self._server
        task = self._server_task
        if server is None or task is None:
            raise WebConsoleError("web console server did not initialize")
        deadline = asyncio.get_running_loop().time() + _SERVER_START_TIMEOUT_SECONDS
        while not server.started:
            if task.done():
                try:
                    task.result()
                except (OSError, RuntimeError) as error:
                    raise WebConsoleError("start web console server") from error
                raise WebConsoleError("web console server stopped during startup")
            if asyncio.get_running_loop().time() >= deadline:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                raise WebConsoleError("web console startup exceeded deadline")
            await asyncio.sleep(0.01)
