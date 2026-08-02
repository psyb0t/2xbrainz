from __future__ import annotations

import unittest

from two_x_brainz.benchmark import validate_benchmark_draft_result, word_error_rate
from two_x_brainz.contracts import (
    DraftRequest,
    DraftResult,
    GenerationStatus,
    SpeakerRole,
    TranscriptLine,
    TranscriptSnapshot,
)
from two_x_brainz.errors import ProtocolError


class DraftProbeTests(unittest.TestCase):
    def test_rejects_a_non_completed_draft_result(self) -> None:
        request = _draft_request()
        result = DraftResult(
            generation_id=request.generation_id,
            trigger_turn_id=request.trigger_turn_id,
            context_revision=request.context_revision,
            status=GenerationStatus.FAILED,
            text="",
        )

        with self.assertRaisesRegex(ProtocolError, "did not complete"):
            validate_benchmark_draft_result(request, result)


class WordErrorRateTests(unittest.TestCase):
    def test_normalizes_case_and_punctuation(self) -> None:
        self.assertEqual(word_error_rate("Hello, World!", "hello world"), 0.0)

    def test_counts_substitution_and_insertion(self) -> None:
        self.assertEqual(
            word_error_rate("one two three", "one four three five"),
            2 / 3,
        )

    def test_rejects_a_reference_without_evaluation_words(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "did not contain evaluation words"):
            word_error_rate("---", "recognized words")

    def test_empty_recognition_counts_every_reference_word_as_an_error(self) -> None:
        self.assertEqual(word_error_rate("one two", "---"), 1.0)

    def test_normalizes_unicode_letters_and_digits(self) -> None:
        self.assertEqual(word_error_rate("Caf\u00e9 123", "CAF\u00c9-123"), 0.0)


def _draft_request() -> DraftRequest:
    return DraftRequest(
        generation_id="generation",
        trigger_turn_id="turn",
        context_revision=1,
        transcript=TranscriptSnapshot(
            revision=1,
            lines=(
                TranscriptLine(
                    stream_id="remote",
                    speaker_role=SpeakerRole.REMOTE,
                    revision=1,
                    text="synthetic text",
                    is_final=True,
                ),
            ),
        ),
        deadline_seconds=15.0,
    )
