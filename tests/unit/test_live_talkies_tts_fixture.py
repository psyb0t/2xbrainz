from __future__ import annotations

import asyncio
import importlib.util
import json
import struct
import unittest
import wave
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from two_x_brainz.aigate import AIGateClient
from two_x_brainz.contracts import (
    InsightKind,
    InsightRequest,
    SpeakerRole,
    TranscriptLine,
    TranscriptSnapshot,
)

_FIXTURE_SCRIPT = Path("tests/integration/live_talkies_tts_fixture.py")
_UNKNOWN_RIFF_CHUNK_SIZE = 0xFFFFFFFF


def _load_fixture_module() -> Any:
    specification = importlib.util.spec_from_file_location(
        "live_talkies_tts_fixture", _FIXTURE_SCRIPT
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


_FIXTURE = _load_fixture_module()


class TalkiesTtsFixtureWavTests(unittest.TestCase):
    def test_normalizes_unknown_streamed_wav_lengths(self) -> None:
        body = _streamed_wav(b"\x00\x00" * 320)

        normalized = _FIXTURE._canonicalize_wav_lengths(body)

        with wave.open(BytesIO(normalized), "rb") as reader:
            self.assertEqual(reader.getnchannels(), 1)
            self.assertEqual(reader.getsampwidth(), 2)
            self.assertEqual(reader.getframerate(), 16_000)
            self.assertEqual(reader.getnframes(), 320)

    def test_rejects_non_terminal_or_unknown_length_data_chunk(self) -> None:
        invalid = _streamed_wav(b"\x00\x00", data_size=1)

        with self.assertRaisesRegex(_FIXTURE.FixtureError, "data length"):
            _FIXTURE._canonicalize_wav_lengths(invalid)


class TalkiesTtsFixtureRecordTests(unittest.TestCase):
    def test_fixture_aigate_serves_the_configured_chat_route(self) -> None:
        request = InsightRequest(
            generation_id="fixture-insight",
            kind=InsightKind.COMMENTARY,
            trigger_turn_id="fixture-turn",
            context_revision=1,
            transcript=TranscriptSnapshot(
                revision=1,
                lines=(
                    TranscriptLine(
                        stream_id="fixture-stream",
                        speaker_role=SpeakerRole.USER,
                        revision=1,
                        text="Synthetic fixture input.",
                        is_final=True,
                    ),
                ),
            ),
            deadline_seconds=1,
        )

        with _FIXTURE._fixture_aigate() as base_url:
            client = AIGateClient(
                base_url=base_url,
                model=_FIXTURE._AIGATE_MODEL,
                token="",
            )
            asyncio.run(client.verify_configured_model())
            result = asyncio.run(client.insight(request))

        self.assertEqual(result.status.value, "completed")
        self.assertEqual(result.text, _FIXTURE._FIXTURE_REPLY_TEXT)

    def test_accepts_complete_safe_live_record_contract(self) -> None:
        _FIXTURE._assert_records(
            _complete_records(),
            _FIXTURE._OVERLAP_SCENARIO,
        )

    def test_accepts_zero_capture_comparisons_for_sequential_audio(self) -> None:
        records = _complete_records()
        records[-1]["comparison_count"] = 0

        _FIXTURE._assert_records(records, _FIXTURE._SEQUENTIAL_SCENARIO)

    def test_rejects_zero_capture_comparisons_for_overlapping_audio(self) -> None:
        records = _complete_records()
        records[-1]["comparison_count"] = 0

        with self.assertRaisesRegex(_FIXTURE.FixtureError, "overlap fixture"):
            _FIXTURE._assert_records(records, _FIXTURE._OVERLAP_SCENARIO)

    def test_rejects_missing_remote_final_transcript(self) -> None:
        records = _complete_records()
        records = [
            record
            for record in records
            if not (
                record["kind"] == "transcript" and record["speaker_role"] == "remote"
            )
        ]

        with self.assertRaisesRegex(_FIXTURE.FixtureError, "finalize both streams"):
            _FIXTURE._assert_records(records, _FIXTURE._OVERLAP_SCENARIO)

    def test_reports_only_safe_session_failure_reason_on_early_exit(self) -> None:
        error = _FIXTURE._early_exit_error(
            [{"kind": "session_error", "reason": "asr_unavailable"}]
        )

        self.assertEqual(
            str(error),
            "live fixture ended before both ASR streams: asr_unavailable",
        )

    def test_accepts_completed_real_product_record_contract(self) -> None:
        _FIXTURE._assert_product_records(_complete_product_records())

    def test_interview_scenario_has_two_turns_for_each_speaker(self) -> None:
        user_texts, remote_texts = _FIXTURE._scenario_texts(
            _FIXTURE._INTERVIEW_SCENARIO
        )

        self.assertEqual(user_texts, _FIXTURE._INTERVIEW_USER_TEXTS)
        self.assertEqual(remote_texts, _FIXTURE._INTERVIEW_REMOTE_TEXTS)

    def test_accepts_completed_interview_story_record_contract(self) -> None:
        _FIXTURE._assert_interview_records(_complete_interview_records())

    def test_accepts_live_asr_idempotency_variant_in_interview_story(self) -> None:
        records = _complete_interview_records()
        for record in records:
            text = record.get("text")
            if isinstance(text, str):
                record["text"] = text.replace("idempotency", "eye potency").replace(
                    "staging", "stajing"
                )

        _FIXTURE._assert_interview_records(records)

    def test_accepts_schedule_reference_without_day_in_first_remote_turn(self) -> None:
        records = _complete_interview_records()
        records[1]["text"] = (
            "How will duplicate deliveries be prevented before rehearsal?"
        )

        _FIXTURE._assert_interview_records(records)

    def test_rejects_interview_that_loses_the_idempotency_concept(self) -> None:
        records = _complete_interview_records()
        for record in records:
            text = record.get("text")
            if isinstance(text, str):
                record["text"] = text.replace("idempotency", "guard")

        with self.assertRaisesRegex(
            _FIXTURE.FixtureError, "required interview context"
        ):
            _FIXTURE._assert_interview_records(records)

    def test_rejects_interview_that_retains_the_first_draft(self) -> None:
        records = _complete_interview_records()
        records[-1]["trigger_turn_id"] = "remote-turn-1"

        with self.assertRaisesRegex(_FIXTURE.FixtureError, "stale reply"):
            _FIXTURE._assert_interview_records(records)

    def test_fails_immediately_for_a_required_provider_failure(self) -> None:
        with self.assertRaisesRegex(_FIXTURE.FixtureError, "provider failure"):
            _FIXTURE._raise_for_required_provider_failure(
                [{"kind": "draft", "status": "failed", "text": ""}],
                _FIXTURE._INTERVIEW_SCENARIO,
            )

    def test_rejects_product_records_without_a_completed_summary(self) -> None:
        records = [
            record
            for record in _complete_product_records()
            if record["kind"] != "summary"
        ]

        with self.assertRaisesRegex(_FIXTURE.FixtureError, "complete summary"):
            _FIXTURE._assert_product_records(records)

    def test_rejects_a_draft_when_audio_overlaps(self) -> None:
        records = _complete_records()
        records.append(
            {
                "kind": "draft",
                "status": "completed",
                "text": "A synthetic reply.",
            }
        )
        records.extend(_completed_insight_records())

        with self.assertRaisesRegex(_FIXTURE.FixtureError, "overlapping"):
            _FIXTURE._assert_overlap_records(records)

    def test_retries_only_bounded_tts_conflicts(self) -> None:
        self.assertTrue(
            _FIXTURE._should_retry_tts_conflict(
                _FIXTURE.HTTPStatus.CONFLICT,
                1,
            )
        )
        self.assertFalse(
            _FIXTURE._should_retry_tts_conflict(
                _FIXTURE.HTTPStatus.CONFLICT,
                _FIXTURE._FIXTURE_TTS_MAX_ATTEMPTS,
            )
        )
        self.assertFalse(_FIXTURE._should_retry_tts_conflict(500, 1))

    def test_records_structured_live_diagnostics_in_the_fixture_trace(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            trace = _FIXTURE.FixtureTrace(
                Path(temporary_directory),
                "synthetic-live",
            )
            _FIXTURE._trace_standard_error_line(
                b'{"level":"INFO","msg":"live started"}\n',
                trace,
            )
            trace.close()

            records = [
                json.loads(line)
                for line in trace.path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(records[-1]["kind"], "live_stderr_record")
        self.assertEqual(records[-1]["record"]["msg"], "live started")

    def test_rejects_and_records_unstructured_live_diagnostics(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            trace = _FIXTURE.FixtureTrace(
                Path(temporary_directory),
                "synthetic-live",
            )
            with self.assertRaisesRegex(_FIXTURE.FixtureError, "unstructured"):
                _FIXTURE._trace_standard_error_line(b"invalid diagnostic\n", trace)
            trace.close()

            records = [
                json.loads(line)
                for line in trace.path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(records[-1]["kind"], "live_stderr_unstructured")


def _streamed_wav(data: bytes, *, data_size: int = _UNKNOWN_RIFF_CHUNK_SIZE) -> bytes:
    fmt = struct.pack("<HHIIHH", 1, 1, 16_000, 32_000, 2, 16)
    return b"".join(
        (
            b"RIFF",
            struct.pack("<I", _UNKNOWN_RIFF_CHUNK_SIZE),
            b"WAVE",
            b"fmt ",
            struct.pack("<I", len(fmt)),
            fmt,
            b"data",
            struct.pack("<I", data_size),
            data,
        )
    )


def _complete_records() -> list[dict[str, object]]:
    return [
        {"kind": "session", "action": "started"},
        {"kind": "asr_stats", "speaker_role": "user", "frames": 1},
        {"kind": "asr_stats", "speaker_role": "remote", "frames": 1},
        {"kind": "capture_stats", "speaker_role": "user", "frame_count": 1},
        {
            "kind": "capture_stats",
            "speaker_role": "remote",
            "frame_count": 1,
        },
        {"kind": "transcript", "type": "final", "speaker_role": "user"},
        {"kind": "transcript", "type": "final", "speaker_role": "remote"},
        {"kind": "timeline", "speaker_role": "user"},
        {"kind": "timeline", "speaker_role": "remote"},
        {"kind": "capture_drift", "comparison_count": 1},
    ]


def _complete_product_records() -> list[dict[str, object]]:
    records = _complete_records()
    for record in records:
        if record["kind"] == "timeline" and record["speaker_role"] == "user":
            record["text"] = (
                "I will lead the Orchid migration before the Tuesday rehearsal. "
                "The risk is duplicate deliveries."
            )
        if record["kind"] == "timeline" and record["speaker_role"] == "remote":
            record["text"] = (
                "How will duplicate deliveries be prevented before Tuesday?"
            )
    records.append(
        {
            "kind": "draft",
            "status": "completed",
            "text": "Use idempotency before the Tuesday rehearsal.",
        }
    )
    records.extend(_completed_insight_records())
    return records


def _completed_insight_records() -> list[dict[str, object]]:
    return [
        {
            "kind": "commentary",
            "status": "completed",
            "text": "A synthetic coaching note.",
        },
        {
            "kind": "summary",
            "status": "completed",
            "text": "Orchid migration has a Tuesday rehearsal and duplicate risk.",
        },
    ]


def _complete_interview_records() -> list[dict[str, object]]:
    return [
        {
            "kind": "timeline",
            "turn_id": "user-turn-1",
            "speaker_role": "user",
            "text": (
                "I will lead the Orchid migration before the Tuesday rehearsal. "
                "The risk is duplicate deliveries."
            ),
        },
        {
            "kind": "timeline",
            "turn_id": "remote-turn-1",
            "speaker_role": "remote",
            "text": "How will duplicate deliveries be prevented before Tuesday?",
        },
        {
            "kind": "timeline",
            "turn_id": "user-turn-2",
            "speaker_role": "user",
            "text": (
                "I will add an idempotency key and validate it in staging before "
                "Tuesday."
            ),
        },
        {
            "kind": "timeline",
            "turn_id": "remote-turn-2",
            "speaker_role": "remote",
            "text": "What evidence will show the idempotency guard before rehearsal?",
        },
        {
            "kind": "commentary",
            "status": "completed",
            "trigger_turn_id": "user-turn-1",
            "text": "The initial commitment identifies the migration risk.",
        },
        {
            "kind": "draft",
            "status": "completed",
            "trigger_turn_id": "remote-turn-1",
            "text": "I will verify duplicate prevention before Tuesday.",
        },
        {
            "kind": "commentary",
            "status": "completed",
            "trigger_turn_id": "user-turn-2",
            "text": "The mitigation identifies the idempotency verification path.",
        },
        {
            "kind": "summary",
            "status": "completed",
            "trigger_turn_id": "remote-turn-2",
            "text": (
                "The Orchid migration has a Tuesday rehearsal, duplicate delivery "
                "risk, an idempotency mitigation, and staging verification."
            ),
        },
        {
            "kind": "draft",
            "status": "completed",
            "trigger_turn_id": "remote-turn-2",
            "text": "I will verify the idempotency guard in staging before rehearsal.",
        },
    ]
