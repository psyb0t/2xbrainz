from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

from two_x_brainz.audio_selection import (
    AudioSelection,
    AudioSelectionSetup,
    AudioSelectionStore,
)
from two_x_brainz.capture import CaptureStats, InterStreamDriftStats
from two_x_brainz.config import Settings
from two_x_brainz.constants import DEFAULT_FRAME_BYTES
from two_x_brainz.contracts import (
    ASRStreamStats,
    AudioFrame,
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
    handle_control_line,
    presentation_for_output,
    run_live,
    session_error_reason,
    write_asr_segment_event,
    write_asr_stats_event,
    write_capture_drift_event,
    write_capture_stats_event,
    write_draft_event,
    write_session_error_event,
)
from two_x_brainz.session_controls import SessionController
from two_x_brainz.terminal import LiveTerminal
from two_x_brainz.web import WebConsole

_FIXTURE_FRAME = bytes(DEFAULT_FRAME_BYTES)


class RuntimeOutputTests(unittest.TestCase):
    def test_web_output_constructs_the_gradio_presentation_adapter(self) -> None:
        terminal = LiveTerminal(log_file="/tmp/2xbrainz-web.log")

        presentation = presentation_for_output("web", terminal, 9000)

        self.assertIsInstance(presentation, WebConsole)
        web_console = cast(WebConsole, presentation)
        self.assertIs(web_console.state, terminal)
        self.assertEqual(web_console.port, 9000)

    def test_unknown_output_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported live output mode"):
            presentation_for_output(
                "unknown",
                LiveTerminal(log_file="/tmp/log"),
                9000,
            )

    def test_live_closes_the_terminal_when_setup_is_interrupted(self) -> None:
        settings = _settings(aigate_model="draft-model")
        terminal = _TerminalAudit(_selection("mic", "system"))
        terminal.open = AsyncMock(side_effect=asyncio.CancelledError())
        terminal.close = AsyncMock()

        with (
            self.assertRaises(asyncio.CancelledError),
            patch("two_x_brainz.runtime.LiveTerminal", return_value=terminal),
        ):
            asyncio.run(run_live(settings, _audio_setup("mic", "system")))

        terminal.close.assert_awaited_once()

    def test_live_requires_an_aigate_model_before_constructing_capture(self) -> None:
        settings = _settings(aigate_model=None)

        with (
            self.assertRaisesRegex(ConfigurationError, "AIGATE_MODEL"),
            patch("two_x_brainz.runtime.PipeWireSource") as capture_source,
            patch("two_x_brainz.runtime.TalkiesClient") as talkies_client,
        ):
            asyncio.run(run_live(settings, _audio_setup("mic", "system")))

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
            asyncio.run(run_live(settings, _audio_setup("mic", "system")))

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
            asyncio.run(run_live(settings, _audio_setup("mic", "system")))

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
            asyncio.run(run_live(settings, _audio_setup("mic", "system")))

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
            asyncio.run(run_live(settings, _audio_setup("mic", "system")))

        preflight_client.verify_configured_model.assert_awaited_once()
        preflight_client.warm_configured_model.assert_awaited_once()
        capture_source.assert_not_called()

    def test_live_capture_failures_emit_one_fixed_session_error(self) -> None:
        settings = _settings(aigate_model="draft-model")
        client = _FrameDrainingTalkiesClient()
        terminal = _TerminalAudit(_selection("mic", "system"))

        with (
            self.assertRaises(ExceptionGroup),
            patch("two_x_brainz.runtime.AIGateClient", return_value=client),
            patch("two_x_brainz.runtime.TalkiesClient", return_value=client),
            patch("two_x_brainz.runtime.PipeWireSource", _CaptureFailingSource),
            patch(
                "two_x_brainz.runtime._read_session_controls",
                _wait_for_cancellation,
            ),
            patch("two_x_brainz.runtime.LiveTerminal", return_value=terminal),
        ):
            asyncio.run(run_live(settings, _audio_setup("mic", "system")))

        session_errors = [
            record
            for record in terminal.records
            if record.get("kind") == "session_error"
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
        terminal = _TerminalAudit(_selection("mic-node", "system-node"))

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
            patch(
                "two_x_brainz.capture.StreamingVoiceActivityDetector",
                _AlwaysSpeechDetector,
            ),
            patch("two_x_brainz.runtime.LiveTerminal", return_value=terminal),
        ):
            asyncio.run(run_live(settings, _audio_setup("mic-node", "system-node")))

        self.assertEqual(talkies_constructor.call_count, 3)
        self.assertEqual(capture_source.call_count, 2)
        capture_source.assert_any_call("mic-node")
        capture_source.assert_any_call("system-node", capture_sink=True)
        self.assertEqual(
            {speaker_role for speaker_role, _percent in terminal.audio_levels},
            {"user", "remote"},
        )
        self.assertEqual(
            {
                record["speaker_role"]
                for record in terminal.records
                if record.get("kind") == "transcript"
            },
            {"user", "remote"},
        )
        self.assertEqual(
            {
                record["speaker_role"]
                for record in terminal.records
                if record.get("kind") == "asr_stats"
            },
            {"user", "remote"},
        )
        self.assertEqual(
            {
                record["speaker_role"]
                for record in terminal.records
                if record.get("kind") == "capture_stats"
            },
            {"user", "remote"},
        )
        self.assertTrue(
            any(
                record.get("kind") == "draft" and record.get("status") == "completed"
                for record in terminal.records
            )
        )
        self.assertTrue(
            any(record.get("kind") == "summary" for record in terminal.records)
        )
        self.assertEqual(
            sum(record.get("kind") == "capture_drift" for record in terminal.records),
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


def _settings(aigate_model: str | None) -> Settings:
    return Settings(
        talkies_ws_url="ws://talkies:8000/v1/audio/transcriptions/stream",
        talkies_model="fixture-model",
        talkies_token=None,
        aigate_url="http://aigate:4000/v1",
        aigate_model=aigate_model,
        aigate_token=None,
        log_level="INFO",
        log_file=Path("/tmp/2xbrainz-test.log"),
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


class _TerminalAudit:
    def __init__(self, selection: AudioSelection) -> None:
        self.records: list[dict[str, object]] = []
        self.audio_levels: list[tuple[str, int]] = []
        self._selection = selection

    async def open(self) -> AudioSelection:
        return self._selection

    def consume(self, record: dict[str, object]) -> None:
        self.records.append(record)

    def refresh(self) -> None:
        return None

    def set_audio_level(self, speaker_role: str, percent: int) -> None:
        self.audio_levels.append((speaker_role, percent))

    async def close(self) -> None:
        return None


class _CaptureFailingSource:
    def __init__(self, node_id: str, *, capture_sink: bool = False) -> None:
        del capture_sink
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
    def __init__(self, node_id: str, *, capture_sink: bool = False) -> None:
        del capture_sink
        self._node_id = node_id

    async def frames(self) -> AsyncIterator[bytes]:
        if not self._node_id:
            return
        yield _FIXTURE_FRAME


class _AlwaysSpeechDetector:
    def observe(self, pcm: bytes) -> tuple[float, ...]:
        del pcm
        return (1.0, 1.0)


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
