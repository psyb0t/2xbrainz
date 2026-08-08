from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from two_x_brainz.contracts import (
    DraftRequest,
    DraftResult,
    GenerationStatus,
    InsightKind,
    InsightRequest,
    InsightResult,
    SpeakerRole,
    TranscriptEvent,
    TranscriptEventType,
)
from two_x_brainz.coordinator import ConversationCoordinator
from two_x_brainz.evaluation import TimedRecord, evaluate_observations, load_scenario

_SCENARIO_PATH = "tests/fixtures/slang-interrupted-project-chat.json"
_FINAL_DRAFT = (
    "Say the Thursday commitment plainly: I own the failover test and will send "
    "the numbers before lunch. For the linkset point, describe it only after the "
    "RFC 9264 research is verified."
)
_FINAL_COMMENTARY = (
    "Keep the Thursday failover commitment crisp. Verify the RFC 9264 linkset "
    "research before making a technical claim."
)
_FINAL_SUMMARY = (
    "RelayCrate is a gateway for provider calls. The corrected demo date is "
    "Thursday. The user owns the failover test and numbers before lunch. The "
    "speakers are evaluating the RFC 9264 linkset for their docs."
)


class _ActivityRecorder:
    def __init__(self) -> None:
        self.records: list[TimedRecord] = []
        self._changed = asyncio.Condition()

    async def add(self, record: dict[str, object]) -> None:
        async with self._changed:
            self.records.append(
                TimedRecord(
                    sequence=len(self.records) + 1,
                    elapsed_ms=len(self.records),
                    record=record,
                )
            )
            self._changed.notify_all()

    async def wait_for_count(
        self,
        phase: str,
        count: int,
    ) -> None:
        async with asyncio.timeout(1):
            async with self._changed:
                await self._changed.wait_for(
                    lambda: sum(
                        record.record.get("phase") == phase for record in self.records
                    )
                    >= count
                )


class _FinalFlowBarrier:
    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.arrived = 0
        self.all_arrived = asyncio.Event()

    async def wait(self) -> None:
        self.arrived += 1
        if self.arrived == self.expected:
            self.all_arrived.set()
        await self.all_arrived.wait()


@dataclass(slots=True)
class _ControlledProvider:
    kind: str
    recorder: _ActivityRecorder
    barrier: _FinalFlowBarrier
    final_revision: int
    requests: list[DraftRequest | InsightRequest] = field(default_factory=lambda: [])

    async def draft(self, request: DraftRequest) -> DraftResult:
        self.requests.append(request)
        text = await self._generate(request.generation_id, request.context_revision)
        return DraftResult(
            generation_id=request.generation_id,
            trigger_turn_id=request.trigger_turn_id,
            context_revision=request.context_revision,
            status=GenerationStatus.COMPLETED,
            text=text,
        )

    async def insight(self, request: InsightRequest) -> InsightResult:
        self.requests.append(request)
        text = await self._generate(request.generation_id, request.context_revision)
        return InsightResult(
            generation_id=request.generation_id,
            kind=request.kind,
            trigger_turn_id=request.trigger_turn_id,
            context_revision=request.context_revision,
            status=GenerationStatus.COMPLETED,
            text=text,
        )

    async def _generate(self, flow_id: str, context_revision: int) -> str:
        await self.recorder.add(
            {
                "kind": "provider_activity",
                "phase": "request_started",
                "flow_id": flow_id,
                "generation_id": flow_id,
                "context_revision": context_revision,
                "output_kind": self.kind,
                "model": f"mock-{self.kind}",
            }
        )
        try:
            if context_revision in {2, 6}:
                phase = "tool_started" if context_revision == 6 else "output_streaming"
                await self.recorder.add(
                    {
                        "kind": "provider_activity",
                        "phase": phase,
                        "flow_id": flow_id,
                    }
                )
                await asyncio.Event().wait()
            if context_revision == self.final_revision:
                await self.barrier.wait()
            await self.recorder.add(
                {
                    "kind": "provider_activity",
                    "phase": "output_streaming",
                    "flow_id": flow_id,
                }
            )
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            await self.recorder.add(
                {
                    "kind": "provider_activity",
                    "phase": "request_cancelled",
                    "flow_id": flow_id,
                }
            )
            raise
        await self.recorder.add(
            {
                "kind": "provider_activity",
                "phase": "request_completed",
                "flow_id": flow_id,
            }
        )
        if self.kind == "draft":
            return _FINAL_DRAFT
        if self.kind == "commentary":
            return _FINAL_COMMENTARY
        return _FINAL_SUMMARY


def test_slang_conversation_runs_parallel_and_recovers_from_interruptions() -> None:
    async def run() -> None:
        scenario = load_scenario(Path(_SCENARIO_PATH))
        recorder = _ActivityRecorder()
        barrier = _FinalFlowBarrier(expected=3)
        providers = {
            kind: _ControlledProvider(kind, recorder, barrier, len(scenario.turns))
            for kind in ("draft", "commentary", "summary")
        }
        coordinator = ConversationCoordinator(
            providers["draft"],
            commentary_provider=providers["commentary"],
            summary_provider=providers["summary"],
        )
        role_revisions = {SpeakerRole.USER: 0, SpeakerRole.REMOTE: 0}
        timeline_records: list[TimedRecord] = []
        interruption_pending = False
        try:
            for index, turn in enumerate(scenario.turns, start=1):
                role = SpeakerRole(turn.speaker_role)
                role_revisions[role] += 1
                if interruption_pending:
                    await recorder.add(
                        {
                            "kind": "evaluation_interruption",
                            "turn_id": turn.identifier,
                            "context_revision": index,
                        }
                    )
                    interruption_pending = False
                update = await coordinator.ingest(
                    _transcript_event(
                        turn.identifier,
                        role,
                        role_revisions[role],
                        turn.text,
                    )
                )
                if update.timeline is not None:
                    timeline_records.append(
                        TimedRecord(
                            sequence=index,
                            elapsed_ms=index,
                            record={
                                "kind": "timeline",
                                "turn_id": update.timeline.turn_id,
                                "speaker_role": update.timeline.speaker_role.value,
                                "text": update.timeline.text,
                            },
                        )
                    )
                await asyncio.sleep(0)
                if index == 2:
                    await recorder.wait_for_count("output_streaming", 1)
                    interruption_pending = True
                if index == 6:
                    await recorder.wait_for_count("tool_started", 1)
                    interruption_pending = True

            draft = await coordinator.wait_for_idle()
            assert draft is not None
            assert draft.status is GenerationStatus.COMPLETED
            insights = coordinator.drain_completed_insights()
            final_insights = {
                insight.kind: insight
                for insight in insights
                if insight.context_revision == len(scenario.turns)
            }
            assert set(final_insights) == {InsightKind.COMMENTARY, InsightKind.SUMMARY}
            assert barrier.arrived == 3

            final_records = list(recorder.records)
            next_sequence = len(final_records)
            elapsed_ms = final_records[-1].elapsed_ms + 1
            for timeline in timeline_records:
                next_sequence += 1
                final_records.append(
                    TimedRecord(next_sequence, elapsed_ms, timeline.record)
                )
                elapsed_ms += 1
            outputs = (
                ("draft", draft.generation_id, draft.text),
                (
                    "commentary",
                    final_insights[InsightKind.COMMENTARY].generation_id,
                    final_insights[InsightKind.COMMENTARY].text,
                ),
                (
                    "summary",
                    final_insights[InsightKind.SUMMARY].generation_id,
                    final_insights[InsightKind.SUMMARY].text,
                ),
            )
            for kind, generation_id, text in outputs:
                next_sequence += 1
                final_records.append(
                    TimedRecord(
                        next_sequence,
                        elapsed_ms,
                        {
                            "kind": kind,
                            "generation_id": generation_id,
                            "status": "completed",
                            "text": text,
                        },
                    )
                )
                elapsed_ms += 1

            report = evaluate_observations(tuple(final_records), scenario)
            assert report.passed is True
            assert report.timeline_turn_count == 8
            assert report.maximum_concurrent_provider_flows >= 3
            assert report.overlapping_provider_pairs >= 3
            assert report.stale_provider_output_count == 0
            assert report.interruption_to_cancellation_latencies is not None
            assert report.interruption_to_cancellation_latencies.count == 2
            assert report.cancellation_to_replacement_latencies is not None
            assert report.cancellation_to_replacement_latencies.count >= 2
            assert len(report.replacement_context_revisions) >= 2
            assert (
                sum(
                    record.record.get("phase") == "request_cancelled"
                    for record in recorder.records
                )
                >= 4
            )
            final_request = providers["draft"].requests[-1]
            assert final_request.context_revision == len(scenario.turns)
            assert len(final_request.transcript.lines) == len(scenario.turns)
            assert "Thursday, not Friday" in " ".join(
                line.text for line in final_request.transcript.lines
            )
        finally:
            await coordinator.stop()

    asyncio.run(run())


def _transcript_event(
    identifier: str,
    speaker_role: SpeakerRole,
    revision: int,
    text: str,
) -> TranscriptEvent:
    return TranscriptEvent(
        session_id="conversation-evaluation",
        stream_id=identifier,
        utterance_id=identifier,
        revision=revision,
        speaker_role=speaker_role,
        source_event_type=TranscriptEventType.FINAL,
        asr_model="mock-evaluation-asr",
        text=text,
        is_final=True,
        audio_seconds=1.0,
        words=(),
    )
