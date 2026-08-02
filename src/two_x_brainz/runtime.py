"""Live two-stream orchestration for the Docker/PipeWire runtime."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import sys
from collections.abc import AsyncIterable
from uuid import uuid4

from two_x_brainz.aigate import AIGateClient
from two_x_brainz.capture import (
    CaptureFrameMonitor,
    CaptureStats,
    InterStreamDriftMonitor,
    InterStreamDriftStats,
    PipeWireSource,
    SilenceTurnSegmenter,
)
from two_x_brainz.config import Settings
from two_x_brainz.constants import JSON_RECORD_SCHEMA_VERSION
from two_x_brainz.contracts import (
    ASRStreamStats,
    AudioFrame,
    DraftOutcome,
    DraftOutcomeAction,
    DraftResult,
    InsightResult,
    SpeakerRole,
    TimelineEntry,
    TranscriptEvent,
)
from two_x_brainz.coordinator import ConversationCoordinator
from two_x_brainz.errors import CaptureError, ProtocolError, RemoteServiceError
from two_x_brainz.session_controls import (
    DraftAction,
    DraftActionRequest,
    SessionCommand,
    SessionController,
    SessionState,
    parse_draft_action,
    parse_session_command,
)
from two_x_brainz.talkies import TalkiesClient, TalkiesStreamConfig

_MAX_CONTROL_LINE_BYTES = 1_024
_SESSION_STARTED_ACTION = "started"
_CONTROL_ERROR_KIND = "control_error"
_CONTROL_ERROR_MESSAGE = "use pause, resume, stop, accept, dismiss, edit, or regenerate"
_MICROPHONE_STREAM_ID = "microphone"
_SYSTEM_STREAM_ID = "system"
_SESSION_ERROR_KIND = "session_error"
_CAPTURE_UNAVAILABLE_REASON = "capture_unavailable"
_ASR_UNAVAILABLE_REASON = "asr_unavailable"
_ASR_PROTOCOL_ERROR_REASON = "asr_protocol_error"
_ASR_SEGMENT_KIND = "asr_segment"
_ASR_SEGMENT_OPENED = "opened"
_ASR_SEGMENT_STREAM_LABEL = "segment"


class _LiveSessionStopped(Exception):
    """Ends the TaskGroup after the local operator explicitly stops capture."""


async def run_live(settings: Settings, mic_node: str, system_node: str) -> None:
    """Capture mic/system nodes and print live transcript events to stdout."""
    provider = AIGateClient(
        base_url=settings.aigate_url,
        model=settings.aigate_model,
        token=settings.aigate_token,
    )
    await provider.verify_configured_model()
    coordinator = ConversationCoordinator(provider, provider)
    controller = SessionController()
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
    microphone_monitor = CaptureFrameMonitor(
        session_id=session_id,
        stream_id=_MICROPHONE_STREAM_ID,
        speaker_role=SpeakerRole.USER,
        drift_monitor=drift_monitor,
    )
    system_monitor = CaptureFrameMonitor(
        session_id=session_id,
        stream_id=_SYSTEM_STREAM_ID,
        speaker_role=SpeakerRole.REMOTE,
        drift_monitor=drift_monitor,
    )
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
    _write_session_event(
        state=controller.state,
        action=_SESSION_STARTED_ACTION,
        changed=True,
        aigate_mode=settings.aigate_mode.value,
        aigate_model=settings.aigate_model,
    )

    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(
                _consume_stream(
                    client=TalkiesClient(stream_config),
                    coordinator=coordinator,
                    session_id=session_id,
                    stream_id=_MICROPHONE_STREAM_ID,
                    speaker_role=SpeakerRole.USER,
                    frames=microphone_monitor.annotate(
                        _gated_frames(
                            PipeWireSource(mic_node).frames(),
                            controller,
                        )
                    ),
                    capture_monitor=microphone_monitor,
                ),
                context=contextvars.copy_context(),
            )
            group.create_task(
                _consume_stream(
                    client=TalkiesClient(stream_config),
                    coordinator=coordinator,
                    session_id=session_id,
                    stream_id=_SYSTEM_STREAM_ID,
                    speaker_role=SpeakerRole.REMOTE,
                    frames=system_monitor.annotate(
                        _gated_frames(
                            PipeWireSource(system_node).frames(),
                            controller,
                        )
                    ),
                    capture_monitor=system_monitor,
                ),
                context=contextvars.copy_context(),
            )
            group.create_task(_read_session_controls(controller, coordinator))
            group.create_task(_raise_when_stopped(controller))
    except* _LiveSessionStopped:
        pass
    except* (CaptureError, ProtocolError, RemoteServiceError) as error_group:
        write_session_error_event(
            session_error_reason(_first_expected_live_error(error_group))
        )
        raise
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


async def _gated_frames(
    frames: AsyncIterable[bytes],
    controller: SessionController,
) -> AsyncIterable[bytes]:
    """Keep unapproved paused frames out of the Talkies transport."""
    async for frame in frames:
        if not await controller.wait_for_forwarding():
            return
        yield frame


async def _read_session_controls(
    controller: SessionController,
    coordinator: ConversationCoordinator,
) -> None:
    """Read bounded local control lines without echoing untrusted input."""
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
            command = parse_session_command(raw_line.decode("utf-8", errors="replace"))
            if command is not None:
                changed = _apply_session_command(command, controller)
                if changed and command is not SessionCommand.RESUME:
                    await coordinator.stop()
                _write_session_event(
                    state=controller.state,
                    action=command.value,
                    changed=changed,
                )
                if command is SessionCommand.STOP:
                    return
                continue
            action_request = parse_draft_action(
                raw_line.decode("utf-8", errors="replace")
            )
            if action_request is None:
                _write_control_error()
                continue
            changed, outcome = await _apply_draft_action(action_request, coordinator)
            write_draft_action_event(action_request.action, changed, outcome)
    finally:
        transport.close()


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


async def _apply_draft_action(
    request: DraftActionRequest,
    coordinator: ConversationCoordinator,
) -> tuple[bool, DraftOutcome | None]:
    """Apply a parsed human-gate action through the coordinator boundary."""
    if request.action is DraftAction.ACCEPT:
        outcome = coordinator.record_draft_outcome(DraftOutcomeAction.ACCEPTED)
        return outcome is not None, outcome
    if request.action is DraftAction.DISMISS:
        outcome = coordinator.record_draft_outcome(DraftOutcomeAction.DISMISSED)
        return outcome is not None, outcome
    if request.action is DraftAction.EDIT:
        if request.text is None:
            return False, None
        return coordinator.edit_current_draft(request.text) is not None, None
    return await coordinator.regenerate_current_draft(), None


async def _raise_when_stopped(controller: SessionController) -> None:
    """Cancel capture streams once stop is requested through the control channel."""
    await controller.wait_for_stop()
    raise _LiveSessionStopped()


def _write_session_event(
    *,
    state: SessionState,
    action: str,
    changed: bool,
    aigate_mode: str | None = None,
    aigate_model: str | None = None,
) -> None:
    record: dict[str, object] = {
        "schema_version": JSON_RECORD_SCHEMA_VERSION,
        "kind": "session",
        "state": state.value,
        "action": action,
        "changed": changed,
    }
    if aigate_mode is not None:
        record["aigate_mode"] = aigate_mode
    if aigate_model is not None:
        record["aigate_model"] = aigate_model
    print(
        json.dumps(
            record,
            separators=(",", ":"),
        )
    )


def _write_control_error() -> None:
    print(
        json.dumps(
            {
                "schema_version": JSON_RECORD_SCHEMA_VERSION,
                "kind": _CONTROL_ERROR_KIND,
                "message": _CONTROL_ERROR_MESSAGE,
            },
            separators=(",", ":"),
        )
    )


def write_session_error_event(reason: str) -> None:
    """Emit a fixed degraded-state record without upstream exception details."""
    print(
        json.dumps(
            {
                "schema_version": JSON_RECORD_SCHEMA_VERSION,
                "kind": _SESSION_ERROR_KIND,
                "reason": reason,
            },
            separators=(",", ":"),
        )
    )


def session_error_reason(error: Exception) -> str:
    if isinstance(error, CaptureError):
        return _CAPTURE_UNAVAILABLE_REASON
    if isinstance(error, ProtocolError):
        return _ASR_PROTOCOL_ERROR_REASON
    return _ASR_UNAVAILABLE_REASON


def _first_expected_live_error(
    error_group: BaseExceptionGroup[Exception],
) -> CaptureError | ProtocolError | RemoteServiceError:
    """Return one typed stream error from a TaskGroup exception group."""
    for error in error_group.exceptions:
        if isinstance(error, CaptureError | ProtocolError | RemoteServiceError):
            return error
    raise RuntimeError("expected a typed live stream error")


def write_draft_action_event(
    action: DraftAction,
    changed: bool,
    outcome: DraftOutcome | None,
) -> None:
    print(
        json.dumps(
            {
                "schema_version": JSON_RECORD_SCHEMA_VERSION,
                "kind": "draft_action",
                "action": action.value,
                "changed": changed,
                "outcome": outcome.action.value if outcome is not None else None,
                "generation_id": (
                    outcome.draft.generation_id if outcome is not None else None
                ),
                "trigger_turn_id": (
                    outcome.draft.trigger_turn_id if outcome is not None else None
                ),
                "context_revision": (
                    outcome.draft.context_revision if outcome is not None else None
                ),
            },
            separators=(",", ":"),
        )
    )


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
                    print(
                        json.dumps(
                            {
                                "schema_version": JSON_RECORD_SCHEMA_VERSION,
                                "kind": "turn",
                                "turn_id": update.turn.turn_id,
                                "speaker_role": update.turn.speaker_role.value,
                                "state": update.turn.state.value,
                                "transcript_revision": update.turn.transcript_revision,
                            },
                            separators=(",", ":"),
                        )
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
    print(
        json.dumps(
            {
                "schema_version": JSON_RECORD_SCHEMA_VERSION,
                "kind": _ASR_SEGMENT_KIND,
                "speaker_role": speaker_role.value,
                "segment_index": segment_index,
                "boundary": boundary,
            },
            separators=(",", ":"),
        )
    )


async def _render_drafts(coordinator: ConversationCoordinator) -> None:
    """Render completed drafts without stopping either ongoing ASR stream."""
    while True:
        write_draft_event(await coordinator.next_draft_event())


async def _render_insights(coordinator: ConversationCoordinator) -> None:
    """Render lower-priority commentary and summary results as JSON records."""
    while True:
        _write_insight_event(await coordinator.next_completed_insight())


def _write_transcript_event(event: TranscriptEvent) -> None:
    print(
        json.dumps(
            transcript_record(event),
            separators=(",", ":"),
        )
    )


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
    print(
        json.dumps(
            {
                "schema_version": JSON_RECORD_SCHEMA_VERSION,
                "kind": "asr_stats",
                "speaker_role": event.speaker_role.value,
                "model": event.asr_model,
                "audio_seconds": event.audio_seconds,
                "frames": event.frames,
                "canceled": event.canceled,
            },
            separators=(",", ":"),
        )
    )


def write_capture_stats_event(stats: CaptureStats) -> None:
    """Emit bounded capture timing diagnostics without sensitive stream identity."""
    print(
        json.dumps(
            {
                "schema_version": JSON_RECORD_SCHEMA_VERSION,
                "kind": "capture_stats",
                "speaker_role": stats.speaker_role.value,
                "frame_count": stats.frame_count,
                "audio_seconds": stats.audio_seconds,
                "gap_count": stats.gap_count,
                "max_gap_seconds": stats.max_gap_seconds,
            },
            separators=(",", ":"),
        )
    )


def write_capture_drift_event(stats: InterStreamDriftStats) -> None:
    """Emit aggregate relative timing without frame payloads or device identity."""
    print(
        json.dumps(
            {
                "schema_version": JSON_RECORD_SCHEMA_VERSION,
                "kind": "capture_drift",
                "comparison_count": stats.comparison_count,
                "max_abs_drift_seconds": stats.max_abs_drift_seconds,
                "unmatched_frame_count": stats.unmatched_frame_count,
            },
            separators=(",", ":"),
        )
    )


def write_draft_event(draft: DraftResult) -> None:
    print(
        json.dumps(
            {
                "schema_version": JSON_RECORD_SCHEMA_VERSION,
                "kind": "draft",
                "generation_id": draft.generation_id,
                "trigger_turn_id": draft.trigger_turn_id,
                "status": draft.status.value,
                "text": draft.text,
                "context_revision": draft.context_revision,
            },
            separators=(",", ":"),
        )
    )


def _write_insight_event(insight: InsightResult) -> None:
    print(
        json.dumps(
            {
                "schema_version": JSON_RECORD_SCHEMA_VERSION,
                "kind": insight.kind.value,
                "generation_id": insight.generation_id,
                "trigger_turn_id": insight.trigger_turn_id,
                "status": insight.status.value,
                "text": insight.text,
                "context_revision": insight.context_revision,
            },
            separators=(",", ":"),
        )
    )


def _write_timeline_event(entry: TimelineEntry) -> None:
    print(
        json.dumps(
            {
                "schema_version": JSON_RECORD_SCHEMA_VERSION,
                "kind": "timeline",
                "turn_id": entry.turn_id,
                "speaker_role": entry.speaker_role.value,
                "transcript_revision": entry.transcript_revision,
                "text": entry.text,
            },
            separators=(",", ":"),
        )
    )
