"""Interactive Textual operator console for a live conversation session."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import ClassVar, TextIO

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.timer import Timer
from textual.widgets import Footer, Input, OptionList, Static

from two_x_brainz.audio_selection import (
    AudioDevice,
    AudioSelection,
    AudioSelectionSetup,
)
from two_x_brainz.capture import PipeWireSource, audio_level_percent
from two_x_brainz.errors import CaptureError, ConfigurationError

_MAX_TIMELINE_ENTRIES = 200
_USER_ROLE = "user"
_REMOTE_ROLE = "remote"
_SPEAKER_LABELS = {_USER_ROLE: "You", _REMOTE_ROLE: "Them"}
_DRAFT_KIND = "draft"
_TIMELINE_KIND = "timeline"
_TRANSCRIPT_KIND = "transcript"
_SESSION_KIND = "session"
_CONTROL_ERROR_KIND = "control_error"
_SESSION_ERROR_KIND = "session_error"
_COMMENTARY_KIND = "commentary"
_SUMMARY_KIND = "summary"
_COMPLETED_STATUS = "completed"
_RUNNING_STATUS = "running"
_FAILED_STATUS = "failed"
_CANCELLED_STATUS = "cancelled"
_EMPTY_TEXT = "—"
_TERMINAL_OUTPUT_UNAVAILABLE_REASON = "terminal_output_unavailable"
_OPERATION_WAITING = "Listening for speech"
_OPERATION_REPLY = "Calling reply LLM"
_OPERATION_REPLY_READY = "Reply suggestion ready"
_OPERATION_REPLY_FAILED = "Reply suggestion unavailable"
_OPERATION_CAPTURE_DEGRADED = "Capture or speech recognition unavailable"
_OPERATION_INSIGHT = "Updating private guidance"
_OPERATION_STOPPED = "Session stopped"
_STOP_CONTROL = object()
_UPDATE_INTERVAL_SECONDS = 0.25
_STATUS_ID = "status"
_SOURCES_ID = "sources"
_CONVERSATION_ID = "conversation"
_CONVERSATION_CONTENT_ID = "conversation-content"
_GUIDANCE_ID = "guidance"
_COMMAND_ID = "command"
_SETUP_ID = "audio-setup"
_SETUP_MICROPHONE_ID = "setup-microphone"
_SETUP_SYSTEM_ID = "setup-system"
_SETUP_MESSAGE_ID = "setup-message"
_CONTROL_PROMPT = "Enter command (pause, resume, stop)"
_VIEW_SPLIT = "split"
_VIEW_CONVERSATION = "conversation"
_VIEW_GUIDANCE = "guidance"
_VIEW_MODES = (_VIEW_SPLIT, _VIEW_CONVERSATION, _VIEW_GUIDANCE)
_CONVERSATION_VIEW_CLASS = "focus-conversation"
_GUIDANCE_VIEW_CLASS = "focus-guidance"
_LEVEL_BAR_WIDTH = 10
_PERCENT_MAXIMUM = 100
_METER_FILLED_CHARACTER = "█"
_METER_EMPTY_CHARACTER = "░"
_TERMINAL_CLOSE_TIMEOUT_SECONDS = 3
_SETUP_INITIAL_MESSAGE = (
    "Speak and play audio now. Every listed source has its own live meter."
)
_SETUP_SAVED_MESSAGE = "Saved for the next live session; current capture is unchanged."
_SETUP_SAVE_FAILED_MESSAGE = "Could not save audio setup. Check the local config path."
_SETUP_MICROPHONE_REQUIRED_MESSAGE = "Select a microphone first."
_SETUP_METER_UNAVAILABLE = " unavailable"

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LiveTerminal:
    """Bounded operator-console state with real scroll and keyboard controls."""

    log_file: str
    microphone_node: str = ""
    system_node: str = ""
    microphone_label: str | None = None
    system_label: str | None = None
    audio_setup: AudioSelectionSetup | None = None
    stream: TextIO = field(default_factory=lambda: sys.stdout)
    _timeline: deque[tuple[str, str]] = field(
        default_factory=lambda: deque(maxlen=_MAX_TIMELINE_ENTRIES),
        init=False,
    )
    _partials: dict[str, str] = field(
        default_factory=lambda: dict[str, str](),
        init=False,
    )
    _state: str = field(default="starting", init=False)
    _draft: str = field(default=_EMPTY_TEXT, init=False)
    _draft_status: str = field(default="waiting", init=False)
    _commentary: str = field(default=_EMPTY_TEXT, init=False)
    _summary: str = field(default=_EMPTY_TEXT, init=False)
    _audio_levels: dict[str, int] = field(
        default_factory=lambda: {_USER_ROLE: 0, _REMOTE_ROLE: 0},
        init=False,
    )
    _notice: str = field(default="Preparing audio and models", init=False)
    _operation: str = field(default="Preparing audio and models", init=False)
    _operation_started_at: float | None = field(default=None, init=False)
    _session_started_at: float = field(default_factory=time.monotonic, init=False)
    _active: bool = field(default=False, init=False)
    _app: OperatorConsole | None = field(default=None, init=False)
    _app_task: asyncio.Task[object] | None = field(default=None, init=False)
    _controls: asyncio.Queue[str | object] = field(
        default_factory=lambda: asyncio.Queue[str | object](),
        init=False,
    )
    _audio_selection_ready: asyncio.Event = field(
        default_factory=asyncio.Event,
        init=False,
    )
    _audio_selection_cancelled: bool = field(default=False, init=False)
    _setup_preview_enabled: bool = True
    _setup_preview_nodes: dict[str, str] = field(
        default_factory=lambda: dict[str, str](),
        init=False,
    )
    _setup_preview_labels: dict[str, str] = field(
        default_factory=lambda: dict[str, str](),
        init=False,
    )
    _setup_preview_tasks: dict[str, asyncio.Task[None]] = field(
        default_factory=lambda: dict[str, asyncio.Task[None]](),
        init=False,
    )
    _setup_preview_cleanup_tasks: set[asyncio.Task[None]] = field(
        default_factory=lambda: set[asyncio.Task[None]](),
        init=False,
    )
    _setup_preview_levels: dict[str, int] = field(
        default_factory=lambda: dict[str, int](),
        init=False,
    )
    _setup_preview_unavailable: set[str] = field(
        default_factory=lambda: set[str](),
        init=False,
    )

    @property
    def interactive(self) -> bool:
        """Whether this session owns an interactive Textual console."""
        return self._active

    @property
    def requires_audio_setup(self) -> bool:
        """Whether this live session needs an operator-selected capture pair."""
        return self.audio_setup is not None and self.audio_setup.selection is None

    @property
    def current_audio_selection(self) -> AudioSelection | None:
        """Return the startup capture pair, once setup has produced one."""
        if self.audio_setup is None:
            return None
        return self.audio_setup.selection

    @property
    def audio_setup_cancelled(self) -> bool:
        """Whether the operator quit before first-run setup could complete."""
        return self._audio_selection_cancelled

    async def open(self) -> AudioSelection | None:
        """Open setup/dashboard and return the capture pair chosen for startup."""
        if self.audio_setup is not None and self.audio_setup.selection is not None:
            self.apply_audio_selection(self.audio_setup.selection)
        if not self.stream.isatty():
            return self.current_audio_selection
        self._app = OperatorConsole(self)
        self._app_task = asyncio.create_task(
            self._app.run_async(), name="operator-console"
        )
        try:
            await self._app.ready.wait()
        except (OSError, RuntimeError):
            self._app_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._app_task
            self._app = None
            self._app_task = None
            logger.warning(
                "terminal output is unavailable",
                extra={"reason": _TERMINAL_OUTPUT_UNAVAILABLE_REASON},
            )
            return self.current_audio_selection
        self.activate_presentation()
        if self.requires_audio_setup:
            return await self.wait_for_audio_selection()
        return self.current_audio_selection

    def activate_presentation(self) -> None:
        """Mark this shared state as owned by one interactive presentation."""
        self._active = True

    async def wait_for_audio_selection(self) -> AudioSelection | None:
        """Wait for a first-run selection or an explicit setup cancellation."""
        await self._audio_selection_ready.wait()
        if self._audio_selection_cancelled:
            return None
        return self.current_audio_selection

    async def close(self) -> None:
        """Restore the calling terminal and stop waiting for console controls."""
        self._active = False
        await self.stop_setup_audio_preview()
        self._controls.put_nowait(_STOP_CONTROL)
        app = self._app
        task = self._app_task
        self._app = None
        self._app_task = None
        if app is None or task is None:
            return
        app.exit()
        try:
            await asyncio.wait_for(task, timeout=_TERMINAL_CLOSE_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning(
                "terminal shutdown exceeded deadline",
                extra={"reason": "terminal_shutdown_deadline_exceeded"},
            )
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError, TimeoutError):
                await asyncio.wait_for(
                    task,
                    timeout=_TERMINAL_CLOSE_TIMEOUT_SECONDS,
                )
        except (asyncio.CancelledError, OSError):
            return

    def consume(self, record: Mapping[str, object]) -> None:
        """Apply one safe runtime event to the visible console state."""
        kind = _text(record.get("kind"))
        if kind == _SESSION_KIND:
            self._state = _text(record.get("state"), self._state)
            self._notice = f"Session {self._state}: {_text(record.get('action'))}"
            self._set_operation(_operation_for_state(self._state))
        elif kind == _TRANSCRIPT_KIND:
            self._consume_transcript(record)
        elif kind == _TIMELINE_KIND:
            self._append_timeline(record)
        elif kind == _DRAFT_KIND:
            self._consume_draft(record)
        elif kind in {_COMMENTARY_KIND, _SUMMARY_KIND}:
            self._consume_insight(kind, record)
        elif kind == _CONTROL_ERROR_KIND:
            self._notice = _text(record.get("message"), "Invalid command")
        elif kind == _SESSION_ERROR_KIND:
            self._state = "degraded"
            self._notice = _OPERATION_CAPTURE_DEGRADED
            self._set_operation(_OPERATION_CAPTURE_DEGRADED)

        if not self._active:
            self._write_plain_update(kind)

    async def control_lines(self) -> AsyncIterator[str]:
        """Yield bounded Textual control commands for the runtime parser."""
        while True:
            control = await self._controls.get()
            if control is _STOP_CONTROL:
                return
            if isinstance(control, str):
                yield control

    def submit_control(self, command: str) -> None:
        """Queue a local command for the existing strict runtime control parser."""
        self._controls.put_nowait(command)

    def set_audio_level(self, speaker_role: str, percent: int) -> None:
        """Update a bounded presentation-only level without retaining PCM."""
        if speaker_role not in _SPEAKER_LABELS:
            return
        self._audio_levels[speaker_role] = max(0, min(percent, _PERCENT_MAXIMUM))

    def audio_level(self, speaker_role: str) -> int:
        """Return one active capture level for a structured presentation."""
        return self._audio_levels.get(speaker_role, 0)

    def set_setup_audio_level(
        self,
        speaker_role: str,
        node_name: str,
        percent: int,
    ) -> None:
        """Update one setup meter after its validated PipeWire source produces PCM."""
        if speaker_role not in _SPEAKER_LABELS:
            return
        meter_key = _setup_meter_key(speaker_role, node_name)
        if self._setup_preview_nodes.get(meter_key) != node_name:
            return
        self._setup_preview_levels[meter_key] = max(
            0,
            min(percent, _PERCENT_MAXIMUM),
        )
        self._setup_preview_unavailable.discard(meter_key)

    def start_setup_audio_metering(
        self,
        microphones: tuple[AudioDevice, ...],
        system_monitors: tuple[AudioDevice, ...],
    ) -> None:
        """Meter every setup candidate concurrently without retaining PCM frames."""
        devices = {
            **{
                _setup_meter_key(_USER_ROLE, device.name): (_USER_ROLE, device)
                for device in microphones
            },
            **{
                _setup_meter_key(_REMOTE_ROLE, device.name): (_REMOTE_ROLE, device)
                for device in system_monitors
            },
        }
        stale_keys = set(self._setup_preview_nodes).difference(devices)
        for meter_key in stale_keys:
            task = self._setup_preview_tasks.pop(meter_key, None)
            if task is not None:
                self._cancel_setup_preview_task(task)
            self._setup_preview_nodes.pop(meter_key, None)
            self._setup_preview_labels.pop(meter_key, None)
            self._setup_preview_levels.pop(meter_key, None)
            self._setup_preview_unavailable.discard(meter_key)

        for meter_key, (speaker_role, device) in devices.items():
            if self._setup_preview_nodes.get(meter_key) == device.name:
                continue
            self._setup_preview_nodes[meter_key] = device.name
            self._setup_preview_labels[meter_key] = device.label
            self._setup_preview_levels[meter_key] = 0
            self._setup_preview_unavailable.discard(meter_key)
            if not self._setup_preview_enabled:
                continue
            self._setup_preview_tasks[meter_key] = asyncio.create_task(
                self._meter_setup_audio(
                    meter_key,
                    speaker_role,
                    device.name,
                    device.capture_sink,
                ),
                name=f"setup-audio-meter-{meter_key}",
            )

    def cancel_setup_audio_preview(self) -> None:
        """Stop setup-only PipeWire probes before dashboard capture owns the choice."""
        for task in self._setup_preview_tasks.values():
            self._cancel_setup_preview_task(task)
        self._setup_preview_tasks.clear()
        self._setup_preview_nodes.clear()
        self._setup_preview_labels.clear()
        self._setup_preview_levels.clear()
        self._setup_preview_unavailable.clear()

    async def stop_setup_audio_preview(self) -> None:
        """Cancel and await setup probes so no PipeWire child survives terminal exit."""
        self.cancel_setup_audio_preview()
        tasks = tuple(self._setup_preview_cleanup_tasks)
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, CaptureError):
                await task

    def select_audio_setup(
        self,
        microphone_index: int,
        system_monitor_index: int,
    ) -> AudioSelection | None:
        """Persist a setup-screen selection without replacing active capture."""
        if self.audio_setup is None:
            return None
        self.cancel_setup_audio_preview()
        startup_selection = self.requires_audio_setup
        try:
            selection = self.audio_setup.select(
                microphone_index,
                system_monitor_index,
            )
        except (CaptureError, ConfigurationError):
            self._notice = _SETUP_SAVE_FAILED_MESSAGE
            return None
        if startup_selection:
            self.apply_audio_selection(selection)
            self._audio_selection_ready.set()
            return selection
        self._notice = _SETUP_SAVED_MESSAGE
        return selection

    def cancel_audio_setup(self) -> None:
        """Release startup without capture when the operator quits setup."""
        self.cancel_setup_audio_preview()
        self._audio_selection_cancelled = True
        self._audio_selection_ready.set()

    def status_text(self) -> Text:
        """Render a fixed status line with session and active-operation timers."""
        now = time.monotonic()
        session_elapsed = _format_elapsed(now - self._session_started_at)
        operation_elapsed = _format_elapsed(
            0.0
            if self._operation_started_at is None
            else now - self._operation_started_at
        )
        status = Text("2xbrainz  ", style="bold cyan")
        status.append(f"{self._state.upper()}  ", style="bold green")
        status.append(f"session {session_elapsed}  ", style="dim")
        status.append(f"{self._operation} {operation_elapsed}", style="bold yellow")
        return status

    def notice_text(self) -> Text:
        """Return the latest operator-facing outcome or setup message."""
        return Text(self._notice)

    def conversation_text(self) -> Text:
        """Render stable transcript history and current partial speech literally."""
        conversation = Text()
        for role, text in self._timeline:
            conversation.append(f"{_SPEAKER_LABELS[role]}\n", style=_role_style(role))
            conversation.append(f"{text}\n\n")
        for role in (_USER_ROLE, _REMOTE_ROLE):
            partial = self._partials.get(role)
            if partial:
                conversation.append(
                    f"{_SPEAKER_LABELS[role]} (live)\n",
                    style=f"italic {_role_style(role)}",
                )
                conversation.append(f"{partial}\n\n", style="dim")
        if not conversation.plain:
            conversation.append("Waiting for the first finalized turn…", style="dim")
        return conversation

    def sources_text(self) -> Text:
        """Render selected capture paths and their current derived activity level."""
        sources = Text()
        sources.append("MIC INPUT  ", style="bold green")
        sources.append(f"{_meter(self._audio_levels[_USER_ROLE])} ", style="green")
        sources.append(
            f"{_device_label(self.microphone_label, self.microphone_node)}\n"
        )
        sources.append("SYSTEM AUDIO  ", style="bold magenta")
        sources.append(f"{_meter(self._audio_levels[_REMOTE_ROLE])} ", style="magenta")
        sources.append(_device_label(self.system_label, self.system_node))
        return sources

    def setup_sources_text(self) -> Text:
        """Render every live setup meter grouped by its eventual capture role."""
        sources = Text()
        sources.append("MICROPHONE INPUTS\n", style="bold green")
        sources.append(self.setup_audio_devices_text(_USER_ROLE))
        sources.append("\nSYSTEM AUDIO SOURCES\n", style="bold magenta")
        sources.append(self.setup_audio_devices_text(_REMOTE_ROLE))
        return sources

    def setup_audio_devices_text(self, speaker_role: str) -> Text:
        """Render every candidate for one setup role with its current live meter."""
        setup = self.audio_setup
        devices: tuple[AudioDevice, ...] = ()
        if setup is not None and speaker_role == _USER_ROLE:
            devices = setup.microphones
        elif setup is not None and speaker_role == _REMOTE_ROLE:
            devices = setup.system_monitors

        rendered = Text()
        for index, device in enumerate(devices):
            if index:
                rendered.append("\n")
            rendered.append(self.setup_audio_device_label(speaker_role, device))
        if not devices:
            rendered.append(_EMPTY_TEXT, style="dim")
        return rendered

    def setup_audio_device_label(
        self,
        speaker_role: str,
        device: AudioDevice,
    ) -> str:
        """Return one literal setup row with its own currently measured level."""
        meter_key = _setup_meter_key(speaker_role, device.name)
        level = self._setup_preview_levels.get(meter_key, 0)
        unavailable = (
            _SETUP_METER_UNAVAILABLE
            if meter_key in self._setup_preview_unavailable
            else ""
        )
        return f"{_meter(level)}  {device.setup_label}{unavailable}"

    def setup_audio_meter(self, speaker_role: str, node_name: str) -> tuple[int, bool]:
        """Return one setup level and availability without exposing PCM state."""
        meter_key = _setup_meter_key(speaker_role, node_name)
        return (
            self._setup_preview_levels.get(meter_key, 0),
            meter_key not in self._setup_preview_unavailable,
        )

    def guidance_text(self) -> Text:
        """Render the reply, coach, and accumulated story in a fixed side panel."""
        guidance = Text()
        guidance.append(self.reply_text())
        guidance.append("\n\n")
        guidance.append(self.coach_text())
        guidance.append("\n\n")
        guidance.append(self.story_text())
        return guidance

    def reply_text(self) -> Text:
        """Render the latest reply independently for its own presentation pane."""
        reply = Text()
        reply.append(f"{self._draft_status.upper()}\n", style="bold green")
        reply.append(self._draft)
        return reply

    def coach_text(self) -> Text:
        """Render private coaching independently from the spoken reply."""
        coach = Text()
        coach.append("PRIVATE COACH\n", style="bold yellow")
        coach.append(self._commentary)
        return coach

    def story_text(self) -> Text:
        """Render the rolling conversation summary independently from coaching."""
        story = Text()
        story.append("STORY SO FAR\n", style="bold yellow")
        story.append(self._summary)
        return story

    def apply_audio_selection(self, selection: AudioSelection) -> None:
        """Set the labels and node identifiers used by the startup capture pair."""
        self.microphone_node = selection.mic_node
        self.system_node = selection.system_node
        self.microphone_label = selection.mic_label
        self.system_label = selection.system_label

    def _consume_transcript(self, record: Mapping[str, object]) -> None:
        role = _text(record.get("speaker_role"))
        if role not in _SPEAKER_LABELS:
            return
        if bool(record.get("is_final")):
            self._partials.pop(role, None)
            return
        text = _visible_text(record.get("text"))
        if text:
            self._partials[role] = text

    def _append_timeline(self, record: Mapping[str, object]) -> None:
        role = _text(record.get("speaker_role"))
        text = _visible_text(record.get("text"))
        if role not in _SPEAKER_LABELS or not text:
            return
        self._timeline.append((role, text))
        self._notice = f"{_SPEAKER_LABELS[role]} finished speaking"
        if role == _REMOTE_ROLE:
            self._set_operation(_OPERATION_REPLY)
        else:
            self._set_operation(_OPERATION_INSIGHT)

    def _consume_draft(self, record: Mapping[str, object]) -> None:
        status = _text(record.get("status"), _FAILED_STATUS)
        self._draft_status = status
        if status == _COMPLETED_STATUS:
            self._draft = _visible_text(record.get("text")) or _EMPTY_TEXT
            self._notice = _OPERATION_REPLY_READY
            self._set_operation(_OPERATION_REPLY_READY)
            return
        if status == _RUNNING_STATUS:
            self._draft = _EMPTY_TEXT
            self._notice = _OPERATION_REPLY
            self._set_operation(_OPERATION_REPLY)
            return
        if status in {_FAILED_STATUS, _CANCELLED_STATUS}:
            self._draft = _EMPTY_TEXT
            self._notice = _OPERATION_REPLY_FAILED
            self._set_operation(_OPERATION_REPLY_FAILED)

    def _consume_insight(self, kind: str, record: Mapping[str, object]) -> None:
        if _text(record.get("status")) != _COMPLETED_STATUS:
            return
        text = _visible_text(record.get("text"))
        if not text:
            return
        if kind == _COMMENTARY_KIND:
            self._commentary = text
        else:
            self._summary = text
        self._set_operation(_OPERATION_WAITING)

    def _set_operation(self, operation: str) -> None:
        if operation == self._operation:
            return
        self._operation = operation
        self._operation_started_at = time.monotonic()

    def _write_plain_update(self, kind: str) -> None:
        if kind == _TIMELINE_KIND and self._timeline:
            role, text = self._timeline[-1]
            self._write(f"{_SPEAKER_LABELS[role]}: {text}\n")
            return
        if kind == _DRAFT_KIND and self._draft_status == _COMPLETED_STATUS:
            self._write(f"Reply suggestion: {self._draft}\n")
            return
        if kind in {_CONTROL_ERROR_KIND, _SESSION_ERROR_KIND}:
            self._write(f"2xbrainz: {self._notice}\n")

    def _write(self, value: str) -> None:
        try:
            self.stream.write(value)
            self.stream.flush()
        except OSError:
            logger.warning(
                "terminal output became unavailable",
                extra={"reason": _TERMINAL_OUTPUT_UNAVAILABLE_REASON},
            )

    async def _meter_setup_audio(
        self,
        meter_key: str,
        speaker_role: str,
        node_name: str,
        capture_sink: bool,
    ) -> None:
        try:
            async for samples in PipeWireSource(
                node_name,
                capture_sink=capture_sink,
            ).frames():
                if self._setup_preview_nodes.get(meter_key) != node_name:
                    return
                self.set_setup_audio_level(
                    speaker_role,
                    node_name,
                    audio_level_percent(samples),
                )
        except CaptureError:
            if self._setup_preview_nodes.get(meter_key) == node_name:
                self._setup_preview_unavailable.add(meter_key)
            logger.warning(
                "audio setup meter unavailable",
                extra={
                    "reason": "audio_setup_meter_unavailable",
                    "role": speaker_role,
                },
            )

    def _cancel_setup_preview_task(self, task: asyncio.Task[None]) -> None:
        task.cancel()
        self._setup_preview_cleanup_tasks.add(task)
        task.add_done_callback(self._setup_preview_cleanup_tasks.discard)


class OperatorConsole(App[None]):
    """Textual app that keeps conversation scroll state separate from guidance."""

    CSS = """
    Screen { layout: vertical; }
    #status { height: 1; padding: 0 1; background: $surface; }
    #sources { height: 2; padding: 0 1; background: $panel; }
    #main { height: 1fr; }
    #conversation { width: 2fr; height: 1fr; border: round $primary; padding: 0 1; }
    #guidance { width: 1fr; height: 1fr; border: round $success; padding: 0 1; }
    #main.focus-conversation #guidance { display: none; }
    #main.focus-guidance #conversation { display: none; }
    #main.focus-conversation #conversation { width: 1fr; }
    #main.focus-guidance #guidance { width: 1fr; }
    #command { height: 3; }
    #audio-setup { height: 1fr; padding: 1 2; }
    #audio-setup-title { height: 2; text-style: bold; }
    #setup-microphone, #setup-system { height: 1fr; min-height: 5; }
    #setup-message { height: 2; color: $text-muted; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+p", "command('pause')", "Pause"),
        Binding("ctrl+r", "command('resume')", "Resume"),
        Binding("ctrl+x", "command('stop')", "Stop"),
        Binding("ctrl+q", "command('stop')", "Quit", priority=True),
        Binding("ctrl+c", "command('stop')", "Quit", priority=True),
        Binding("f2", "cycle_view", "View"),
        Binding("f3", "open_audio_setup", "Setup"),
        Binding("colon", "focus_command", "Command"),
    ]

    def __init__(self, terminal: LiveTerminal) -> None:
        super().__init__()
        self._terminal = terminal
        self.ready = asyncio.Event()
        self._following_conversation = True
        self._following_guidance = True
        self._view_mode = _VIEW_SPLIT
        self._refresh_timer: Timer | None = None
        self._setup_open = False
        self._setup_microphone_index: int | None = None
        self._setup_labels: dict[str, tuple[str, ...]] = {}

    @property
    def view_mode(self) -> str:
        """Return the active operator-selected pane arrangement."""
        return self._view_mode

    def compose(self) -> ComposeResult:
        yield Static(id=_STATUS_ID, markup=False)
        yield Static(id=_SOURCES_ID, markup=False)
        with Horizontal(id="main"):
            with VerticalScroll(id=_CONVERSATION_ID):
                yield Static(id=_CONVERSATION_CONTENT_ID, markup=False)
            with VerticalScroll(id=_GUIDANCE_ID):
                yield Static(id="guidance-content", markup=False)
        with Vertical(id=_SETUP_ID):
            yield Static("Audio setup", id="audio-setup-title", markup=False)
            yield Static("Microphone input", markup=False)
            yield OptionList(
                *self._setup_device_labels("microphones"),
                id=_SETUP_MICROPHONE_ID,
                markup=False,
            )
            yield Static("System audio source", markup=False)
            yield OptionList(
                *self._setup_device_labels("system_monitors"),
                id=_SETUP_SYSTEM_ID,
                markup=False,
            )
            yield Static(_SETUP_INITIAL_MESSAGE, id=_SETUP_MESSAGE_ID, markup=False)
        yield Input(placeholder=_CONTROL_PROMPT, id=_COMMAND_ID)
        yield Footer()

    def on_mount(self) -> None:
        if self._terminal.requires_audio_setup:
            self._show_audio_setup()
        else:
            self._show_dashboard()
        self._refresh()
        self._refresh_timer = self.set_interval(
            _UPDATE_INTERVAL_SECONDS,
            self._refresh,
        )
        self.ready.set()

    def on_unmount(self) -> None:
        if self._refresh_timer is not None:
            self._refresh_timer.stop()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        command = event.value.strip()
        event.input.value = ""
        if command:
            self._terminal.submit_control(command)
        self.query_one(f"#{_CONVERSATION_ID}", VerticalScroll).focus()

    def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        """Use only setup-list indexes already validated by audio selection."""
        if not self._setup_open:
            return
        if event.option_list.id == _SETUP_MICROPHONE_ID:
            self._setup_microphone_index = event.option_index
            self.query_one(f"#{_SETUP_SYSTEM_ID}", OptionList).focus()
            return
        if event.option_list.id != _SETUP_SYSTEM_ID:
            return
        if self._setup_microphone_index is None:
            self._set_setup_message(_SETUP_MICROPHONE_REQUIRED_MESSAGE)
            self.query_one(f"#{_SETUP_MICROPHONE_ID}", OptionList).focus()
            return
        selection = self._terminal.select_audio_setup(
            self._setup_microphone_index,
            event.option_index,
        )
        if selection is None:
            self._set_setup_message(_SETUP_SAVE_FAILED_MESSAGE)
            return
        if self._terminal.requires_audio_setup:
            self._set_setup_message(_SETUP_INITIAL_MESSAGE)
            return
        self._show_dashboard()

    def action_command(self, command: str) -> None:
        if self._setup_open and command == "stop":
            self._terminal.cancel_audio_setup()
            self.exit()
            return
        self._terminal.submit_control(command)

    def action_focus_command(self) -> None:
        self.query_one(f"#{_COMMAND_ID}", Input).focus()

    def action_open_audio_setup(self) -> None:
        self._show_audio_setup()

    def action_cycle_view(self) -> None:
        """Cycle split, conversation, and guidance views without changing content."""
        next_index = (_VIEW_MODES.index(self._view_mode) + 1) % len(_VIEW_MODES)
        self._view_mode = _VIEW_MODES[next_index]
        main = self.query_one("#main", Horizontal)
        main.set_class(
            self._view_mode == _VIEW_CONVERSATION,
            _CONVERSATION_VIEW_CLASS,
        )
        main.set_class(
            self._view_mode == _VIEW_GUIDANCE,
            _GUIDANCE_VIEW_CLASS,
        )
        if self._view_mode == _VIEW_GUIDANCE:
            self.query_one(f"#{_GUIDANCE_ID}", VerticalScroll).focus()
            return
        self.query_one(f"#{_CONVERSATION_ID}", VerticalScroll).focus()

    def _refresh(self) -> None:
        try:
            conversation = self.query_one(
                f"#{_CONVERSATION_ID}",
                VerticalScroll,
            )
            guidance = self.query_one(f"#{_GUIDANCE_ID}", VerticalScroll)
            status = self.query_one(f"#{_STATUS_ID}", Static)
            sources = self.query_one(f"#{_SOURCES_ID}", Static)
            conversation_content = self.query_one(
                f"#{_CONVERSATION_CONTENT_ID}",
                Static,
            )
            guidance_content = self.query_one("#guidance-content", Static)
        except NoMatches:
            return
        was_at_bottom = conversation.scroll_y >= conversation.max_scroll_y
        self._following_conversation = was_at_bottom
        self._following_guidance = guidance.scroll_y >= guidance.max_scroll_y
        status.update(
            self._terminal.status_text(),
            layout=False,
        )
        sources.update(
            self._terminal.sources_text(),
            layout=False,
        )
        conversation_content.update(
            self._terminal.conversation_text(),
        )
        guidance_content.update(
            self._terminal.guidance_text(),
        )
        if self._setup_open:
            self._refresh_setup_device_labels()
        if self._following_conversation:
            conversation.scroll_end(animate=False)
        if self._following_guidance:
            guidance.scroll_end(animate=False)

    def _show_audio_setup(self) -> None:
        self._setup_open = True
        self.query_one(f"#{_STATUS_ID}", Static).display = False
        self.query_one(f"#{_SOURCES_ID}", Static).display = False
        self.query_one("#main", Horizontal).display = False
        self.query_one(f"#{_COMMAND_ID}", Input).display = False
        setup = self.query_one(f"#{_SETUP_ID}", Vertical)
        setup.display = True
        selection = self._terminal.current_audio_selection
        self._setup_microphone_index = _audio_device_index(
            self._setup_microphones(),
            None if selection is None else selection.mic_node,
        )
        microphone_list = self.query_one(f"#{_SETUP_MICROPHONE_ID}", OptionList)
        system_list = self.query_one(f"#{_SETUP_SYSTEM_ID}", OptionList)
        microphone_list.highlighted = (
            0 if self._setup_microphone_index is None else self._setup_microphone_index
        )
        system_index = _audio_device_index(
            self._setup_system_monitors(),
            None if selection is None else selection.system_node,
        )
        system_list.highlighted = 0 if system_index is None else system_index
        self._terminal.start_setup_audio_metering(
            self._setup_microphones(),
            self._setup_system_monitors(),
        )
        self._refresh_setup_device_labels()
        self._set_setup_message(_SETUP_INITIAL_MESSAGE)
        microphone_list.focus()

    def _show_dashboard(self) -> None:
        self._setup_open = False
        self._terminal.cancel_setup_audio_preview()
        self.query_one(f"#{_SETUP_ID}", Vertical).display = False
        self.query_one(f"#{_STATUS_ID}", Static).display = True
        self.query_one(f"#{_SOURCES_ID}", Static).display = True
        self.query_one("#main", Horizontal).display = True
        self.query_one(f"#{_COMMAND_ID}", Input).display = True
        self.query_one(f"#{_CONVERSATION_ID}", VerticalScroll).focus()

    def _set_setup_message(self, message: str) -> None:
        self.query_one(f"#{_SETUP_MESSAGE_ID}", Static).update(message)

    def _setup_device_labels(self, attribute: str) -> tuple[str, ...]:
        setup = self._terminal.audio_setup
        if setup is None:
            return ()
        devices = (
            setup.microphones if attribute == "microphones" else setup.system_monitors
        )
        speaker_role = _USER_ROLE if attribute == "microphones" else _REMOTE_ROLE
        return tuple(
            self._terminal.setup_audio_device_label(speaker_role, device)
            for device in devices
        )

    def _setup_microphones(self) -> tuple[AudioDevice, ...]:
        setup = self._terminal.audio_setup
        return () if setup is None else setup.microphones

    def _setup_system_monitors(self) -> tuple[AudioDevice, ...]:
        setup = self._terminal.audio_setup
        return () if setup is None else setup.system_monitors

    def _refresh_setup_device_labels(self) -> None:
        self._replace_setup_option_labels(
            _SETUP_MICROPHONE_ID,
            self._setup_device_labels("microphones"),
        )
        self._replace_setup_option_labels(
            _SETUP_SYSTEM_ID,
            self._setup_device_labels("system_monitors"),
        )

    def _replace_setup_option_labels(
        self,
        list_id: str,
        labels: tuple[str, ...],
    ) -> None:
        if self._setup_labels.get(list_id) == labels:
            return
        option_list = self.query_one(f"#{list_id}", OptionList)
        if option_list.option_count != len(labels):
            return
        for index, label in enumerate(labels):
            option_list.replace_option_prompt_at_index(index, label)
        self._setup_labels[list_id] = labels


def _operation_for_state(state: str) -> str:
    if state == "running":
        return _OPERATION_WAITING
    if state == "stopped":
        return _OPERATION_STOPPED
    if state == "paused":
        return "Capture paused"
    return "Preparing audio and models"


def _format_elapsed(seconds: float) -> str:
    whole_seconds = max(0, int(seconds))
    minutes, remainder = divmod(whole_seconds, 60)
    return f"{minutes:02d}:{remainder:02d}"


def _role_style(role: str) -> str:
    return "green" if role == _USER_ROLE else "magenta"


def _text(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _visible_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(value.split())
    return "".join(
        character if character.isprintable() else "�" for character in normalized
    )


def _device_label(label: str | None, fallback: str) -> str:
    return _visible_text(label) or _visible_text(fallback) or _EMPTY_TEXT


def _audio_device_index(
    devices: tuple[AudioDevice, ...],
    selected_identifier: str | None,
) -> int | None:
    if selected_identifier is None:
        return None
    for index, device in enumerate(devices):
        if selected_identifier in {device.node_id, device.name}:
            return index
    return None


def _setup_meter_key(speaker_role: str, node_name: str) -> str:
    return f"{speaker_role}:{node_name}"


def _meter(percent: int) -> str:
    bounded_percent = max(0, min(percent, _PERCENT_MAXIMUM))
    filled = round(_LEVEL_BAR_WIDTH * bounded_percent / _PERCENT_MAXIMUM)
    return (
        f"[{_METER_FILLED_CHARACTER * filled}"
        f"{_METER_EMPTY_CHARACTER * (_LEVEL_BAR_WIDTH - filled)}]"
        f" {bounded_percent:3d}%"
    )
