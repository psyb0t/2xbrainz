from __future__ import annotations

import asyncio
import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

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


class RealAIGatePromptContractTests(unittest.TestCase):
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
