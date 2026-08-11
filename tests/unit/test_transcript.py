from __future__ import annotations

import unittest

from two_x_brainz.constants import MAX_RECENT_TRANSCRIPT_LINES
from two_x_brainz.contracts import SpeakerRole, TranscriptEvent, TranscriptEventType
from two_x_brainz.transcript import TranscriptStore


class TranscriptStoreTests(unittest.TestCase):
    def test_summary_acceptance_trims_only_covered_history(self) -> None:
        store = TranscriptStore()
        for index in range(MAX_RECENT_TRANSCRIPT_LINES + 1):
            store.apply(_event(index))

        accepted = store.set_running_summary(
            "running summary",
            MAX_RECENT_TRANSCRIPT_LINES + 1,
        )
        snapshot = store.snapshot()

        self.assertTrue(accepted)
        self.assertEqual(snapshot.running_summary, "running summary")
        self.assertEqual(len(snapshot.lines), MAX_RECENT_TRANSCRIPT_LINES)
        self.assertEqual(snapshot.lines[0].stream_id, "stream-1")

    def test_summary_rejects_empty_older_and_future_coverage(self) -> None:
        store = TranscriptStore()
        store.apply(_event(0))

        self.assertFalse(store.set_running_summary("", 1))
        self.assertFalse(store.set_running_summary("future", 2))
        self.assertTrue(store.set_running_summary("accepted", 1))
        self.assertFalse(store.set_running_summary("older", 1))
        self.assertEqual(store.snapshot().running_summary, "accepted")

    def test_post_summary_lines_remain_after_the_context_prefix(self) -> None:
        store = TranscriptStore()
        store.apply(_event(0))
        self.assertTrue(store.set_running_summary("covered history", 1))
        store.apply(_event(1))

        snapshot = store.snapshot()

        self.assertEqual(snapshot.running_summary, "covered history")
        self.assertEqual(snapshot.lines[-1].stream_id, "stream-1")

    def test_research_context_is_bounded_and_only_advances(self) -> None:
        store = TranscriptStore()
        store.apply(_event(0))

        self.assertFalse(store.set_research_context("", 1))
        self.assertFalse(store.set_research_context("future", 2))
        self.assertTrue(store.set_research_context("verified findings", 1))
        self.assertFalse(store.set_research_context("older findings", 1))
        self.assertEqual(store.snapshot().research_context, "verified findings")


def _event(index: int) -> TranscriptEvent:
    stream_id = f"stream-{index}"
    return TranscriptEvent(
        session_id="session",
        stream_id=stream_id,
        utterance_id=f"{stream_id}:1",
        revision=1,
        speaker_role=SpeakerRole.REMOTE,
        source_event_type=TranscriptEventType.FINAL,
        asr_model="test-model",
        text=f"line {index}",
        is_final=True,
        audio_seconds=1.0,
        words=(),
    )
