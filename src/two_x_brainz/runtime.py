"""Live two-stream orchestration for the Docker/PipeWire runtime."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import sys
from collections.abc import AsyncIterable, Mapping
from typing import Protocol
from uuid import uuid4

from two_x_brainz.aigate import AIGateClient
from two_x_brainz.audio_selection import AudioSelection, AudioSelectionSetup
from two_x_brainz.capture import (
    CaptureFrameMonitor,
    CaptureStats,
    InterStreamDriftMonitor,
    InterStreamDriftStats,
    PipeWireSource,
    SilenceTurnSegmenter,
    audio_level_percent,
)
from two_x_brainz.config import Settings
from two_x_brainz.constants import (
    DEFAULT_PROVIDER_CONFIG_FILENAME,
    DEFAULT_WEB_CONSOLE_PORT,
    JSON_RECORD_SCHEMA_VERSION,
)
from two_x_brainz.contracts import (
    ASRStreamStats,
    AudioFrame,
    DraftResult,
    InsightResult,
    SpeakerRole,
    TimelineEntry,
    TranscriptEvent,
)
from two_x_brainz.coordinator import ConversationCoordinator
from two_x_brainz.errors import (
    CaptureError,
    ConfigurationError,
    ProtocolError,
    RemoteServiceError,
)
from two_x_brainz.provider_selection import (
    ProviderAssignment,
    ProviderFlow,
    ProviderSelection,
    ProviderSelectionStore,
)
from two_x_brainz.session_controls import (
    SessionCommand,
    SessionController,
    SessionState,
    parse_session_command,
)
from two_x_brainz.talkies import TalkiesClient, TalkiesStreamConfig
from two_x_brainz.terminal import LiveTerminal
from two_x_brainz.web import WebConsole

_MAX_CONTROL_LINE_BYTES = 1_024
_SESSION_STARTED_ACTION = "started"
_CONTROL_ERROR_KIND = "control_error"
_CONTROL_ERROR_MESSAGE = "use pause, resume, or stop"
_MICROPHONE_STREAM_ID = "microphone"
_SYSTEM_STREAM_ID = "system"
_SESSION_ERROR_KIND = "session_error"
_CAPTURE_UNAVAILABLE_REASON = "capture_unavailable"
_ASR_UNAVAILABLE_REASON = "asr_unavailable"
_ASR_PROTOCOL_ERROR_REASON = "asr_protocol_error"
_ASR_SEGMENT_KIND = "asr_segment"
_ASR_SEGMENT_OPENED = "opened"
_ASR_SEGMENT_STREAM_LABEL = "segment"
_RUNTIME_EVENT_LOG_MESSAGE = "live runtime event"
_PROVIDER_ACTIVITY_KIND = "provider_activity"
_PROVIDER_STREAM_EVENT_LOG_MESSAGE = "live provider stream event"
_PROVIDER_STREAMING_PHASES = frozenset({"output_streaming", "reasoning_streaming"})
_AUDIO_CHANNEL_KIND = "audio_channel"
_AUDIO_CHANNEL_RETRY_SECONDS = 1.0

logger = logging.getLogger(__name__)


class LivePresentation(Protocol):
    """The shared runtime adapter contract for a local operator presentation."""

    @property
    def interactive(self) -> bool: ...

    async def open(self) -> AudioSelection | None: ...

    async def close(self) -> None: ...

    def consume(self, record: dict[str, object]) -> None: ...

    def set_audio_level(self, speaker_role: str, percent: int) -> None: ...

    def control_lines(self) -> AsyncIterable[str]: ...


_ACTIVE_TERMINAL: contextvars.ContextVar[LivePresentation | None] = (
    contextvars.ContextVar(
        "active_terminal",
        default=None,
    )
)


async def run_live(
    settings: Settings,
    audio_setup: AudioSelectionSetup,
    *,
    web_port: int = DEFAULT_WEB_CONSOLE_PORT,
) -> None:
    """Serve the web console and start resilient capture only on operator request."""
    terminal_state = LiveTerminal(
        log_file=str(settings.log_file), audio_setup=audio_setup
    )
    terminal = WebConsole(terminal_state, port=web_port)
    terminal_token: contextvars.Token[LivePresentation | None] | None = None
    try:
        await terminal.open()
        inventory_client = AIGateClient(
            base_url=settings.aigate_url,
            model=settings.aigate_model,
            token=settings.aigate_token,
        )
        models = await inventory_client.list_models()
        provider_store = ProviderSelectionStore(
            settings.audio_config_file.with_name(DEFAULT_PROVIDER_CONFIG_FILENAME)
        )
        saved_provider = provider_store.load()
        selection = _initial_provider_selection(settings, saved_provider, models)
        providers = {
            flow: _aigate_client(
                settings,
                selection.assignment(flow),
                web_research_enabled=(flow is ProviderFlow.DRAFT),
            )
            for flow in ProviderFlow
        }
        selection_lock = asyncio.Lock()

        async def configure_provider(
            flow: ProviderFlow,
            model: str,
            reasoning_effort: str,
        ) -> bool:
            nonlocal selection
            if model not in models:
                raise RemoteServiceError("selected AIGate model is unavailable")
            assignment = ProviderAssignment(
                model=model,
                reasoning_effort=reasoning_effort,
            )
            async with selection_lock:
                updated_selection = selection.replace(flow, assignment)
                try:
                    provider_store.save(updated_selection)
                except ConfigurationError:
                    logger.warning(
                        "provider settings were not persisted",
                        extra={
                            "reason": "provider_settings_persistence_failed",
                            "output_kind": flow.value,
                        },
                        exc_info=True,
                    )
                    _write_provider_activity_event(
                        {
                            "phase": "settings_failed",
                            "output_kind": flow.value,
                            "model": model,
                            "reasoning_effort": reasoning_effort,
                        }
                    )
                    return False
                providers[flow].configure(model, reasoning_effort)
                selection = updated_selection
            _write_provider_activity_event(
                {
                    "phase": "settings_changed",
                    "output_kind": flow.value,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                }
            )
            return True

        terminal.configure_provider(
            models=models,
            selection=selection,
            callback=configure_provider,
        )
        coordinator = ConversationCoordinator(
            draft_provider=providers[ProviderFlow.DRAFT],
            commentary_provider=providers[ProviderFlow.COMMENTARY],
            summary_provider=providers[ProviderFlow.SUMMARY],
        )
        controller = SessionController(start_paused=True)
        session_id = str(uuid4())
        stream_config = TalkiesStreamConfig(
            url=settings.talkies_ws_url,
            model=settings.talkies_model,
            token=settings.talkies_token,
        )
        talkies_client = TalkiesClient(stream_config)
        await talkies_client.verify_configured_model()
        await talkies_client.warm_configured_model()
        drift_monitor = InterStreamDriftMonitor()
        terminal_token = _ACTIVE_TERMINAL.set(terminal)
        renderer_context = contextvars.copy_context()
        renderer = renderer_context.run(
            asyncio.create_task,
            _render_drafts(coordinator),
        )
        insight_renderer_context = contextvars.copy_context()
        insight_renderer = insight_renderer_context.run(
            asyncio.create_task,
            _render_insights(coordinator),
        )
        try:
            _write_session_event(
                state=controller.state,
                action=_SESSION_STARTED_ACTION,
                changed=True,
                aigate_model=selection.draft.model,
            )
            async with asyncio.TaskGroup() as group:
                group.create_task(
                    _supervise_audio_channel(
                        stream_config=stream_config,
                        coordinator=coordinator,
                        session_id=session_id,
                        stream_id=_MICROPHONE_STREAM_ID,
                        speaker_role=SpeakerRole.USER,
                        audio_setup=audio_setup,
                        controller=controller,
                        drift_monitor=drift_monitor,
                    ),
                    context=contextvars.copy_context(),
                )
                group.create_task(
                    _supervise_audio_channel(
                        stream_config=stream_config,
                        coordinator=coordinator,
                        session_id=session_id,
                        stream_id=_SYSTEM_STREAM_ID,
                        speaker_role=SpeakerRole.REMOTE,
                        audio_setup=audio_setup,
                        controller=controller,
                        drift_monitor=drift_monitor,
                    ),
                    context=contextvars.copy_context(),
                )
                group.create_task(_read_session_controls(controller, coordinator))
        finally:
            controller.stop()
            await coordinator.stop()
            write_capture_drift_event(drift_monitor.stats())
            renderer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renderer
            insight_renderer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await insight_renderer
    finally:
        if terminal_token is not None:
            _ACTIVE_TERMINAL.reset(terminal_token)
        await terminal.close()


def _initial_provider_selection(
    settings: Settings,
    saved_selection: ProviderSelection | None,
    available_models: tuple[str, ...],
) -> ProviderSelection:
    if saved_selection is not None and all(
        model in available_models for model in saved_selection.models()
    ):
        return saved_selection
    configured_models = (
        settings.aigate_reply_model or settings.aigate_model,
        settings.aigate_coach_model or settings.aigate_model,
        settings.aigate_summary_model or settings.aigate_model,
    )
    if any(model is None for model in configured_models):
        raise ConfigurationError(
            "configure all three AIGate flow models or TWOXBRAINZ_AIGATE_MODEL"
        )
    reply_model, coach_model, summary_model = configured_models
    assert reply_model is not None
    assert coach_model is not None
    assert summary_model is not None
    if any(model not in available_models for model in configured_models):
        raise RemoteServiceError(
            "a configured AIGate flow model is not available from the current inventory"
        )
    return ProviderSelection(
        draft=ProviderAssignment(
            reply_model,
            settings.aigate_reply_reasoning_effort or settings.aigate_reasoning_effort,
        ),
        commentary=ProviderAssignment(
            coach_model,
            settings.aigate_coach_reasoning_effort or settings.aigate_reasoning_effort,
        ),
        summary=ProviderAssignment(
            summary_model,
            settings.aigate_summary_reasoning_effort
            or settings.aigate_reasoning_effort,
        ),
    )


def _aigate_client(
    settings: Settings,
    assignment: ProviderAssignment,
    *,
    web_research_enabled: bool,
) -> AIGateClient:
    return AIGateClient(
        base_url=settings.aigate_url,
        model=assignment.model,
        token=settings.aigate_token,
        web_research_enabled=(web_research_enabled and settings.web_research_enabled),
        session_brief=settings.session_brief,
        reasoning_effort=assignment.reasoning_effort,
        activity_sink=_write_provider_activity_event,
        streaming_enabled=True,
    )


async def _gated_frames(
    frames: AsyncIterable[bytes],
    controller: SessionController,
) -> AsyncIterable[bytes]:
    """Do not even open capture until the operator enables forwarding."""
    iterator = aiter(frames)
    while await controller.wait_for_forwarding():
        try:
            frame = await anext(iterator)
        except StopAsyncIteration:
            return
        if controller.state is not SessionState.RUNNING:
            continue
        yield frame


async def _supervise_audio_channel(
    *,
    stream_config: TalkiesStreamConfig,
    coordinator: ConversationCoordinator,
    session_id: str,
    stream_id: str,
    speaker_role: SpeakerRole,
    audio_setup: AudioSelectionSetup,
    controller: SessionController,
    drift_monitor: InterStreamDriftMonitor,
) -> None:
    """Restart one failed or rerouted channel without disturbing its peer."""
    attempt = 0
    while controller.state is not SessionState.STOPPED:
        selection = audio_setup.selection
        revision = audio_setup.revision
        if selection is None:
            _write_audio_channel_event(speaker_role, "waiting_for_device")
            await audio_setup.wait_for_change(revision)
            continue
        attempt += 1
        node_name = (
            selection.mic_node
            if speaker_role is SpeakerRole.USER
            else selection.system_node
        )
        capture_sink = (
            False if speaker_role is SpeakerRole.USER else selection.system_capture_sink
        )
        attempt_stream_id = f"{stream_id}:route:{revision}:attempt:{attempt}"
        capture_monitor = CaptureFrameMonitor(
            session_id=session_id,
            stream_id=attempt_stream_id,
            speaker_role=speaker_role,
            drift_monitor=drift_monitor,
        )
        _write_audio_channel_event(speaker_role, "ready")
        capture_task = asyncio.create_task(
            _consume_stream(
                client=TalkiesClient(stream_config),
                coordinator=coordinator,
                session_id=session_id,
                stream_id=attempt_stream_id,
                speaker_role=speaker_role,
                frames=_metered_frames(
                    capture_monitor.annotate(
                        _gated_frames(
                            PipeWireSource(
                                node_name,
                                capture_sink=capture_sink,
                            ).frames(),
                            controller,
                        )
                    )
                ),
                capture_monitor=capture_monitor,
            ),
            name=f"capture-{speaker_role.value}-{attempt}",
        )
        route_task = asyncio.create_task(
            audio_setup.wait_for_change(revision),
            name=f"capture-route-{speaker_role.value}-{attempt}",
        )
        done, pending = await asyncio.wait(
            {capture_task, route_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if route_task in done:
            _write_audio_channel_event(speaker_role, "switching")
            continue
        try:
            await capture_task
        except asyncio.CancelledError:
            raise
        except (CaptureError, ProtocolError, RemoteServiceError):
            _write_audio_channel_event(speaker_role, "reconnecting")
            terminal = _ACTIVE_TERMINAL.get()
            if terminal is not None:
                terminal.set_audio_level(speaker_role.value, 0)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    audio_setup.wait_for_change(revision),
                    timeout=_AUDIO_CHANNEL_RETRY_SECONDS,
                )
            continue
        _write_audio_channel_event(speaker_role, "reconnecting")


def _write_audio_channel_event(speaker_role: SpeakerRole, state: str) -> None:
    _emit_event(
        {
            "schema_version": JSON_RECORD_SCHEMA_VERSION,
            "kind": _AUDIO_CHANNEL_KIND,
            "speaker_role": speaker_role.value,
            "state": state,
        }
    )


def _write_provider_activity_event(activity: Mapping[str, object]) -> None:
    record = {
        "schema_version": JSON_RECORD_SCHEMA_VERSION,
        "kind": _PROVIDER_ACTIVITY_KIND,
        **activity,
    }
    _emit_event(record)


async def _metered_frames(
    frames: AsyncIterable[AudioFrame],
) -> AsyncIterable[AudioFrame]:
    """Update the active local console from derived frame amplitude only."""
    async for frame in frames:
        terminal = _ACTIVE_TERMINAL.get()
        if terminal is not None:
            terminal.set_audio_level(
                frame.speaker_role.value,
                audio_level_percent(frame.samples),
            )
        yield frame


async def _read_session_controls(
    controller: SessionController,
    coordinator: ConversationCoordinator,
) -> None:
    """Read bounded local control lines without echoing untrusted input."""
    terminal = _ACTIVE_TERMINAL.get()
    if terminal is not None and terminal.interactive:
        async for line in terminal.control_lines():
            if await handle_control_line(line, controller, coordinator):
                return
        return

    reader = asyncio.StreamReader(limit=_MAX_CONTROL_LINE_BYTES)
    protocol = asyncio.StreamReaderProtocol(reader)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    try:
        while controller.state is not SessionState.STOPPED:
            try:
                raw_line = await reader.readline()
            except ValueError:
                _write_control_error()
                continue
            if not raw_line:
                return
            line = raw_line.decode("utf-8", errors="replace")
            if await handle_control_line(line, controller, coordinator):
                return
    finally:
        transport.close()


async def handle_control_line(
    line: str,
    controller: SessionController,
    coordinator: ConversationCoordinator,
) -> bool:
    """Apply one trusted-local console line through the existing strict parser."""
    command = parse_session_command(line)
    if command is not None:
        changed = _apply_session_command(command, controller)
        if changed and command is not SessionCommand.RESUME:
            await coordinator.stop()
        _write_session_event(
            state=controller.state,
            action=command.value,
            changed=changed,
        )
        return command is SessionCommand.STOP
    _write_control_error()
    return False


def _apply_session_command(
    command: SessionCommand,
    controller: SessionController,
) -> bool:
    """Apply the exact parsed command without exposing an arbitrary action path."""
    if command is SessionCommand.PAUSE:
        return controller.pause()
    if command is SessionCommand.RESUME:
        return controller.resume()
    return controller.stop()


def _emit_event(record: dict[str, object]) -> None:
    """Log every runtime event and route it to the active human-facing terminal."""
    if (
        record.get("kind") == _PROVIDER_ACTIVITY_KIND
        and record.get("phase") in _PROVIDER_STREAMING_PHASES
    ):
        reasoning = record.get("reasoning")
        output = record.get("output")
        logger.debug(
            _PROVIDER_STREAM_EVENT_LOG_MESSAGE,
            extra={
                "phase": record.get("phase"),
                "flow_id": record.get("flow_id"),
                "output_kind": record.get("output_kind"),
                "model": record.get("model"),
                "reasoning_characters": len(reasoning)
                if isinstance(reasoning, str)
                else 0,
                "output_characters": len(output) if isinstance(output, str) else 0,
            },
        )
    else:
        logger.info(_RUNTIME_EVENT_LOG_MESSAGE, extra={"event": record})
    terminal = _ACTIVE_TERMINAL.get()
    if terminal is not None:
        terminal.consume(record)
        return
    print(json.dumps(record, separators=(",", ":")))


def _write_session_event(
    *,
    state: SessionState,
    action: str,
    changed: bool,
    aigate_model: str | None = None,
) -> None:
    record: dict[str, object] = {
        "schema_version": JSON_RECORD_SCHEMA_VERSION,
        "kind": "session",
        "state": state.value,
        "action": action,
        "changed": changed,
    }
    if aigate_model is not None:
        record["aigate_model"] = aigate_model
    _emit_event(record)


def _write_control_error() -> None:
    _emit_event(
        {
            "schema_version": JSON_RECORD_SCHEMA_VERSION,
            "kind": _CONTROL_ERROR_KIND,
            "message": _CONTROL_ERROR_MESSAGE,
        }
    )


def write_session_error_event(reason: str) -> None:
    """Emit a fixed degraded-state record without upstream exception details."""
    _emit_event(
        {
            "schema_version": JSON_RECORD_SCHEMA_VERSION,
            "kind": _SESSION_ERROR_KIND,
            "reason": reason,
        }
    )


def session_error_reason(error: Exception) -> str:
    if isinstance(error, CaptureError):
        return _CAPTURE_UNAVAILABLE_REASON
    if isinstance(error, ProtocolError):
        return _ASR_PROTOCOL_ERROR_REASON
    return _ASR_UNAVAILABLE_REASON


async def _consume_stream(
    *,
    client: TalkiesClient,
    coordinator: ConversationCoordinator,
    session_id: str,
    stream_id: str,
    speaker_role: SpeakerRole,
    frames: AsyncIterable[AudioFrame],
    capture_monitor: CaptureFrameMonitor,
) -> None:
    segmenter = SilenceTurnSegmenter(frames)
    segment_index = 0
    try:
        while not segmenter.capture_ended:
            first_speech_frame = await segmenter.next_speech_frame()
            if first_speech_frame is None:
                break
            segment_index += 1
            segment_stream_id = asr_segment_stream_id(stream_id, segment_index)
            write_asr_segment_event(
                speaker_role,
                segment_index,
                _ASR_SEGMENT_OPENED,
            )
            async for event in client.transcribe(
                session_id=session_id,
                stream_id=segment_stream_id,
                speaker_role=speaker_role,
                frames=segmenter.next_segment(first_speech_frame),
            ):
                if isinstance(event, ASRStreamStats):
                    write_asr_stats_event(event)
                    continue
                update = await coordinator.ingest(event)
                _write_transcript_event(event)
                if update.turn is not None:
                    _emit_event(
                        {
                            "schema_version": JSON_RECORD_SCHEMA_VERSION,
                            "kind": "turn",
                            "turn_id": update.turn.turn_id,
                            "speaker_role": update.turn.speaker_role.value,
                            "state": update.turn.state.value,
                            "transcript_revision": update.turn.transcript_revision,
                        }
                    )
                if update.timeline is not None:
                    _write_timeline_event(update.timeline)
            boundary = segmenter.last_boundary
            if boundary is None:
                raise ProtocolError("ASR segment ended without a capture boundary")
            write_asr_segment_event(speaker_role, segment_index, boundary)
    finally:
        write_capture_stats_event(capture_monitor.stats())


def asr_segment_stream_id(capture_stream_id: str, segment_index: int) -> str:
    """Give a rotated ASR connection a new coordinator turn identity."""
    return f"{capture_stream_id}:{_ASR_SEGMENT_STREAM_LABEL}:{segment_index}"


def write_asr_segment_event(
    speaker_role: SpeakerRole,
    segment_index: int,
    boundary: str,
) -> None:
    """Emit ASR transport rotation boundaries for reconstructable live traces."""
    _emit_event(
        {
            "schema_version": JSON_RECORD_SCHEMA_VERSION,
            "kind": _ASR_SEGMENT_KIND,
            "speaker_role": speaker_role.value,
            "segment_index": segment_index,
            "boundary": boundary,
        }
    )


async def _render_drafts(coordinator: ConversationCoordinator) -> None:
    """Render completed drafts without stopping either ongoing ASR stream."""
    while True:
        write_draft_event(await coordinator.next_draft_event())


async def _render_insights(coordinator: ConversationCoordinator) -> None:
    """Render lower-priority commentary and summary results in the terminal."""
    while True:
        _write_insight_event(await coordinator.next_completed_insight())


def _write_transcript_event(event: TranscriptEvent) -> None:
    _emit_event(transcript_record(event))


def transcript_record(event: TranscriptEvent) -> dict[str, object]:
    """Return the shared privacy-safe JSON record for one transcript event."""
    return {
        "schema_version": JSON_RECORD_SCHEMA_VERSION,
        "kind": "transcript",
        "speaker_role": event.speaker_role.value,
        "type": event.source_event_type.value,
        "revision": event.revision,
        "asr_model": event.asr_model,
        "started_at_ms": event.started_at_ms,
        "ended_at_ms": event.ended_at_ms,
        "text": event.text,
        "is_final": event.is_final,
        "confidence": event.confidence,
        "language": event.language,
        "words": [
            {
                "word": word.word,
                "start_ms": word.start_ms,
                "end_ms": word.end_ms,
                "confidence": word.confidence,
            }
            for word in event.words
        ],
        "audio_seconds": event.audio_seconds,
    }


def write_asr_stats_event(event: ASRStreamStats) -> None:
    _emit_event(
        {
            "schema_version": JSON_RECORD_SCHEMA_VERSION,
            "kind": "asr_stats",
            "speaker_role": event.speaker_role.value,
            "model": event.asr_model,
            "audio_seconds": event.audio_seconds,
            "frames": event.frames,
            "canceled": event.canceled,
        }
    )


def write_capture_stats_event(stats: CaptureStats) -> None:
    """Emit bounded capture timing diagnostics without sensitive stream identity."""
    _emit_event(
        {
            "schema_version": JSON_RECORD_SCHEMA_VERSION,
            "kind": "capture_stats",
            "speaker_role": stats.speaker_role.value,
            "frame_count": stats.frame_count,
            "audio_seconds": stats.audio_seconds,
            "gap_count": stats.gap_count,
            "max_gap_seconds": stats.max_gap_seconds,
        }
    )


def write_capture_drift_event(stats: InterStreamDriftStats) -> None:
    """Emit aggregate relative timing without frame payloads or device identity."""
    _emit_event(
        {
            "schema_version": JSON_RECORD_SCHEMA_VERSION,
            "kind": "capture_drift",
            "comparison_count": stats.comparison_count,
            "max_abs_drift_seconds": stats.max_abs_drift_seconds,
            "unmatched_frame_count": stats.unmatched_frame_count,
        }
    )


def write_draft_event(draft: DraftResult) -> None:
    _emit_event(
        {
            "schema_version": JSON_RECORD_SCHEMA_VERSION,
            "kind": "draft",
            "generation_id": draft.generation_id,
            "trigger_turn_id": draft.trigger_turn_id,
            "status": draft.status.value,
            "text": draft.text,
            "context_revision": draft.context_revision,
        }
    )


def _write_insight_event(insight: InsightResult) -> None:
    _emit_event(
        {
            "schema_version": JSON_RECORD_SCHEMA_VERSION,
            "kind": insight.kind.value,
            "generation_id": insight.generation_id,
            "trigger_turn_id": insight.trigger_turn_id,
            "status": insight.status.value,
            "text": insight.text,
            "context_revision": insight.context_revision,
        }
    )


def _write_timeline_event(entry: TimelineEntry) -> None:
    _emit_event(
        {
            "schema_version": JSON_RECORD_SCHEMA_VERSION,
            "kind": "timeline",
            "turn_id": entry.turn_id,
            "speaker_role": entry.speaker_role.value,
            "transcript_revision": entry.transcript_revision,
            "text": entry.text,
        }
    )
