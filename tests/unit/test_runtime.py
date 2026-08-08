from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from two_x_brainz.audio_selection import (
    AudioSelection,
    AudioSelectionSetup,
)
from two_x_brainz.capture import (
    CaptureStats,
    InterStreamDriftMonitor,
    InterStreamDriftStats,
)
from two_x_brainz.config import Settings
from two_x_brainz.constants import DEFAULT_FRAME_BYTES
from two_x_brainz.contracts import (
    ASRStreamStats,
    DraftRequest,
    DraftResult,
    GenerationStatus,
    InsightRequest,
    InsightResult,
    SpeakerRole,
)
from two_x_brainz.coordinator import ConversationCoordinator
from two_x_brainz.errors import (
    CaptureError,
    ProtocolError,
    RemoteServiceError,
)
from two_x_brainz.provider_selection import ProviderAssignment
from two_x_brainz.runtime import (
    _aigate_client,  # pyright: ignore[reportPrivateUsage]
    _gated_frames,  # pyright: ignore[reportPrivateUsage]
    _initial_provider_selection,  # pyright: ignore[reportPrivateUsage]
    _supervise_audio_channel,  # pyright: ignore[reportPrivateUsage]
    _write_provider_activity_event,  # pyright: ignore[reportPrivateUsage]
    asr_segment_stream_id,
    handle_control_line,
    session_error_reason,
    write_asr_segment_event,
    write_asr_stats_event,
    write_capture_drift_event,
    write_capture_stats_event,
    write_draft_event,
    write_session_error_event,
)
from two_x_brainz.session_controls import SessionController
from two_x_brainz.talkies import TalkiesStreamConfig

_FIXTURE_FRAME = bytes(DEFAULT_FRAME_BYTES)


class RuntimeOutputTests(unittest.TestCase):
    def test_code_defaults_use_independent_flow_models(self) -> None:
        settings = _settings("fallback-model")
        settings = replace(
            settings,
            aigate_reply_model="cerebras-model",
            aigate_coach_model="coach-model",
            aigate_summary_model="summary-model",
            aigate_reply_reasoning_effort="minimal",
            aigate_coach_reasoning_effort="low",
            aigate_summary_reasoning_effort="high",
        )

        selection = _initial_provider_selection(
            settings,
            (
                "fallback-model",
                "cerebras-model",
                "coach-model",
                "summary-model",
            ),
        )

        self.assertEqual(selection.draft.model, "cerebras-model")
        self.assertEqual(selection.draft.reasoning_effort, "minimal")
        self.assertEqual(selection.commentary.model, "coach-model")
        self.assertEqual(selection.commentary.reasoning_effort, "low")
        self.assertEqual(selection.summary.model, "summary-model")
        self.assertEqual(selection.summary.reasoning_effort, "high")

    def test_web_research_is_enabled_only_for_the_reply_client(self) -> None:
        settings = _settings("model-a", web_research_enabled=True)
        assignment = ProviderAssignment("model-a", "none")

        reply = _aigate_client(
            settings,
            assignment,
            web_research_enabled=True,
        )
        story = _aigate_client(
            settings,
            assignment,
            web_research_enabled=False,
        )

        self.assertTrue(reply.web_research_enabled)
        self.assertFalse(story.web_research_enabled)

    def test_stream_snapshots_log_counts_without_raw_text_at_debug(self) -> None:
        with (
            self.assertLogs("two_x_brainz.runtime", level="DEBUG") as captured,
            patch("builtins.print"),
        ):
            _write_provider_activity_event(
                {
                    "phase": "reasoning_streaming",
                    "flow_id": "flow-a",
                    "output_kind": "summary",
                    "model": "model-a",
                    "reasoning": "private cumulative reasoning",
                }
            )
            _write_provider_activity_event(
                {
                    "phase": "stream_completed",
                    "flow_id": "flow-a",
                    "output_kind": "summary",
                    "model": "model-a",
                    "reasoning": "final reasoning",
                    "output": "final summary",
                }
            )

        self.assertEqual(
            [record.getMessage() for record in captured.records],
            ["live provider stream event", "live runtime event"],
        )
        self.assertEqual(
            captured.records[0].__dict__["reasoning_characters"],
            len("private cumulative reasoning"),
        )
        self.assertNotIn("event", captured.records[0].__dict__)
        self.assertEqual(
            captured.records[1].__dict__["event"]["output"],
            "final summary",
        )

    def test_paused_gate_does_not_open_capture_before_start(self) -> None:
        opened = False

        async def frames() -> AsyncIterator[bytes]:
            nonlocal opened
            opened = True
            yield _FIXTURE_FRAME

        async def exercise() -> None:
            controller = SessionController(start_paused=True)
            gated = aiter(_gated_frames(frames(), controller))

            async def next_frame() -> bytes:
                return await anext(gated)

            pending = asyncio.create_task(next_frame())
            await asyncio.sleep(0)
            self.assertFalse(opened)
            self.assertTrue(controller.resume())
            self.assertEqual(await pending, _FIXTURE_FRAME)

        asyncio.run(exercise())

    def test_pause_stops_requesting_additional_capture_frames(self) -> None:
        requested = 0

        async def frames() -> AsyncIterator[bytes]:
            nonlocal requested
            while True:
                requested += 1
                yield _FIXTURE_FRAME

        async def exercise() -> None:
            controller = SessionController()
            gated = aiter(_gated_frames(frames(), controller))
            await anext(gated)
            self.assertEqual(requested, 1)
            self.assertTrue(controller.pause())

            async def next_frame() -> bytes:
                return await anext(gated)

            pending = asyncio.create_task(next_frame())
            await asyncio.sleep(0)
            self.assertEqual(requested, 1)
            self.assertTrue(controller.resume())
            await pending
            self.assertEqual(requested, 2)

        asyncio.run(exercise())

    def test_one_failed_audio_channel_does_not_cancel_its_peer(self) -> None:
        calls = {SpeakerRole.USER: 0, SpeakerRole.REMOTE: 0}

        async def consume_stream(**kwargs: object) -> None:
            role = kwargs["speaker_role"]
            assert isinstance(role, SpeakerRole)
            calls[role] += 1
            if role is SpeakerRole.USER:
                raise CaptureError("disconnected")
            await asyncio.sleep(0)

        async def exercise() -> None:
            controller = SessionController()
            setup = _audio_setup("mic", "system")
            config = TalkiesStreamConfig(
                url="ws://talkies.test/stream",
                model="test-model",
                token=None,
            )
            coordinator = ConversationCoordinator(_LiveProvider())
            with (
                patch("two_x_brainz.runtime._consume_stream", new=consume_stream),
                patch("two_x_brainz.runtime._AUDIO_CHANNEL_RETRY_SECONDS", 0.001),
            ):
                microphone = asyncio.create_task(
                    _supervise_audio_channel(
                        stream_config_factory=lambda: config,
                        coordinator=coordinator,
                        session_id="session",
                        stream_id="microphone",
                        speaker_role=SpeakerRole.USER,
                        audio_setup=setup,
                        controller=controller,
                        drift_monitor=InterStreamDriftMonitor(),
                    )
                )
                remote = asyncio.create_task(
                    _supervise_audio_channel(
                        stream_config_factory=lambda: config,
                        coordinator=coordinator,
                        session_id="session",
                        stream_id="system",
                        speaker_role=SpeakerRole.REMOTE,
                        audio_setup=setup,
                        controller=controller,
                        drift_monitor=InterStreamDriftMonitor(),
                    )
                )
                while calls[SpeakerRole.USER] < 2 or calls[SpeakerRole.REMOTE] < 2:
                    await asyncio.sleep(0.001)
                controller.stop()
                microphone.cancel()
                remote.cancel()
                await asyncio.gather(microphone, remote, return_exceptions=True)
                await coordinator.stop()

        asyncio.run(exercise())
        self.assertGreaterEqual(calls[SpeakerRole.USER], 2)
        self.assertGreaterEqual(calls[SpeakerRole.REMOTE], 2)

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

    def test_removed_reply_action_emits_only_fixed_control_error(self) -> None:
        controller = SessionController()
        coordinator = ConversationCoordinator(_LiveProvider())

        with patch("builtins.print") as print_mock:
            stopped = asyncio.run(
                handle_control_line("accept", controller, coordinator)
            )

        self.assertFalse(stopped)
        self.assertEqual(controller.state.value, "running")
        record = json.loads(print_mock.call_args.args[0])
        self.assertEqual(
            record,
            {
                "schema_version": 1,
                "kind": "control_error",
                "message": "use pause, resume, or stop",
            },
        )

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


def _selection(mic_node: str, system_node: str) -> AudioSelection:
    return AudioSelection(
        mic_node=mic_node,
        system_node=system_node,
        mic_label=mic_node,
        system_label=system_node,
        system_capture_sink=True,
    )


def _settings(
    flow_model: str,
    *,
    web_research_enabled: bool = False,
) -> Settings:
    return Settings(
        talkies_ws_url="ws://aigate.test/talkies/v1/audio/transcriptions/stream",
        talkies_model="test-asr-model",
        talkies_token=None,
        aigate_url="https://aigate.test/v1",
        aigate_reply_model=flow_model,
        aigate_coach_model=flow_model,
        aigate_summary_model=flow_model,
        aigate_token=None,
        log_level="INFO",
        log_file=Path("/tmp/2xbrainz-test.log"),
        aigate_reply_reasoning_effort="medium",
        aigate_coach_reasoning_effort="medium",
        aigate_summary_reasoning_effort="medium",
        web_research_enabled=web_research_enabled,
    )


def _audio_setup(mic_node: str, system_node: str) -> AudioSelectionSetup:
    return AudioSelectionSetup(
        microphones=(),
        system_monitors=(),
        selection=_selection(mic_node, system_node),
    )


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
