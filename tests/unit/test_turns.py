from __future__ import annotations

import unittest

from two_x_brainz.contracts import (
    SpeakerRole,
    TranscriptEvent,
    TranscriptEventType,
    TurnState,
)
from two_x_brainz.turns import TurnManager


class TurnManagerTests(unittest.TestCase):
    def test_partial_endpoint_final_preserves_one_turn_identity(self) -> None:
        manager = TurnManager()

        speaking = manager.apply(_event(TranscriptEventType.PARTIAL, 1))
        candidate = manager.apply(_event(TranscriptEventType.ENDPOINT, 2))
        finalized = manager.apply(_event(TranscriptEventType.FINAL, 3))

        self.assertIsNotNone(speaking)
        self.assertIsNotNone(candidate)
        self.assertIsNotNone(finalized)
        assert speaking is not None
        assert candidate is not None
        assert finalized is not None
        self.assertEqual(
            [speaking.state, candidate.state, finalized.state],
            [TurnState.SPEAKING, TurnState.CANDIDATE_END, TurnState.FINALIZED],
        )
        self.assertEqual(speaking.turn_id, candidate.turn_id)
        self.assertEqual(candidate.turn_id, finalized.turn_id)

    def test_duplicate_events_do_not_create_extra_transitions(self) -> None:
        manager = TurnManager()

        self.assertIsNotNone(manager.apply(_event(TranscriptEventType.PARTIAL, 1)))
        self.assertIsNone(manager.apply(_event(TranscriptEventType.PARTIAL, 2)))
        self.assertIsNotNone(manager.apply(_event(TranscriptEventType.ENDPOINT, 3)))
        self.assertIsNone(manager.apply(_event(TranscriptEventType.ENDPOINT, 4)))
        self.assertIsNotNone(manager.apply(_event(TranscriptEventType.FINAL, 5)))
        self.assertIsNone(manager.apply(_event(TranscriptEventType.FINAL, 5)))

    def test_partial_reopens_candidate_and_new_partial_follows_final(self) -> None:
        manager = TurnManager()

        initial = manager.apply(_event(TranscriptEventType.PARTIAL, 1))
        manager.apply(_event(TranscriptEventType.ENDPOINT, 2))
        reopened = manager.apply(_event(TranscriptEventType.PARTIAL, 3))
        repeated_partial = manager.apply(_event(TranscriptEventType.PARTIAL, 4))
        manager.apply(_event(TranscriptEventType.FINAL, 5))
        next_turn = manager.apply(_event(TranscriptEventType.PARTIAL, 6))

        self.assertIsNotNone(initial)
        self.assertIsNotNone(reopened)
        self.assertIsNotNone(next_turn)
        assert initial is not None
        assert reopened is not None
        assert next_turn is not None
        self.assertEqual(reopened.state, TurnState.REOPENED)
        self.assertEqual(reopened.turn_id, initial.turn_id)
        self.assertIsNone(repeated_partial)
        self.assertEqual(next_turn.state, TurnState.SPEAKING)
        self.assertNotEqual(next_turn.turn_id, initial.turn_id)

    def test_empty_endpoint_and_final_do_not_finalize_a_turn(self) -> None:
        manager = TurnManager()

        self.assertIsNone(
            manager.apply(_event(TranscriptEventType.ENDPOINT, 1, text=""))
        )
        self.assertIsNone(manager.apply(_event(TranscriptEventType.FINAL, 2, text="")))

    def test_active_speech_is_scoped_to_its_speaker_role(self) -> None:
        manager = TurnManager()

        manager.apply(_event(TranscriptEventType.PARTIAL, 1, SpeakerRole.USER))
        manager.apply(_event(TranscriptEventType.ENDPOINT, 2, SpeakerRole.USER))
        manager.apply(_event(TranscriptEventType.PARTIAL, 1, SpeakerRole.REMOTE))

        self.assertTrue(manager.has_active_speech(SpeakerRole.USER))
        self.assertTrue(manager.has_active_speech(SpeakerRole.REMOTE))

        manager.apply(_event(TranscriptEventType.FINAL, 3, SpeakerRole.USER))

        self.assertFalse(manager.has_active_speech(SpeakerRole.USER))
        self.assertTrue(manager.has_active_speech(SpeakerRole.REMOTE))


def _event(
    event_type: TranscriptEventType,
    revision: int,
    speaker_role: SpeakerRole = SpeakerRole.REMOTE,
    text: str = "synthetic test transcript",
) -> TranscriptEvent:
    stream_id = f"{speaker_role.value}-stream"
    return TranscriptEvent(
        session_id="session",
        stream_id=stream_id,
        utterance_id=f"{stream_id}:{revision}",
        revision=revision,
        speaker_role=speaker_role,
        source_event_type=event_type,
        asr_model="nemotron-3.5-asr-0.6b",
        text=text,
        is_final=event_type is TranscriptEventType.FINAL,
        audio_seconds=1.0,
        words=(),
    )
