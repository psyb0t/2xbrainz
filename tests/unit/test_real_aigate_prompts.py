from __future__ import annotations

import asyncio
import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from two_x_brainz.config import Settings
from two_x_brainz.contracts import (
    DraftRequest,
    GenerationStatus,
    InsightKind,
    InsightRequest,
    TranscriptSnapshot,
)
from two_x_brainz.errors import ProtocolError
from two_x_brainz.fixture_trace import FixtureTrace

_PROMPT_SCRIPT = Path("tests/integration/real_aigate_prompts.py")


def _load_prompt_module() -> Any:
    specification = importlib.util.spec_from_file_location(
        "real_aigate_prompts", _PROMPT_SCRIPT
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


_PROMPTS = _load_prompt_module()


def _settings(flow_model: str) -> Settings:
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
        log_file=Path("/tmp/2xbrainz-real-prompt-test.log"),
    )


class RealAIGatePromptContractTests(unittest.TestCase):
    def test_fixture_uses_three_explicit_distinct_models(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "TWOXBRAINZ_FIXTURE_DRAFT_MODEL": "groq-model",
                "TWOXBRAINZ_FIXTURE_COMMENTARY_MODEL": "claudebox-model",
                "TWOXBRAINZ_FIXTURE_SUMMARY_MODEL": "pibox-model",
            },
            clear=False,
        ):
            models = _PROMPTS._fixture_models(_settings("fallback-model"))

        self.assertEqual(
            models,
            ("groq-model", "claudebox-model", "pibox-model"),
        )

    def test_fixture_rejects_duplicate_model_assignments(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "TWOXBRAINZ_FIXTURE_DRAFT_MODEL": "model-a",
                    "TWOXBRAINZ_FIXTURE_COMMENTARY_MODEL": "model-a",
                    "TWOXBRAINZ_FIXTURE_SUMMARY_MODEL": "model-b",
                },
                clear=False,
            ),
            self.assertRaisesRegex(_PROMPTS.PromptFixtureError, "must be distinct"),
        ):
            _PROMPTS._fixture_models(_settings("fallback-model"))

    def test_reply_research_requires_completed_research_tool_activity(self) -> None:
        _PROMPTS._assert_research_activity(
            [
                {"phase": "tool_started", "tool": "research_web"},
                {"phase": "tool_completed", "tool": "research_web"},
            ]
        )

        invalid_activities: tuple[list[dict[str, object]], ...] = (
            [],
            [{"phase": "tool_started", "tool": "research_web"}],
            [{"phase": "tool_completed", "tool": "execute_code"}],
        )
        for activities in invalid_activities:
            with (
                self.subTest(activities=activities),
                self.assertRaisesRegex(
                    _PROMPTS.PromptFixtureError,
                    "did not autonomously complete",
                ),
            ):
                _PROMPTS._assert_research_activity(activities)

    def test_accepts_plain_completed_outputs(self) -> None:
        _PROMPTS._assert_completed_text(
            "generation-id",
            GenerationStatus.COMPLETED,
            "A concise synthetic response.",
            requires_one_line=True,
        )

    def test_rejects_non_terminal_empty_multiline_and_markdown_outputs(self) -> None:
        cases = (
            (GenerationStatus.FAILED, "", True),
            (GenerationStatus.COMPLETED, "", True),
            (GenerationStatus.COMPLETED, "First line.\nSecond line.", True),
            (GenerationStatus.COMPLETED, "- A list item", False),
            (GenerationStatus.COMPLETED, "Use **emphasis**.", False),
        )

        for status, text, requires_one_line in cases:
            with (
                self.subTest(status=status, text=text),
                self.assertRaises(_PROMPTS.PromptFixtureError),
            ):
                _PROMPTS._assert_completed_text(
                    "generation-id",
                    status,
                    text,
                    requires_one_line=requires_one_line,
                )

    def test_story_summary_requires_every_interview_anchor(self) -> None:
        _PROMPTS._assert_story_anchors(
            "Orchid migration Tuesday rehearsal risks duplicate deliveries.",
            _PROMPTS._INITIAL_STORY_ANCHORS,
            "summary",
        )

        with self.assertRaises(_PROMPTS.PromptFixtureError):
            _PROMPTS._assert_story_anchors(
                "The migration has a rehearsal and a delivery risk.",
                _PROMPTS._INITIAL_STORY_ANCHORS,
                "summary",
            )

    def test_reply_context_requires_the_previous_running_summary(self) -> None:
        expected_summary = "Orchid migration Tuesday rehearsal duplicate deliveries."
        request = DraftRequest(
            generation_id="draft-id",
            trigger_turn_id="turn-id",
            context_revision=2,
            transcript=TranscriptSnapshot(
                revision=2,
                lines=(),
                running_summary=expected_summary,
            ),
            deadline_seconds=15.0,
        )

        _PROMPTS._assert_draft_received_summary([request], expected_summary)

        with self.assertRaises(_PROMPTS.PromptFixtureError):
            _PROMPTS._assert_draft_received_summary([request], "other summary")

    def test_final_draft_requires_evidence_delivery_safety_and_no_new_weekday(
        self,
    ) -> None:
        _PROMPTS._assert_final_draft_story(
            "I will use staging logs that show the same message ID arriving "
            "twice and only one notification sent before the Tuesday rehearsal."
        )

        invalid_drafts = (
            "I will provide evidence before the Tuesday rehearsal.",
            "I will use staging logs to verify the idempotency guard by Monday.",
        )
        for draft in invalid_drafts:
            with (
                self.subTest(draft=draft),
                self.assertRaises(_PROMPTS.PromptFixtureError),
            ):
                _PROMPTS._assert_final_draft_story(draft)

    def test_provider_error_trace_contains_the_sanitized_error_message(self) -> None:
        class RejectingClient:
            async def insight(self, _request: InsightRequest) -> object:
                raise ProtocolError("AIGate content must not use Markdown structure")

        with TemporaryDirectory() as temporary_directory:
            trace = FixtureTrace(Path(temporary_directory), "provider-error")
            provider = _PROMPTS._TracingProvider(RejectingClient(), trace)
            request = InsightRequest(
                generation_id="commentary-id",
                kind=InsightKind.COMMENTARY,
                trigger_turn_id="turn-id",
                context_revision=1,
                transcript=TranscriptSnapshot(revision=1, lines=()),
                deadline_seconds=1.0,
            )

            with self.assertRaisesRegex(ProtocolError, "Markdown structure"):
                asyncio.run(provider.insight(request))
            trace.close()

            records = [
                json.loads(line)
                for line in trace.path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(records[-1]["kind"], "insight_error")
        self.assertEqual(records[-1]["error_type"], "ProtocolError")
        self.assertEqual(
            records[-1]["error_message"],
            "AIGate content must not use Markdown structure",
        )

    def test_provider_activity_trace_preserves_stream_order(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            trace = FixtureTrace(Path(temporary_directory), "provider-stream")
            _PROMPTS._trace_provider_activity(
                trace,
                {
                    "kind": "ignored-wrapper-kind",
                    "phase": "reasoning_streaming",
                    "flow_id": "flow-a",
                    "reasoning": "Synthetic reasoning",
                },
            )
            _PROMPTS._trace_provider_activity(
                trace,
                {
                    "phase": "output_streaming",
                    "flow_id": "flow-a",
                    "output": "Synthetic answer",
                },
            )
            trace.close()
            records = [
                json.loads(line)
                for line in trace.path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(
            [record["phase"] for record in records[1:]],
            ["reasoning_streaming", "output_streaming"],
        )
        self.assertTrue(
            all(record["kind"] == "provider_activity" for record in records[1:])
        )
