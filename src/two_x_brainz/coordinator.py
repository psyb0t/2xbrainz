"""Session coordinator with cancellation and stale-result protection."""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from two_x_brainz.aigate import DraftProvider, InsightProvider, SessionDraftProvider
from two_x_brainz.claudebox import NO_NEW_RESEARCH_RESULT
from two_x_brainz.constants import (
    DEFAULT_PROVIDER_GENERATION_DEADLINE,
    MAX_DRAFT_RESULT_BACKLOG,
    MAX_FAST_DRAFT_RESULT_BACKLOG,
    MAX_INSIGHT_RESULT_BACKLOG,
)
from two_x_brainz.contracts import (
    DraftRequest,
    DraftResult,
    GenerationStatus,
    InsightKind,
    InsightRequest,
    InsightResult,
    SpeakerRole,
    TimelineEntry,
    TranscriptEvent,
    TranscriptSnapshot,
    TurnEvent,
    TurnState,
)
from two_x_brainz.transcript import TranscriptStore
from two_x_brainz.turns import TurnManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CoordinatorUpdate:
    """Observable state emitted after each transcript event."""

    transcript: TranscriptSnapshot
    turn: TurnEvent | None
    draft: DraftResult | None
    fast_draft: DraftResult | None
    timeline: TimelineEntry | None


class ConversationCoordinator:
    """Owns transcript state and one high-priority advisory reply job."""

    def __init__(
        self,
        draft_provider: DraftProvider,
        commentary_provider: InsightProvider | None = None,
        summary_provider: InsightProvider | None = None,
        research_provider: DraftProvider | None = None,
        generation_deadline: timedelta = DEFAULT_PROVIDER_GENERATION_DEADLINE,
        draft_generation_deadline: timedelta | None = None,
        auto_dispatch_enabled: bool = True,
        research_enabled: bool = True,
        *,
        fast_draft_provider: DraftProvider | None = None,
    ) -> None:
        if generation_deadline <= timedelta():
            raise ValueError("generation_deadline must be positive")
        if (
            draft_generation_deadline is not None
            and draft_generation_deadline <= timedelta()
        ):
            raise ValueError("draft_generation_deadline must be positive")
        self._draft_provider = draft_provider
        self._fast_draft_provider = fast_draft_provider
        self._commentary_provider = commentary_provider
        self._summary_provider = summary_provider or commentary_provider
        self._research_provider = research_provider
        self._generation_deadline_seconds = generation_deadline.total_seconds()
        self._draft_generation_deadline_seconds = (
            draft_generation_deadline or generation_deadline
        ).total_seconds()
        self._transcript = TranscriptStore()
        self._turns = TurnManager()
        self._ingest_lock = asyncio.Lock()
        self._active_task: asyncio.Task[None] | None = None
        self._active_generation_id: str | None = None
        self._active_draft_request: DraftRequest | None = None
        self._last_draft: DraftResult | None = None
        self._completed_drafts: asyncio.Queue[DraftResult] = asyncio.Queue(
            maxsize=MAX_DRAFT_RESULT_BACKLOG
        )
        self._draft_events: asyncio.Queue[DraftResult] = asyncio.Queue(
            maxsize=MAX_DRAFT_RESULT_BACKLOG
        )
        self._active_fast_task: asyncio.Task[None] | None = None
        self._active_fast_generation_id: str | None = None
        self._active_fast_draft_request: DraftRequest | None = None
        self._last_fast_draft: DraftResult | None = None
        self._completed_fast_drafts: asyncio.Queue[DraftResult] = asyncio.Queue(
            maxsize=MAX_FAST_DRAFT_RESULT_BACKLOG
        )
        self._fast_draft_events: asyncio.Queue[DraftResult] = asyncio.Queue(
            maxsize=MAX_FAST_DRAFT_RESULT_BACKLOG
        )
        self._commentary_task: asyncio.Task[None] | None = None
        self._summary_task: asyncio.Task[None] | None = None
        self._research_task: asyncio.Task[None] | None = None
        self._active_research_generation_id: str | None = None
        self._completed_insights: asyncio.Queue[InsightResult] = asyncio.Queue(
            maxsize=MAX_INSIGHT_RESULT_BACKLOG
        )
        self._auto_dispatch_enabled = auto_dispatch_enabled
        self._research_enabled = research_enabled
        self._last_dispatched_revision = 0
        self._manual_dispatch_revision: int | None = None
        self._latest_turn_id: str | None = None

    async def ingest(self, event: TranscriptEvent) -> CoordinatorUpdate:
        """Apply ASR output and create/cancel draft work as session state changes."""
        async with self._ingest_lock:
            return await self._ingest(event)

    async def _ingest(self, event: TranscriptEvent) -> CoordinatorUpdate:
        transcript = self._transcript.apply(event)
        turn = self._turns.apply(event)
        timeline = _timeline_entry(turn, transcript)
        if turn is not None:
            self._latest_turn_id = turn.turn_id
        if not self._auto_dispatch_enabled:
            return CoordinatorUpdate(
                transcript=transcript,
                turn=turn,
                draft=self.current_draft(),
                fast_draft=self.current_fast_draft(),
                timeline=timeline,
            )
        if event.speaker_role is SpeakerRole.USER and turn is not None:
            await self._cancel_active(GenerationStatus.CANCELLED)
            await self._cancel_fast(GenerationStatus.CANCELLED)
            if turn.state is not TurnState.FINALIZED:
                await self._cancel_insights(GenerationStatus.SUPERSEDED)
                await self._cancel_research(GenerationStatus.SUPERSEDED)
            if turn.state is TurnState.FINALIZED:
                await self._cancel_insights(GenerationStatus.SUPERSEDED)
                await self._cancel_research(GenerationStatus.SUPERSEDED)
                self._mark_automatic_dispatch(transcript.revision)
                await self._start_commentary(turn, transcript)
                await self._start_summary(turn.turn_id, transcript)
                await self._start_research(turn.turn_id, transcript)
        elif turn is not None and turn.speaker_role is SpeakerRole.REMOTE:
            if turn.state in {
                TurnState.SPEAKING,
                TurnState.CANDIDATE_END,
                TurnState.REOPENED,
            }:
                await self._cancel_active(GenerationStatus.SUPERSEDED)
                await self._cancel_fast(GenerationStatus.SUPERSEDED)
            await self._cancel_insights(GenerationStatus.SUPERSEDED)
            await self._cancel_research(GenerationStatus.SUPERSEDED)
            if turn.state is TurnState.FINALIZED:
                if self._turns.has_active_speech(SpeakerRole.USER):
                    logger.info(
                        "suppressed remote reply during local speech",
                        extra={"reason": "local_speaking"},
                    )
                    return CoordinatorUpdate(
                        transcript=transcript,
                        turn=turn,
                        draft=self.current_draft(),
                        fast_draft=self.current_fast_draft(),
                        timeline=timeline,
                    )
                self._mark_automatic_dispatch(transcript.revision)
                await self._start_draft(turn, transcript)
                await self._start_fast_draft(turn, transcript)
                await self._start_commentary(turn, transcript)
                await self._start_summary(turn.turn_id, transcript)
                await self._start_research(turn.turn_id, transcript)
        return CoordinatorUpdate(
            transcript=transcript,
            turn=turn,
            draft=self.current_draft(),
            fast_draft=self.current_fast_draft(),
            timeline=timeline,
        )

    async def wait_for_idle(self) -> DraftResult | None:
        """Wait for the current draft task, if any, without exposing stale work."""
        while True:
            tasks = tuple(
                task
                for task in (
                    self._active_task,
                    self._active_fast_task,
                    self._commentary_task,
                    self._summary_task,
                    self._research_task,
                )
                if task is not None
            )
            if not tasks:
                return self._last_draft
            await asyncio.gather(*tasks)
            if all(task.done() for task in tasks) and all(
                task is None or task.done()
                for task in (
                    self._active_task,
                    self._active_fast_task,
                    self._commentary_task,
                    self._summary_task,
                    self._research_task,
                )
            ):
                break
        return self._last_draft

    async def next_completed_draft(self) -> DraftResult:
        """Wait for a terminal advisory result and deterministic tests."""
        return await self._completed_drafts.get()

    async def next_draft_event(self) -> DraftResult:
        """Wait for the latest visible draft lifecycle event for terminal output."""
        return await self._draft_events.get()

    async def next_completed_fast_draft(self) -> DraftResult:
        """Wait for a terminal instant-reply result for deterministic tests."""
        return await self._completed_fast_drafts.get()

    async def next_fast_draft_event(self) -> DraftResult:
        """Wait for the latest instant-reply lifecycle event for terminal output."""
        return await self._fast_draft_events.get()

    def current_draft(self) -> DraftResult | None:
        """Return only current display guidance, never provider context."""
        return _current_draft(self._last_draft, self._request_revision_is_current)

    def current_fast_draft(self) -> DraftResult | None:
        """Return the current instant reply suggestion, never provider context."""
        return _current_draft(
            self._last_fast_draft,
            self._request_revision_is_current,
        )

    def transcript_snapshot(self) -> TranscriptSnapshot:
        """Return the bounded current provider context for local diagnostics."""
        return self._transcript.snapshot()

    async def next_completed_insight(self) -> InsightResult:
        """Wait for one completed commentary or summary result."""
        return await self._completed_insights.get()

    def drain_completed_insights(self) -> tuple[InsightResult, ...]:
        """Return completed background outputs without blocking CLI replay."""
        insights: list[InsightResult] = []
        while not self._completed_insights.empty():
            insights.append(self._completed_insights.get_nowait())
        return tuple(insights)

    async def stop(self) -> None:
        """Immediately cancel active generation during pause or shutdown."""
        await self._cancel_active(GenerationStatus.CANCELLED)
        await self._cancel_fast(GenerationStatus.CANCELLED)
        await self._cancel_insights(GenerationStatus.CANCELLED)
        await self._cancel_research(GenerationStatus.CANCELLED)

    async def start_session(self) -> str | None:
        """Reset provider work before starting a new listening lifecycle."""
        await self.stop()
        self._transcript.reset_session_context()
        self._last_dispatched_revision = self._transcript.snapshot().revision
        self._manual_dispatch_revision = None
        self._latest_turn_id = None
        if not isinstance(self._research_provider, SessionDraftProvider):
            return None
        return await self._research_provider.start_session()

    async def configure_dispatch(
        self,
        *,
        auto_dispatch_enabled: bool,
        research_enabled: bool,
    ) -> None:
        """Apply future dispatch behavior without losing transcript state."""
        async with self._ingest_lock:
            self._auto_dispatch_enabled = auto_dispatch_enabled
            self._research_enabled = research_enabled
            if auto_dispatch_enabled:
                self._manual_dispatch_revision = None
            if not research_enabled:
                await self._cancel_research(GenerationStatus.CANCELLED)
            logger.info(
                "generation dispatch settings changed",
                extra={
                    "auto_dispatch_enabled": auto_dispatch_enabled,
                    "research_enabled": research_enabled,
                },
            )

    def dispatch_state(self) -> tuple[bool, bool]:
        """Return automatic-mode and server-owned pending-transcript state."""
        return self._auto_dispatch_enabled, self.has_unsent_transcript()

    def has_unsent_transcript(self) -> bool:
        """Report meaningful transcript text newer than the last dispatch."""
        snapshot = self._transcript.snapshot()
        if snapshot.revision <= self._last_dispatched_revision:
            return False
        return any(line.text.strip() for line in snapshot.lines)

    async def dispatch_current(self) -> bool:
        """Supersede all work and dispatch the newest complete conversation state."""
        async with self._ingest_lock:
            transcript = self._transcript.snapshot()
            if not self.has_unsent_transcript():
                logger.debug(
                    "manual generation dispatch ignored",
                    extra={"reason": "no_unsent_transcript"},
                )
                return False
            await self._cancel_all(GenerationStatus.SUPERSEDED)
            self._last_dispatched_revision = transcript.revision
            self._manual_dispatch_revision = transcript.revision
            trigger_turn_id = self._latest_turn_id or str(uuid4())
            await self._start_draft_request(trigger_turn_id, transcript)
            await self._start_fast_draft_request(trigger_turn_id, transcript)
            await self._start_insight(
                InsightKind.COMMENTARY,
                trigger_turn_id,
                transcript,
            )
            await self._start_insight(
                InsightKind.SUMMARY,
                trigger_turn_id,
                transcript,
            )
            await self._start_research(trigger_turn_id, transcript)
            logger.info(
                "manual generation dispatch started",
                extra={"context_revision": transcript.revision},
            )
            return True

    async def _start_draft(
        self,
        turn: TurnEvent,
        transcript: TranscriptSnapshot,
    ) -> None:
        await self._cancel_active(GenerationStatus.SUPERSEDED)
        await self._start_draft_request(turn.turn_id, transcript)

    async def _start_draft_request(
        self,
        trigger_turn_id: str,
        transcript: TranscriptSnapshot,
    ) -> None:
        """Create a fresh generation ID for an immutable reply context."""
        request = DraftRequest(
            generation_id=str(uuid4()),
            trigger_turn_id=trigger_turn_id,
            context_revision=transcript.revision,
            transcript=transcript,
            deadline_seconds=self._draft_generation_deadline_seconds,
        )
        self._active_generation_id = request.generation_id
        self._active_draft_request = request
        self._publish_draft_event(
            _draft_lifecycle_result(request, GenerationStatus.RUNNING)
        )
        context = contextvars.copy_context()
        self._active_task = context.run(asyncio.create_task, self._generate(request))

    async def _generate(self, request: DraftRequest) -> None:
        try:
            async with asyncio.timeout(request.deadline_seconds):
                result = await self._draft_provider.draft(request)
        except TimeoutError:
            logger.warning(
                "draft generation exceeded deadline",
                extra={
                    "generation_id": request.generation_id,
                    "deadline_seconds": request.deadline_seconds,
                },
            )
            if self._active_generation_id == request.generation_id:
                self._publish_draft_failure(request)
            return
        # A provider failure must not terminate continuous ASR or its session.
        except Exception as error:
            logger.error(
                "draft generation failed",
                extra={
                    "generation_id": request.generation_id,
                    "error_type": type(error).__name__,
                },
            )
            if self._active_generation_id == request.generation_id:
                self._publish_draft_failure(request)
            return

        if (
            self._active_generation_id != request.generation_id
            or not self._request_revision_is_current(request.context_revision)
        ):
            logger.info(
                "discarded stale draft",
                extra={"generation_id": request.generation_id},
            )
            return
        self._publish_terminal_draft(result)
        logger.info(
            "draft generation completed",
            extra={"generation_id": request.generation_id},
        )

    async def _start_fast_draft(
        self,
        turn: TurnEvent,
        transcript: TranscriptSnapshot,
    ) -> None:
        await self._cancel_fast(GenerationStatus.SUPERSEDED)
        await self._start_fast_draft_request(turn.turn_id, transcript)

    async def _start_fast_draft_request(
        self,
        trigger_turn_id: str,
        transcript: TranscriptSnapshot,
    ) -> None:
        """Start the instant reply lane on the same immutable reply context."""
        if self._fast_draft_provider is None:
            return
        request = DraftRequest(
            generation_id=str(uuid4()),
            trigger_turn_id=trigger_turn_id,
            context_revision=transcript.revision,
            transcript=transcript,
            deadline_seconds=self._draft_generation_deadline_seconds,
        )
        self._active_fast_generation_id = request.generation_id
        self._active_fast_draft_request = request
        self._publish_fast_draft_event(
            _draft_lifecycle_result(request, GenerationStatus.RUNNING)
        )
        context = contextvars.copy_context()
        self._active_fast_task = context.run(
            asyncio.create_task,
            self._generate_fast(request),
        )

    async def _generate_fast(self, request: DraftRequest) -> None:
        provider = self._fast_draft_provider
        if provider is None:
            return
        try:
            async with asyncio.timeout(request.deadline_seconds):
                result = await provider.draft(request)
        except TimeoutError:
            logger.warning(
                "fast draft generation exceeded deadline",
                extra={
                    "generation_id": request.generation_id,
                    "deadline_seconds": request.deadline_seconds,
                },
            )
            if self._active_fast_generation_id == request.generation_id:
                self._publish_fast_draft_failure(request)
            return
        # A provider failure must not terminate continuous ASR or its session.
        except Exception as error:
            logger.error(
                "fast draft generation failed",
                extra={
                    "generation_id": request.generation_id,
                    "error_type": type(error).__name__,
                },
            )
            if self._active_fast_generation_id == request.generation_id:
                self._publish_fast_draft_failure(request)
            return

        if (
            self._active_fast_generation_id != request.generation_id
            or not self._request_revision_is_current(request.context_revision)
        ):
            logger.info(
                "discarded stale fast draft",
                extra={"generation_id": request.generation_id},
            )
            return
        self._publish_terminal_fast_draft(result)
        logger.info(
            "fast draft generation completed",
            extra={"generation_id": request.generation_id},
        )

    async def _start_commentary(
        self,
        turn: TurnEvent,
        transcript: TranscriptSnapshot,
    ) -> None:
        await self._cancel_commentary(GenerationStatus.SUPERSEDED)
        await self._start_insight(InsightKind.COMMENTARY, turn.turn_id, transcript)

    async def _start_summary(
        self,
        turn_id: str,
        transcript: TranscriptSnapshot,
    ) -> None:
        await self._cancel_summary(GenerationStatus.SUPERSEDED)
        await self._start_insight(InsightKind.SUMMARY, turn_id, transcript)

    async def _start_research(
        self,
        turn_id: str,
        transcript: TranscriptSnapshot,
    ) -> None:
        if not self._research_enabled or self._research_provider is None:
            return
        await self._cancel_research(GenerationStatus.SUPERSEDED)
        request = DraftRequest(
            generation_id=str(uuid4()),
            trigger_turn_id=turn_id,
            context_revision=transcript.revision,
            transcript=transcript,
            deadline_seconds=self._draft_generation_deadline_seconds,
        )
        self._active_research_generation_id = request.generation_id
        context = contextvars.copy_context()
        self._research_task = context.run(
            asyncio.create_task,
            self._generate_research(request),
        )

    async def _generate_research(self, request: DraftRequest) -> None:
        provider = self._research_provider
        if provider is None:
            return
        try:
            async with asyncio.timeout(request.deadline_seconds):
                result = await provider.draft(request)
        except TimeoutError:
            logger.warning(
                "research generation exceeded deadline",
                extra={
                    "generation_id": request.generation_id,
                    "deadline_seconds": request.deadline_seconds,
                },
            )
            return
        except Exception as error:
            logger.error(
                "research generation failed",
                extra={
                    "generation_id": request.generation_id,
                    "error_type": type(error).__name__,
                },
            )
            return
        if (
            self._active_research_generation_id != request.generation_id
            or not self._request_revision_is_current(request.context_revision)
        ):
            logger.info(
                "discarded stale research",
                extra={"generation_id": request.generation_id},
            )
            return
        if result.text.strip() == NO_NEW_RESEARCH_RESULT:
            logger.info(
                "research completed without new findings",
                extra={"generation_id": request.generation_id},
            )
            return
        if not self._transcript.set_research_context(
            result.text,
            request.context_revision,
        ):
            logger.info(
                "discarded non-advancing research",
                extra={"generation_id": request.generation_id},
            )
            return
        logger.info(
            "shared research context updated",
            extra={"generation_id": request.generation_id},
        )

    async def _start_insight(
        self,
        kind: InsightKind,
        turn_id: str,
        transcript: TranscriptSnapshot,
    ) -> None:
        provider = self._insight_provider(request_kind=kind)
        if provider is None:
            return
        request = InsightRequest(
            generation_id=str(uuid4()),
            kind=kind,
            trigger_turn_id=turn_id,
            context_revision=transcript.revision,
            transcript=transcript,
            deadline_seconds=self._generation_deadline_seconds,
        )
        context = contextvars.copy_context()
        task = context.run(asyncio.create_task, self._generate_insight(request))
        if kind is InsightKind.COMMENTARY:
            self._commentary_task = task
            return
        self._summary_task = task

    async def _generate_insight(self, request: InsightRequest) -> None:
        provider = self._insight_provider(request.kind)
        if provider is None:
            return
        try:
            async with asyncio.timeout(request.deadline_seconds):
                result = await provider.insight(request)
        except TimeoutError:
            logger.warning(
                "background generation exceeded deadline",
                extra={
                    "generation_id": request.generation_id,
                    "kind": request.kind.value,
                    "deadline_seconds": request.deadline_seconds,
                },
            )
            result = _failed_insight(request)
        # Background failure remains observable but never stops ASR or a reply.
        except Exception as error:
            logger.error(
                "background generation failed",
                extra={
                    "generation_id": request.generation_id,
                    "kind": request.kind,
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                },
            )
            result = _failed_insight(request)

        if request.kind is InsightKind.SUMMARY:
            self._handle_summary_result(request, result)
            return

        if not self._request_revision_is_current(request.context_revision):
            logger.info(
                "discarded stale background result",
                extra={"generation_id": request.generation_id, "kind": request.kind},
            )
            return
        self._publish_insight(result)
        logger.info(
            "background generation completed",
            extra={"generation_id": request.generation_id, "kind": request.kind},
        )

    def _insight_provider(
        self,
        request_kind: InsightKind,
    ) -> InsightProvider | None:
        if request_kind is InsightKind.COMMENTARY:
            return self._commentary_provider
        return self._summary_provider

    def _handle_summary_result(
        self,
        request: InsightRequest,
        result: InsightResult,
    ) -> None:
        if not self._request_revision_is_current(request.context_revision):
            logger.info(
                "discarded stale summary",
                extra={"generation_id": request.generation_id},
            )
            return
        if result.status is not GenerationStatus.COMPLETED:
            self._publish_insight(result)
            return
        if not self._transcript.set_running_summary(
            result.text,
            request.context_revision,
        ):
            logger.info(
                "discarded non-advancing summary",
                extra={"generation_id": request.generation_id},
            )
            return
        self._publish_insight(result)
        logger.info(
            "running summary updated",
            extra={"generation_id": request.generation_id},
        )

    def _publish_terminal_draft(self, result: DraftResult) -> None:
        """Store a terminal result and make it visible to both consumers."""
        if result.status is GenerationStatus.COMPLETED:
            self._last_draft = result
        if self._completed_drafts.full():
            self._completed_drafts.get_nowait()
            logger.warning(
                "discarded unrendered draft",
                extra={
                    "generation_id": result.generation_id,
                    "reason": "backlog_full",
                },
            )
        self._completed_drafts.put_nowait(result)
        self._publish_draft_event(result)

    def _publish_draft_event(self, result: DraftResult) -> None:
        """Keep only the latest terminal-facing lifecycle update in memory."""
        if self._draft_events.full():
            self._draft_events.get_nowait()
            logger.debug(
                "discarded unrendered draft lifecycle event",
                extra={
                    "generation_id": result.generation_id,
                    "reason": "backlog_full",
                },
            )
        self._draft_events.put_nowait(result)

    def _publish_draft_failure(self, request: DraftRequest) -> None:
        """Make a current provider failure observable without visible text."""
        self._publish_terminal_draft(
            _draft_lifecycle_result(request, GenerationStatus.FAILED)
        )

    def _publish_terminal_fast_draft(self, result: DraftResult) -> None:
        """Store a terminal instant reply and make it visible to both consumers."""
        if result.status is GenerationStatus.COMPLETED:
            self._last_fast_draft = result
        if self._completed_fast_drafts.full():
            self._completed_fast_drafts.get_nowait()
            logger.warning(
                "discarded unrendered fast draft",
                extra={
                    "generation_id": result.generation_id,
                    "reason": "backlog_full",
                },
            )
        self._completed_fast_drafts.put_nowait(result)
        self._publish_fast_draft_event(result)

    def _publish_fast_draft_event(self, result: DraftResult) -> None:
        """Keep only the latest instant-reply lifecycle update in memory."""
        if self._fast_draft_events.full():
            self._fast_draft_events.get_nowait()
            logger.debug(
                "discarded unrendered fast draft lifecycle event",
                extra={
                    "generation_id": result.generation_id,
                    "reason": "backlog_full",
                },
            )
        self._fast_draft_events.put_nowait(result)

    def _publish_fast_draft_failure(self, request: DraftRequest) -> None:
        """Make a current instant-reply failure observable without visible text."""
        self._publish_terminal_fast_draft(
            _draft_lifecycle_result(request, GenerationStatus.FAILED)
        )

    def _publish_insight(self, result: InsightResult) -> None:
        """Keep a bounded newest-first backlog for line-oriented CLI rendering."""
        if self._completed_insights.full():
            self._completed_insights.get_nowait()
            logger.warning(
                "discarded unrendered background result",
                extra={"generation_id": result.generation_id, "reason": "backlog_full"},
            )
        self._completed_insights.put_nowait(result)

    async def _cancel_active(self, status: GenerationStatus) -> None:
        task = self._active_task
        generation_id = self._active_generation_id
        request = self._active_draft_request
        self._active_task = None
        self._active_generation_id = None
        self._active_draft_request = None
        if task is None or task.done() or request is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            self._publish_terminal_draft(_draft_lifecycle_result(request, status))
            logger.info(
                "draft generation cancelled",
                extra={"generation_id": generation_id, "status": status.value},
            )

    async def _cancel_fast(self, status: GenerationStatus) -> None:
        task = self._active_fast_task
        generation_id = self._active_fast_generation_id
        request = self._active_fast_draft_request
        self._active_fast_task = None
        self._active_fast_generation_id = None
        self._active_fast_draft_request = None
        if task is None or task.done() or request is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            self._publish_terminal_fast_draft(_draft_lifecycle_result(request, status))
            logger.info(
                "fast draft generation cancelled",
                extra={"generation_id": generation_id, "status": status.value},
            )

    async def _cancel_insights(self, status: GenerationStatus) -> None:
        await self._cancel_commentary(status)
        await self._cancel_summary(status)

    async def _cancel_all(self, status: GenerationStatus) -> None:
        await self._cancel_active(status)
        await self._cancel_fast(status)
        await self._cancel_insights(status)
        await self._cancel_research(status)

    async def _cancel_commentary(self, status: GenerationStatus) -> None:
        task = self._commentary_task
        self._commentary_task = None
        await _cancel_task(task, "commentary", status)

    async def _cancel_summary(self, status: GenerationStatus) -> None:
        task = self._summary_task
        self._summary_task = None
        await _cancel_task(task, "summary", status)

    async def _cancel_research(self, status: GenerationStatus) -> None:
        task = self._research_task
        self._research_task = None
        self._active_research_generation_id = None
        await _cancel_task(task, "research", status)

    def _mark_automatic_dispatch(self, revision: int) -> None:
        self._last_dispatched_revision = revision
        self._manual_dispatch_revision = None

    def _request_revision_is_current(self, revision: int) -> bool:
        if self._manual_dispatch_revision is not None:
            return revision == self._manual_dispatch_revision
        return self._transcript.snapshot().revision == revision


def _failed_insight(request: InsightRequest) -> InsightResult:
    return InsightResult(
        generation_id=request.generation_id,
        kind=request.kind,
        trigger_turn_id=request.trigger_turn_id,
        context_revision=request.context_revision,
        status=GenerationStatus.FAILED,
        text="",
    )


def _current_draft(
    draft: DraftResult | None,
    revision_is_current: Callable[[int], bool],
) -> DraftResult | None:
    """Return a stored draft only while it is completed and still current."""
    if draft is None or draft.status is not GenerationStatus.COMPLETED:
        return None
    if not revision_is_current(draft.context_revision):
        return None
    return draft


def _draft_lifecycle_result(
    request: DraftRequest,
    status: GenerationStatus,
) -> DraftResult:
    return DraftResult(
        generation_id=request.generation_id,
        trigger_turn_id=request.trigger_turn_id,
        context_revision=request.context_revision,
        status=status,
        text="",
    )


def _timeline_entry(
    turn: TurnEvent | None,
    transcript: TranscriptSnapshot,
) -> TimelineEntry | None:
    if turn is None or turn.state is not TurnState.FINALIZED:
        return None
    for line in reversed(transcript.lines):
        if (
            line.speaker_role is turn.speaker_role
            and line.revision == turn.transcript_revision
            and line.text.strip()
        ):
            return TimelineEntry(
                turn_id=turn.turn_id,
                speaker_role=turn.speaker_role,
                transcript_revision=turn.transcript_revision,
                text=line.text,
            )
    return None


async def _cancel_task(
    task: asyncio.Task[None] | None,
    kind: str,
    status: GenerationStatus,
) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        logger.info(
            "background generation cancelled",
            extra={"kind": kind, "status": status.value},
        )
