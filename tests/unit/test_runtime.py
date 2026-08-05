from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

from two_x_brainz.audio_selection import (
    AudioSelection,
    AudioSelectionSetup,
    AudioSelectionStore,
)
from two_x_brainz.capture import (
    CaptureStats,
    InterStreamDriftMonitor,
    InterStreamDriftStats,
)
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
from two_x_brainz.runtime import (
    _gated_frames,  # pyright: ignore[reportPrivateUsage]
    _supervise_audio_channel,  # pyright: ignore[reportPrivateUsage]
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
                        stream_config=config,
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
                        stream_config=config,
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


def _audio_setup(mic_node: str, system_node: str) -> AudioSelectionSetup:
    return AudioSelectionSetup(
        store=AudioSelectionStore(Path("/tmp/2xbrainz-runtime-audio.json")),
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
