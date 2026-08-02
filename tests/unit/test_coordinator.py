from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta
from unittest.mock import patch

from two_x_brainz.aigate import DraftProvider, InsightProvider
from two_x_brainz.contracts import (
    DraftOutcomeAction,
    DraftRequest,
    DraftResult,
    GenerationStatus,
    InsightKind,
    InsightRequest,
    InsightResult,
    SpeakerRole,
    TranscriptEvent,
    TranscriptEventType,
    TurnState,
)
from two_x_brainz.coordinator import ConversationCoordinator
from two_x_brainz.errors import ProtocolError

_TEST_PROVIDER_DEADLINE = timedelta(milliseconds=10)


class BlockingProvider(DraftProvider):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def draft(self, request: DraftRequest) -> DraftResult:
        self.started.set()
        await self.release.wait()
        return DraftResult(
            generation_id=request.generation_id,
            trigger_turn_id=request.trigger_turn_id,
            context_revision=request.context_revision,
            status=GenerationStatus.COMPLETED,
            text="draft",
        )


class ImmediateProvider(DraftProvider):
    async def draft(self, request: DraftRequest) -> DraftResult:
        return DraftResult(
            generation_id=request.generation_id,
            trigger_turn_id=request.trigger_turn_id,
            context_revision=request.context_revision,
            status=GenerationStatus.COMPLETED,
            text="draft",
        )


class ImmediateSessionProvider(DraftProvider, InsightProvider):
    async def draft(self, request: DraftRequest) -> DraftResult:
        return DraftResult(
            generation_id=request.generation_id,
            trigger_turn_id=request.trigger_turn_id,
            context_revision=request.context_revision,
            status=GenerationStatus.COMPLETED,
            text="draft",
        )

    async def insight(self, request: InsightRequest) -> InsightResult:
        return InsightResult(
            generation_id=request.generation_id,
            kind=request.kind,
            trigger_turn_id=request.trigger_turn_id,
            context_revision=request.context_revision,
            status=GenerationStatus.COMPLETED,
            text=request.kind.value,
        )


class BlockingInsightProvider(ImmediateSessionProvider):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def insight(self, request: InsightRequest) -> InsightResult:
        self.started.set()
        await self.release.wait()
        return await super().insight(request)


class CancellationIgnoringSummaryProvider(ImmediateSessionProvider):
    def __init__(self) -> None:
        self.summary_started = asyncio.Event()

    async def insight(self, request: InsightRequest) -> InsightResult:
        if request.kind is InsightKind.COMMENTARY:
            return await super().insight(request)
        self.summary_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return await super().insight(request)
        raise AssertionError("unreachable after cancellation")


class DeadlineProvider(DraftProvider):
    def __init__(self) -> None:
        self.requests: list[DraftRequest] = []

    async def draft(self, request: DraftRequest) -> DraftResult:
        self.requests.append(request)
        await asyncio.Event().wait()
        raise AssertionError("unreachable after cancellation")


class DeadlineSessionProvider(ImmediateSessionProvider):
    def __init__(self) -> None:
        self.requests: list[InsightRequest] = []

    async def insight(self, request: InsightRequest) -> InsightResult:
        self.requests.append(request)
        await asyncio.Event().wait()
        raise AssertionError("unreachable after cancellation")


class FirstDeadlineThenImmediateProvider(ImmediateProvider):
    def __init__(self) -> None:
        self.requests: list[DraftRequest] = []

    async def draft(self, request: DraftRequest) -> DraftResult:
        self.requests.append(request)
        if len(self.requests) == 1:
            await asyncio.Event().wait()
            raise AssertionError("unreachable after cancellation")
        return await super().draft(request)


class FailingProvider(DraftProvider):
    async def draft(self, request: DraftRequest) -> DraftResult:
        raise ProtocolError("invalid provider response")


class FailingInsightProvider(ImmediateSessionProvider):
    async def insight(self, request: InsightRequest) -> InsightResult:
        raise ProtocolError("invalid provider response")


class ConversationCoordinatorTests(unittest.TestCase):
    def test_user_speech_cancels_a_remote_turn_draft(self) -> None:
        asyncio.run(self._assert_user_speech_cancels_draft())

    def test_remote_endpoint_does_not_start_draft_before_final(self) -> None:
        asyncio.run(self._assert_remote_endpoint_waits_for_final())

    def test_new_remote_speech_supersedes_an_active_draft(self) -> None:
        asyncio.run(self._assert_new_remote_speech_supersedes_draft())

    def test_remote_endpoint_supersedes_an_active_draft(self) -> None:
        asyncio.run(self._assert_remote_endpoint_supersedes_draft())

    def test_remote_final_during_local_speech_does_not_start_a_draft(self) -> None:
        asyncio.run(self._assert_overlap_suppresses_remote_draft())

    async def _assert_user_speech_cancels_draft(self) -> None:
        provider = BlockingProvider()
        coordinator = ConversationCoordinator(provider)
        await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.FINAL, 1)
        )
        await provider.started.wait()
        await coordinator.ingest(
            _event(SpeakerRole.USER, TranscriptEventType.PARTIAL, 1)
        )
        provider.release.set()

        self.assertIsNone(await coordinator.wait_for_idle())

    async def _assert_remote_endpoint_waits_for_final(self) -> None:
        provider = BlockingProvider()
        coordinator = ConversationCoordinator(provider)

        endpoint_update = await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.ENDPOINT, 1)
        )

        self.assertIsNotNone(endpoint_update.turn)
        assert endpoint_update.turn is not None
        self.assertEqual(endpoint_update.turn.state, TurnState.CANDIDATE_END)
        self.assertFalse(provider.started.is_set())

        await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.FINAL, 2)
        )
        await provider.started.wait()
        await coordinator.stop()

    async def _assert_new_remote_speech_supersedes_draft(self) -> None:
        provider = BlockingProvider()
        coordinator = ConversationCoordinator(provider)
        await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.FINAL, 1)
        )
        await provider.started.wait()

        update = await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.PARTIAL, 2)
        )
        provider.release.set()

        self.assertIsNotNone(update.turn)
        assert update.turn is not None
        self.assertEqual(update.turn.state, TurnState.SPEAKING)
        self.assertIsNone(await coordinator.wait_for_idle())

    async def _assert_remote_endpoint_supersedes_draft(self) -> None:
        provider = BlockingProvider()
        coordinator = ConversationCoordinator(provider)
        await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.FINAL, 1)
        )
        await provider.started.wait()

        update = await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.ENDPOINT, 2)
        )
        provider.release.set()

        self.assertIsNotNone(update.turn)
        assert update.turn is not None
        self.assertEqual(update.turn.state, TurnState.CANDIDATE_END)
        self.assertIsNone(await coordinator.wait_for_idle())

    async def _assert_overlap_suppresses_remote_draft(self) -> None:
        provider = BlockingProvider()
        coordinator = ConversationCoordinator(provider)
        await coordinator.ingest(
            _event(SpeakerRole.USER, TranscriptEventType.PARTIAL, 1)
        )
        remote_update = await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.FINAL, 1)
        )

        self.assertIsNotNone(remote_update.timeline)
        self.assertFalse(provider.started.is_set())

        await coordinator.ingest(_event(SpeakerRole.USER, TranscriptEventType.FINAL, 2))
        await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.FINAL, 2)
        )
        await provider.started.wait()
        await coordinator.stop()

    def test_stale_revision_does_not_replace_transcript(self) -> None:
        asyncio.run(self._assert_stale_revision_is_ignored())

    def test_completed_draft_is_available_to_terminal_renderer(self) -> None:
        asyncio.run(self._assert_completed_draft_is_renderable())

    def test_user_turn_emits_timeline_commentary_and_summary(self) -> None:
        asyncio.run(self._assert_user_turn_background_outputs())

    def test_remote_speech_preempts_background_work(self) -> None:
        asyncio.run(self._assert_remote_speech_preempts_background_work())

    def test_duplicate_final_does_not_emit_duplicate_timeline_entry(self) -> None:
        asyncio.run(self._assert_duplicate_final_has_no_new_timeline_entry())

    def test_stale_background_result_is_not_rendered(self) -> None:
        asyncio.run(self._assert_stale_background_result_is_not_rendered())

    def test_stale_summary_cannot_replace_current_context(self) -> None:
        asyncio.run(self._assert_stale_summary_is_not_accepted())

    def test_draft_outcome_is_retained_after_later_transcript_changes(self) -> None:
        asyncio.run(self._assert_draft_outcome_is_retained())

    def test_current_draft_can_be_edited_and_regenerated(self) -> None:
        asyncio.run(self._assert_draft_can_be_edited_and_regenerated())

    def test_stale_draft_cannot_be_actioned(self) -> None:
        asyncio.run(self._assert_stale_draft_cannot_be_actioned())

    def test_draft_deadline_publishes_a_failed_result(self) -> None:
        asyncio.run(self._assert_draft_deadline_publishes_failed_result())

    def test_background_deadlines_publish_failed_results(self) -> None:
        asyncio.run(self._assert_background_deadlines_publish_failed_results())

    def test_background_failure_logs_a_safe_error_type(self) -> None:
        asyncio.run(self._assert_background_failure_logs_a_safe_error_type())

    def test_remote_turn_can_start_after_a_prior_deadline(self) -> None:
        asyncio.run(self._assert_remote_turn_can_start_after_prior_deadline())

    def test_non_positive_generation_deadline_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be positive"):
            ConversationCoordinator(
                ImmediateProvider(),
                generation_deadline=timedelta(),
            )

    def test_draft_lifecycle_emits_running_then_completed(self) -> None:
        asyncio.run(self._assert_draft_lifecycle_completion())

    def test_draft_lifecycle_emits_cancelled_without_text(self) -> None:
        asyncio.run(self._assert_draft_lifecycle_cancellation())

    def test_draft_lifecycle_emits_failed_without_text(self) -> None:
        asyncio.run(self._assert_draft_lifecycle_failure())

    async def _assert_draft_lifecycle_completion(self) -> None:
        provider = BlockingProvider()
        coordinator = ConversationCoordinator(provider)
        await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.FINAL, 1)
        )

        running = await coordinator.next_draft_event()
        self.assertEqual(running.status, GenerationStatus.RUNNING)
        self.assertEqual(running.text, "")
        await provider.started.wait()
        provider.release.set()

        completed = await coordinator.next_draft_event()
        self.assertEqual(completed.status, GenerationStatus.COMPLETED)
        self.assertEqual(completed.generation_id, running.generation_id)
        self.assertEqual(completed.trigger_turn_id, running.trigger_turn_id)

    async def _assert_draft_lifecycle_cancellation(self) -> None:
        provider = BlockingProvider()
        coordinator = ConversationCoordinator(provider)
        await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.FINAL, 1)
        )
        await coordinator.next_draft_event()
        await provider.started.wait()
        await coordinator.ingest(
            _event(SpeakerRole.USER, TranscriptEventType.PARTIAL, 1)
        )

        cancelled = await coordinator.next_draft_event()
        self.assertEqual(cancelled.status, GenerationStatus.CANCELLED)
        self.assertEqual(cancelled.text, "")

    async def _assert_draft_lifecycle_failure(self) -> None:
        coordinator = ConversationCoordinator(FailingProvider())
        await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.FINAL, 1)
        )
        await coordinator.next_draft_event()

        failed = await coordinator.next_draft_event()
        self.assertEqual(failed.status, GenerationStatus.FAILED)
        self.assertEqual(failed.text, "")

    async def _assert_completed_draft_is_renderable(self) -> None:
        coordinator = ConversationCoordinator(ImmediateProvider())
        await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.FINAL, 1)
        )

        draft = await coordinator.next_completed_draft()

        self.assertEqual(draft.status, GenerationStatus.COMPLETED)
        self.assertEqual(draft.text, "draft")

    async def _assert_user_turn_background_outputs(self) -> None:
        provider = ImmediateSessionProvider()
        coordinator = ConversationCoordinator(provider, provider)
        update = await coordinator.ingest(
            _event(SpeakerRole.USER, TranscriptEventType.FINAL, 1)
        )

        self.assertIsNotNone(update.timeline)
        await coordinator.wait_for_idle()
        insights = coordinator.drain_completed_insights()

        self.assertEqual(
            [insight.kind for insight in insights],
            [InsightKind.COMMENTARY, InsightKind.SUMMARY],
        )
        self.assertEqual(
            coordinator.transcript_snapshot().running_summary,
            InsightKind.SUMMARY.value,
        )

    async def _assert_remote_speech_preempts_background_work(self) -> None:
        provider = BlockingInsightProvider()
        coordinator = ConversationCoordinator(provider, provider)
        await coordinator.ingest(_event(SpeakerRole.USER, TranscriptEventType.FINAL, 1))
        await provider.started.wait()
        await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.PARTIAL, 1)
        )
        provider.release.set()

        await coordinator.wait_for_idle()

        self.assertEqual(coordinator.drain_completed_insights(), ())
        self.assertEqual(coordinator.transcript_snapshot().running_summary, "")

    async def _assert_duplicate_final_has_no_new_timeline_entry(self) -> None:
        provider = ImmediateSessionProvider()
        coordinator = ConversationCoordinator(provider, provider)
        event = _event(SpeakerRole.USER, TranscriptEventType.FINAL, 1)

        first_update = await coordinator.ingest(event)
        await coordinator.wait_for_idle()
        coordinator.drain_completed_insights()
        duplicate_update = await coordinator.ingest(event)
        await coordinator.wait_for_idle()

        self.assertIsNotNone(first_update.timeline)
        self.assertIsNone(duplicate_update.timeline)
        self.assertEqual(coordinator.drain_completed_insights(), ())

    async def _assert_stale_background_result_is_not_rendered(self) -> None:
        provider = BlockingInsightProvider()
        coordinator = ConversationCoordinator(provider, provider)
        await coordinator.ingest(_event(SpeakerRole.USER, TranscriptEventType.FINAL, 1))
        await provider.started.wait()
        await coordinator.ingest(
            _event(SpeakerRole.USER, TranscriptEventType.PARTIAL, 2)
        )
        provider.release.set()

        await coordinator.wait_for_idle()

        self.assertEqual(coordinator.drain_completed_insights(), ())

    async def _assert_stale_summary_is_not_accepted(self) -> None:
        provider = CancellationIgnoringSummaryProvider()
        coordinator = ConversationCoordinator(provider, provider)
        await coordinator.ingest(_event(SpeakerRole.USER, TranscriptEventType.FINAL, 1))
        await provider.summary_started.wait()

        await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.PARTIAL, 1)
        )
        await coordinator.wait_for_idle()

        insights = coordinator.drain_completed_insights()
        self.assertEqual(
            [insight.kind for insight in insights], [InsightKind.COMMENTARY]
        )
        self.assertEqual(coordinator.transcript_snapshot().running_summary, "")

    async def _assert_draft_outcome_is_retained(self) -> None:
        coordinator = ConversationCoordinator(ImmediateProvider())
        await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.FINAL, 1)
        )
        await coordinator.wait_for_idle()

        outcome = coordinator.record_draft_outcome(DraftOutcomeAction.ACCEPTED)

        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome.action, DraftOutcomeAction.ACCEPTED)
        self.assertEqual(outcome.draft.text, "draft")
        self.assertIsNone(
            coordinator.record_draft_outcome(DraftOutcomeAction.DISMISSED)
        )
        await coordinator.ingest(
            _event(SpeakerRole.USER, TranscriptEventType.PARTIAL, 1)
        )
        self.assertEqual(coordinator.draft_outcomes(), (outcome,))

    async def _assert_draft_can_be_edited_and_regenerated(self) -> None:
        coordinator = ConversationCoordinator(ImmediateProvider())
        await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.FINAL, 1)
        )
        await coordinator.wait_for_idle()
        original = coordinator.current_draft()
        self.assertIsNotNone(original)
        assert original is not None

        edited = coordinator.edit_current_draft("  revised reply  ")
        regenerated = await coordinator.regenerate_current_draft()
        await coordinator.wait_for_idle()
        current = coordinator.current_draft()

        self.assertIsNotNone(edited)
        assert edited is not None
        self.assertEqual(edited.text, "revised reply")
        self.assertTrue(regenerated)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertNotEqual(current.generation_id, original.generation_id)
        self.assertEqual(current.text, "draft")

    async def _assert_stale_draft_cannot_be_actioned(self) -> None:
        coordinator = ConversationCoordinator(ImmediateProvider())
        await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.FINAL, 1)
        )
        await coordinator.wait_for_idle()
        await coordinator.ingest(
            _event(SpeakerRole.USER, TranscriptEventType.PARTIAL, 2)
        )

        self.assertIsNone(coordinator.record_draft_outcome(DraftOutcomeAction.ACCEPTED))
        self.assertIsNone(coordinator.edit_current_draft("revised reply"))
        self.assertFalse(await coordinator.regenerate_current_draft())

    async def _assert_draft_deadline_publishes_failed_result(self) -> None:
        provider = DeadlineProvider()
        coordinator = ConversationCoordinator(
            provider,
            generation_deadline=_TEST_PROVIDER_DEADLINE,
        )
        await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.FINAL, 1)
        )

        result = await coordinator.next_completed_draft()

        self.assertEqual(result.status, GenerationStatus.FAILED)
        self.assertEqual(result.text, "")
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(
            provider.requests[0].deadline_seconds,
            _TEST_PROVIDER_DEADLINE.total_seconds(),
        )

    async def _assert_background_deadlines_publish_failed_results(self) -> None:
        provider = DeadlineSessionProvider()
        coordinator = ConversationCoordinator(
            provider,
            provider,
            generation_deadline=_TEST_PROVIDER_DEADLINE,
        )
        await coordinator.ingest(_event(SpeakerRole.USER, TranscriptEventType.FINAL, 1))

        await coordinator.wait_for_idle()
        insights = coordinator.drain_completed_insights()

        self.assertEqual(
            [insight.kind for insight in insights],
            [InsightKind.COMMENTARY, InsightKind.SUMMARY],
        )
        self.assertTrue(
            all(insight.status is GenerationStatus.FAILED for insight in insights)
        )
        self.assertTrue(all(not insight.text for insight in insights))
        self.assertEqual(len(provider.requests), 2)
        self.assertTrue(
            all(
                request.deadline_seconds == _TEST_PROVIDER_DEADLINE.total_seconds()
                for request in provider.requests
            )
        )

    async def _assert_background_failure_logs_a_safe_error_type(self) -> None:
        provider = FailingInsightProvider()
        coordinator = ConversationCoordinator(provider, provider)

        with patch("two_x_brainz.coordinator.logger.error") as logger_error:
            await coordinator.ingest(
                _event(SpeakerRole.USER, TranscriptEventType.FINAL, 1)
            )
            await coordinator.wait_for_idle()

        self.assertEqual(logger_error.call_count, 2)
        for call in logger_error.call_args_list:
            self.assertEqual(call.args, ("background generation failed",))
            self.assertEqual(call.kwargs["extra"]["error_type"], "ProtocolError")
            self.assertEqual(
                call.kwargs["extra"]["error_message"],
                "invalid provider response",
            )

    async def _assert_remote_turn_can_start_after_prior_deadline(self) -> None:
        provider = FirstDeadlineThenImmediateProvider()
        coordinator = ConversationCoordinator(
            provider,
            generation_deadline=_TEST_PROVIDER_DEADLINE,
        )
        await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.FINAL, 1)
        )

        first_result = await coordinator.next_completed_draft()
        await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.FINAL, 2)
        )
        await coordinator.wait_for_idle()
        current = coordinator.current_draft()

        self.assertEqual(first_result.status, GenerationStatus.FAILED)
        self.assertEqual(len(provider.requests), 2)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.status, GenerationStatus.COMPLETED)
        self.assertNotEqual(
            current.generation_id,
            first_result.generation_id,
        )

    async def _assert_stale_revision_is_ignored(self) -> None:
        coordinator = ConversationCoordinator(BlockingProvider())
        await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.PARTIAL, 2)
        )
        update = await coordinator.ingest(
            _event(SpeakerRole.REMOTE, TranscriptEventType.PARTIAL, 1)
        )

        self.assertEqual(update.transcript.lines[0].revision, 2)


def _event(
    speaker_role: SpeakerRole,
    event_type: TranscriptEventType,
    revision: int,
) -> TranscriptEvent:
    stream_id = f"{speaker_role.value}-stream"
    return TranscriptEvent(
        session_id="session",
        stream_id=stream_id,
        utterance_id=f"{stream_id}:{revision}",
        revision=revision,
        speaker_role=speaker_role,
        source_event_type=event_type,
        asr_model="nemotron-3.5-asr-0.6b",
        text="synthetic test transcript",
        is_final=event_type is TranscriptEventType.FINAL,
        audio_seconds=1.0,
        words=(),
    )
