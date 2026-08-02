"""Revision-safe transcript reconciliation."""

from __future__ import annotations

from two_x_brainz.constants import (
    MAX_RECENT_TRANSCRIPT_LINES,
    MAX_SUMMARY_TEXT_CHARACTERS,
)
from two_x_brainz.contracts import TranscriptEvent, TranscriptLine, TranscriptSnapshot


class TranscriptStore:
    """Maintains only ordered, monotonic transcript revisions."""

    def __init__(self) -> None:
        self._lines: list[tuple[int, TranscriptLine]] = []
        self._latest_revision_by_stream: dict[str, int] = {}
        self._snapshot_revision = 0
        self._running_summary = ""
        self._summary_through_revision = 0

    def apply(self, event: TranscriptEvent) -> TranscriptSnapshot:
        """Apply an event or reject a stale revision without mutating state."""
        latest_revision = self._latest_revision_by_stream.get(event.stream_id, -1)
        if event.revision <= latest_revision:
            return self.snapshot()

        self._latest_revision_by_stream[event.stream_id] = event.revision
        line = TranscriptLine(
            stream_id=event.stream_id,
            speaker_role=event.speaker_role,
            revision=event.revision,
            text=event.text,
            is_final=event.is_final,
        )
        self._snapshot_revision += 1
        if self._lines and self._lines[-1][1].stream_id == event.stream_id:
            self._lines[-1] = (self._snapshot_revision, line)
        else:
            self._lines.append((self._snapshot_revision, line))
        return self.snapshot()

    def set_running_summary(self, summary: str, through_revision: int) -> bool:
        """Accept a bounded summary only when it advances known transcript history."""
        normalized_summary = summary.strip()
        if not normalized_summary:
            return False
        if len(normalized_summary) > MAX_SUMMARY_TEXT_CHARACTERS:
            return False
        if through_revision <= self._summary_through_revision:
            return False
        if through_revision > self._snapshot_revision:
            return False

        self._running_summary = normalized_summary
        self._summary_through_revision = through_revision
        self._trim_summarized_lines()
        return True

    def snapshot(self) -> TranscriptSnapshot:
        """Return an immutable view for context assembly and terminal rendering."""
        return TranscriptSnapshot(
            revision=self._snapshot_revision,
            lines=tuple(line for _, line in self._lines),
            running_summary=self._running_summary,
        )

    def _trim_summarized_lines(self) -> None:
        while len(self._lines) > MAX_RECENT_TRANSCRIPT_LINES:
            oldest_revision, _ = self._lines[0]
            if oldest_revision > self._summary_through_revision:
                return
            self._lines.pop(0)
