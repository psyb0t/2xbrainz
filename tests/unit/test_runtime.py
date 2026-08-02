from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from two_x_brainz.capture import CaptureStats, InterStreamDriftStats
from two_x_brainz.config import AIGateMode, Settings
from two_x_brainz.constants import (
    DEFAULT_FRAME_BYTES,
    SPEECH_ACTIVITY_SAMPLE_THRESHOLD,
)
from two_x_brainz.contracts import (
    ASRStreamStats,
    AudioFrame,
    DraftOutcome,
    DraftOutcomeAction,
    DraftRequest,
    DraftResult,
    GenerationStatus,
    InsightRequest,
    InsightResult,
    SpeakerRole,
    TranscriptEvent,
    TranscriptEventType,
)
from two_x_brainz.coordinator import ConversationCoordinator
from two_x_brainz.errors import (
    CaptureError,
    ConfigurationError,
    ProtocolError,
    RemoteServiceError,
)
from two_x_brainz.runtime import (
    asr_segment_stream_id,
    run_live,
    session_error_reason,
    write_asr_segment_event,
    write_asr_stats_event,
    write_capture_drift_event,
    write_capture_stats_event,
    write_draft_action_event,
    write_draft_event,
    write_session_error_event,
)
from two_x_brainz.session_controls import DraftAction, SessionController

_AUDIBLE_PCM_SAMPLE = SPEECH_ACTIVITY_SAMPLE_THRESHOLD + 1
_AUDIBLE_FRAME = _AUDIBLE_PCM_SAMPLE.to_bytes(2, "little", signed=True) * (
    DEFAULT_FRAME_BYTES // 2
)


class RuntimeOutputTests(unittest.TestCase):
    def test_live_requires_an_aigate_model_before_constructing_capture(self) -> None:
        settings = _settings(aigate_model=None)

        with (
            self.assertRaisesRegex(ConfigurationError, "AIGATE_MODEL"),
            patch("two_x_brainz.runtime.PipeWireSource") as capture_source,
            patch("two_x_brainz.runtime.TalkiesClient") as talkies_client,
        ):
            asyncio.run(run_live(settings, "mic", "system"))

        capture_source.assert_not_called()
        talkies_client.assert_not_called()

    def test_live_rejects_an_unavailable_talkies_model_before_capture(self) -> None:
        settings = _settings(aigate_model="draft-model")
        aigate_preflight = Mock()
        aigate_preflight.verify_configured_model = AsyncMock()
        preflight_client = Mock()
        preflight_client.verify_configured_model = AsyncMock(
            side_effect=RemoteServiceError("unavailable")
        )

        with (
            self.assertRaisesRegex(RemoteServiceError, "unavailable"),
            patch(
                "two_x_brainz.runtime.TalkiesClient",
                return_value=preflight_client,
            ) as talkies_client,
            patch(
                "two_x_brainz.runtime.AIGateClient",
                return_value=aigate_preflight,
            ) as aigate_client,
            patch("two_x_brainz.runtime.PipeWireSource") as capture_source,
        ):
            asyncio.run(run_live(settings, "mic", "system"))

        aigate_client.assert_called_once()
        aigate_preflight.verify_configured_model.assert_awaited_once()
        talkies_client.assert_called_once()
        preflight_client.verify_configured_model.assert_awaited_once()
        capture_source.assert_not_called()

    def test_live_rejects_an_unavailable_aigate_model_before_talkies_or_capture(
        self,
    ) -> None:
        settings = _settings(aigate_model="draft-model")
        aigate_preflight = Mock()
        aigate_preflight.verify_configured_model = AsyncMock(
            side_effect=RemoteServiceError("unavailable")
        )

        with (
            self.assertRaisesRegex(RemoteServiceError, "unavailable"),
            patch(
                "two_x_brainz.runtime.AIGateClient",
                return_value=aigate_preflight,
            ) as aigate_client,
            patch("two_x_brainz.runtime.TalkiesClient") as talkies_client,
            patch("two_x_brainz.runtime.PipeWireSource") as capture_source,
        ):
            asyncio.run(run_live(settings, "mic", "system"))

        aigate_client.assert_called_once()
        aigate_preflight.verify_configured_model.assert_awaited_once()
        talkies_client.assert_not_called()
        capture_source.assert_not_called()

    def test_live_reaches_capture_setup_after_aigate_preflight(self) -> None:
        settings = _settings(aigate_model="draft-model")
        aigate_preflight = Mock()
        aigate_preflight.verify_configured_model = AsyncMock()
        preflight_client = Mock()
        preflight_client.verify_configured_model = AsyncMock()
        preflight_client.warm_configured_model = AsyncMock()

        with (
            self.assertRaises(ExceptionGroup) as error_context,
            patch(
                "two_x_brainz.runtime.TalkiesClient",
                return_value=preflight_client,
            ) as talkies_client,
            patch(
                "two_x_brainz.runtime.AIGateClient",
                return_value=aigate_preflight,
            ) as aigate_client,
            patch(
                "two_x_brainz.runtime.PipeWireSource",
                side_effect=CaptureError("unavailable"),
            ) as capture_source,
            patch("two_x_brainz.runtime.print"),
        ):
            asyncio.run(run_live(settings, "mic", "system"))

        self.assertIsInstance(error_context.exception.exceptions[0], CaptureError)
        aigate_client.assert_called_once()
        aigate_preflight.verify_configured_model.assert_awaited_once()
        self.assertEqual(talkies_client.call_count, 2)
        preflight_client.verify_configured_model.assert_awaited_once()
        preflight_client.warm_configured_model.assert_awaited_once()
        capture_source.assert_called_once_with("mic")

    def test_live_rejects_a_failed_talkies_warmup_before_capture(self) -> None:
        settings = _settings(aigate_model="draft-model")
        aigate_preflight = Mock()
        aigate_preflight.verify_configured_model = AsyncMock()
        preflight_client = Mock()
        preflight_client.verify_configured_model = AsyncMock()
        preflight_client.warm_configured_model = AsyncMock(
            side_effect=RemoteServiceError("warm-up failed")
        )

        with (
            self.assertRaisesRegex(RemoteServiceError, "warm-up failed"),
            patch(
                "two_x_brainz.runtime.AIGateClient",
                return_value=aigate_preflight,
            ),
            patch(
                "two_x_brainz.runtime.TalkiesClient",
                return_value=preflight_client,
            ),
            patch("two_x_brainz.runtime.PipeWireSource") as capture_source,
        ):
            asyncio.run(run_live(settings, "mic", "system"))

        preflight_client.verify_configured_model.assert_awaited_once()
        preflight_client.warm_configured_model.assert_awaited_once()
        capture_source.assert_not_called()

    def test_live_capture_failures_emit_one_fixed_session_error(self) -> None:
        settings = _settings(aigate_model="draft-model")
        client = _FrameDrainingTalkiesClient()

        with (
            self.assertRaises(ExceptionGroup),
            patch("two_x_brainz.runtime.AIGateClient", return_value=client),
            patch("two_x_brainz.runtime.TalkiesClient", return_value=client),
            patch("two_x_brainz.runtime.PipeWireSource", _CaptureFailingSource),
            patch(
                "two_x_brainz.runtime._read_session_controls",
                _wait_for_cancellation,
            ),
            patch("builtins.print") as print_mock,
        ):
            asyncio.run(run_live(settings, "mic", "system"))

        records = [
            json.loads(call.args[0]) for call in print_mock.call_args_list if call.args
        ]
        session_errors = [
            record for record in records if record.get("kind") == "session_error"
        ]
        self.assertEqual(
            session_errors,
            [
                {
                    "schema_version": 1,
                    "kind": "session_error",
                    "reason": "capture_unavailable",
                }
            ],
        )

    def test_live_orchestrates_two_completed_streams_before_local_stop(self) -> None:
        settings = _settings(aigate_model="draft-model")
        harness = _LiveHarness()
        provider = _LiveProvider()
        talkies_client = _LiveTalkiesClient(harness)

        with (
            patch("two_x_brainz.runtime.AIGateClient", return_value=provider),
            patch(
                "two_x_brainz.runtime.TalkiesClient",
                return_value=talkies_client,
            ) as talkies_constructor,
            patch(
                "two_x_brainz.runtime.PipeWireSource",
                side_effect=_LiveCaptureSource,
            ) as capture_source,
            patch(
                "two_x_brainz.runtime._read_session_controls",
                harness.stop_after_completion,
            ),
            patch("builtins.print") as print_mock,
        ):
            asyncio.run(run_live(settings, "mic-node", "system-node"))

        self.assertEqual(talkies_constructor.call_count, 3)
        self.assertEqual(capture_source.call_count, 2)
        records = [
            json.loads(call.args[0]) for call in print_mock.call_args_list if call.args
        ]
        self.assertEqual(
            {
                record["speaker_role"]
                for record in records
                if record.get("kind") == "transcript"
            },
            {"user", "remote"},
        )
        self.assertEqual(
            {
                record["speaker_role"]
                for record in records
                if record.get("kind") == "asr_stats"
            },
            {"user", "remote"},
        )
        self.assertEqual(
            {
                record["speaker_role"]
                for record in records
                if record.get("kind") == "capture_stats"
            },
            {"user", "remote"},
        )
        self.assertTrue(
            any(
                record.get("kind") == "draft" and record.get("status") == "completed"
                for record in records
            )
        )
        self.assertTrue(any(record.get("kind") == "summary" for record in records))
        self.assertEqual(
            sum(record.get("kind") == "capture_drift" for record in records),
            1,
        )

    def test_non_completed_draft_record_has_no_visible_text(self) -> None:
        draft = DraftResult(
            generation_id="generation-id",
            trigger_turn_id="turn-id",
            context_revision=3,
            status=GenerationStatus.FAILED,
            text="",
        )

        with patch("builtins.print") as print_mock:
            write_draft_event(draft)

        record = json.loads(print_mock.call_args.args[0])
        self.assertEqual(
            record,
            {
                "schema_version": 1,
                "kind": "draft",
                "generation_id": "generation-id",
                "trigger_turn_id": "turn-id",
                "status": "failed",
                "text": "",
                "context_revision": 3,
            },
        )

    def test_session_error_records_only_a_fixed_reason(self) -> None:
        with patch("builtins.print") as print_mock:
            write_session_error_event("asr_unavailable")

        record = json.loads(print_mock.call_args.args[0])
        self.assertEqual(
            record,
            {
                "schema_version": 1,
                "kind": "session_error",
                "reason": "asr_unavailable",
            },
        )

    def test_session_error_reason_maps_expected_boundaries(self) -> None:
        self.assertEqual(
            session_error_reason(CaptureError("private")), "capture_unavailable"
        )
        self.assertEqual(
            session_error_reason(ProtocolError("private")), "asr_protocol_error"
        )
        self.assertEqual(
            session_error_reason(RemoteServiceError("private")),
            "asr_unavailable",
        )

    def test_draft_outcome_record_has_a_fixed_safe_shape(self) -> None:
        outcome = DraftOutcome(
            action=DraftOutcomeAction.ACCEPTED,
            draft=DraftResult(
                generation_id="generation-id",
                trigger_turn_id="turn-id",
                context_revision=3,
                status=GenerationStatus.COMPLETED,
                text="private draft text",
            ),
        )

        with patch("builtins.print") as print_mock:
            write_draft_action_event(DraftAction.ACCEPT, True, outcome)

        record = json.loads(print_mock.call_args.args[0])
        self.assertEqual(
            record,
            {
                "schema_version": 1,
                "kind": "draft_action",
                "action": "accept",
                "changed": True,
                "outcome": "accepted",
                "generation_id": "generation-id",
                "trigger_turn_id": "turn-id",
                "context_revision": 3,
            },
        )

    def test_non_outcome_record_has_null_identifiers(self) -> None:
        with patch("builtins.print") as print_mock:
            write_draft_action_event(DraftAction.EDIT, True, None)

        record = json.loads(print_mock.call_args.args[0])
        self.assertEqual(record["outcome"], None)
        self.assertEqual(record["generation_id"], None)
        self.assertEqual(record["trigger_turn_id"], None)
        self.assertEqual(record["context_revision"], None)

    def test_asr_statistics_record_has_a_fixed_safe_shape(self) -> None:
        event = ASRStreamStats(
            session_id="session-id",
            stream_id="stream-id",
            speaker_role=SpeakerRole.REMOTE,
            asr_model="nemotron-3.5-asr-0.6b",
            audio_seconds=1.28,
            frames=64,
            canceled=False,
        )

        with patch("builtins.print") as print_mock:
            write_asr_stats_event(event)

        record = json.loads(print_mock.call_args.args[0])
        self.assertEqual(
            record,
            {
                "schema_version": 1,
                "kind": "asr_stats",
                "speaker_role": "remote",
                "model": "nemotron-3.5-asr-0.6b",
                "audio_seconds": 1.28,
                "frames": 64,
                "canceled": False,
            },
        )

    def test_asr_segment_record_has_a_fixed_safe_shape(self) -> None:
        with patch("builtins.print") as print_mock:
            write_asr_segment_event(SpeakerRole.USER, 2, "silence")

        record = json.loads(print_mock.call_args.args[0])
        self.assertEqual(
            record,
            {
                "schema_version": 1,
                "kind": "asr_segment",
                "speaker_role": "user",
                "segment_index": 2,
                "boundary": "silence",
            },
        )

    def test_rotated_asr_segments_receive_distinct_turn_stream_ids(self) -> None:
        self.assertEqual(
            (
                asr_segment_stream_id("microphone", 1),
                asr_segment_stream_id("microphone", 2),
            ),
            ("microphone:segment:1", "microphone:segment:2"),
        )

    def test_capture_statistics_record_has_a_fixed_safe_shape(self) -> None:
        stats = CaptureStats(
            speaker_role=SpeakerRole.USER,
            frame_count=42,
            audio_seconds=0.84,
            gap_count=1,
            max_gap_seconds=0.06,
        )

        with patch("builtins.print") as print_mock:
            write_capture_stats_event(stats)

        record = json.loads(print_mock.call_args.args[0])
        self.assertEqual(
            record,
            {
                "schema_version": 1,
                "kind": "capture_stats",
                "speaker_role": "user",
                "frame_count": 42,
                "audio_seconds": 0.84,
                "gap_count": 1,
                "max_gap_seconds": 0.06,
            },
        )

    def test_capture_drift_record_has_a_fixed_safe_shape(self) -> None:
        stats = InterStreamDriftStats(
            comparison_count=42,
            max_abs_drift_seconds=0.03,
            unmatched_frame_count=2,
        )

        with patch("builtins.print") as print_mock:
            write_capture_drift_event(stats)

        record = json.loads(print_mock.call_args.args[0])
        self.assertEqual(
            record,
            {
                "schema_version": 1,
                "kind": "capture_drift",
                "comparison_count": 42,
                "max_abs_drift_seconds": 0.03,
                "unmatched_frame_count": 2,
            },
        )


def _settings(aigate_model: str | None) -> Settings:
    return Settings(
        talkies_ws_url="ws://talkies:8000/v1/audio/transcriptions/stream",
        talkies_model="fixture-model",
        talkies_token=None,
        aigate_url="http://aigate:4000/v1",
        aigate_mode=AIGateMode.LOCAL,
        aigate_model=aigate_model,
        aigate_token=None,
        log_level="INFO",
        log_file=Path("/tmp/2xbrainz-test.log"),
    )


class _CaptureFailingSource:
    def __init__(self, node_id: str) -> None:
        self._node_id = node_id

    async def frames(self) -> AsyncIterator[bytes]:
        if self._node_id:
            raise CaptureError("private capture failure")
        yield b""


class _FrameDrainingTalkiesClient:
    async def verify_configured_model(self) -> None:
        return None

    async def warm_configured_model(self) -> None:
        return None

    async def transcribe(
        self,
        *,
        session_id: str,
        stream_id: str,
        speaker_role: SpeakerRole,
        frames: AsyncIterable[AudioFrame],
    ) -> AsyncIterator[TranscriptEvent | ASRStreamStats]:
        async for _ in frames:
            continue
        yield ASRStreamStats(
            session_id=session_id,
            stream_id=stream_id,
            speaker_role=speaker_role,
            asr_model="fixture-model",
            audio_seconds=0.0,
            frames=0,
            canceled=False,
        )


async def _wait_for_cancellation(*_: object) -> None:
    await asyncio.Event().wait()


class _LiveHarness:
    def __init__(self) -> None:
        self.user_finalized = asyncio.Event()
        self.streams_completed = asyncio.Event()
        self._completed_stream_count = 0

    def mark_stream_complete(self) -> None:
        self._completed_stream_count += 1
        if self._completed_stream_count == 2:
            self.streams_completed.set()

    async def stop_after_completion(
        self,
        controller: object,
        coordinator: ConversationCoordinator,
    ) -> None:
        await self.streams_completed.wait()
        await coordinator.wait_for_idle()
        await asyncio.sleep(0)
        assert isinstance(controller, SessionController)
        controller.stop()


class _LiveProvider:
    async def verify_configured_model(self) -> None:
        return None

    async def draft(self, request: DraftRequest) -> DraftResult:
        return DraftResult(
            generation_id=request.generation_id,
            trigger_turn_id=request.trigger_turn_id,
            context_revision=request.context_revision,
            status=GenerationStatus.COMPLETED,
            text="A concise spoken reply.",
        )

    async def insight(self, request: InsightRequest) -> InsightResult:
        return InsightResult(
            generation_id=request.generation_id,
            kind=request.kind,
            trigger_turn_id=request.trigger_turn_id,
            context_revision=request.context_revision,
            status=GenerationStatus.COMPLETED,
            text="A concise running summary.",
        )


class _LiveCaptureSource:
    def __init__(self, node_id: str) -> None:
        self._node_id = node_id

    async def frames(self) -> AsyncIterator[bytes]:
        if not self._node_id:
            return
        yield _AUDIBLE_FRAME


class _LiveTalkiesClient:
    def __init__(self, harness: _LiveHarness) -> None:
        self._harness = harness

    async def verify_configured_model(self) -> None:
        return None

    async def warm_configured_model(self) -> None:
        return None

    async def transcribe(
        self,
        *,
        session_id: str,
        stream_id: str,
        speaker_role: SpeakerRole,
        frames: AsyncIterable[AudioFrame],
    ) -> AsyncIterator[TranscriptEvent | ASRStreamStats]:
        frame_count = 0
        async for _ in frames:
            frame_count += 1
        if speaker_role is SpeakerRole.REMOTE:
            await self._harness.user_finalized.wait()
        yield TranscriptEvent(
            session_id=session_id,
            stream_id=stream_id,
            utterance_id=f"{speaker_role.value}-utterance",
            revision=1,
            speaker_role=speaker_role,
            source_event_type=TranscriptEventType.FINAL,
            asr_model="fixture-model",
            text=f"{speaker_role.value} fixture turn",
            is_final=True,
            audio_seconds=0.02,
            words=(),
        )
        if speaker_role is SpeakerRole.USER:
            self._harness.user_finalized.set()
        yield ASRStreamStats(
            session_id=session_id,
            stream_id=stream_id,
            speaker_role=speaker_role,
            asr_model="fixture-model",
            audio_seconds=0.02,
            frames=frame_count,
            canceled=False,
        )
        self._harness.mark_stream_complete()
