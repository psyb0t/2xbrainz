"""ASR endpoint-confirmed turn state transitions."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from two_x_brainz.contracts import (
    SpeakerRole,
    TranscriptEvent,
    TranscriptEventType,
    TurnEvent,
    TurnState,
)


@dataclass(frozen=True, slots=True)
class _StreamTurn:
    """The current independent turn for one ASR stream."""

    turn_id: str
    speaker_role: SpeakerRole
    state: TurnState
    revision: int


class TurnManager:
    """Turn each stream's ASR signals into stable, replayable transitions."""

    def __init__(self) -> None:
        self._turns: dict[str, _StreamTurn] = {}

    def apply(self, event: TranscriptEvent) -> TurnEvent | None:
        """Return a new transition only when one stream changes turn state."""
        current = self._turns.get(event.stream_id)
        if current is not None and event.revision <= current.revision:
            return None
        if event.source_event_type is TranscriptEventType.PARTIAL:
            return self._on_partial(event, current)
        if not event.text.strip():
            return None
        if event.source_event_type is TranscriptEventType.ENDPOINT:
            return self._on_endpoint(event, current)
        return self._on_final(event, current)

    def has_active_speech(self, speaker_role: SpeakerRole) -> bool:
        """Return whether the role has a turn that can still resume speech."""
        return any(
            turn.speaker_role is speaker_role
            and turn.state in {TurnState.SPEAKING, TurnState.CANDIDATE_END}
            for turn in self._turns.values()
        )

    def _on_partial(
        self,
        event: TranscriptEvent,
        current: _StreamTurn | None,
    ) -> TurnEvent | None:
        if current is not None and current.state is TurnState.SPEAKING:
            self._turns[event.stream_id] = _StreamTurn(
                turn_id=current.turn_id,
                speaker_role=event.speaker_role,
                state=current.state,
                revision=event.revision,
            )
            return None
        if current is not None and current.state is TurnState.CANDIDATE_END:
            return self._reopen_turn(event, current.turn_id)
        return self._set_turn(event, str(uuid4()), TurnState.SPEAKING)

    def _on_endpoint(
        self,
        event: TranscriptEvent,
        current: _StreamTurn | None,
    ) -> TurnEvent | None:
        if current is not None and current.state is TurnState.CANDIDATE_END:
            self._turns[event.stream_id] = _StreamTurn(
                turn_id=current.turn_id,
                speaker_role=event.speaker_role,
                state=current.state,
                revision=event.revision,
            )
            return None
        turn_id = current.turn_id if current is not None else str(uuid4())
        return self._set_turn(event, turn_id, TurnState.CANDIDATE_END)

    def _on_final(
        self,
        event: TranscriptEvent,
        current: _StreamTurn | None,
    ) -> TurnEvent:
        turn_id = current.turn_id if current is not None else str(uuid4())
        return self._set_turn(event, turn_id, TurnState.FINALIZED)

    def _set_turn(
        self,
        event: TranscriptEvent,
        turn_id: str,
        state: TurnState,
    ) -> TurnEvent:
        self._turns[event.stream_id] = _StreamTurn(
            turn_id=turn_id,
            speaker_role=event.speaker_role,
            state=state,
            revision=event.revision,
        )
        return TurnEvent(
            turn_id=turn_id,
            speaker_role=event.speaker_role,
            state=state,
            transcript_revision=event.revision,
        )

    def _reopen_turn(self, event: TranscriptEvent, turn_id: str) -> TurnEvent:
        self._turns[event.stream_id] = _StreamTurn(
            turn_id=turn_id,
            speaker_role=event.speaker_role,
            state=TurnState.SPEAKING,
            revision=event.revision,
        )
        return TurnEvent(
            turn_id=turn_id,
            speaker_role=event.speaker_role,
            state=TurnState.REOPENED,
            transcript_revision=event.revision,
        )
