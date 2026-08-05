"""Typed domain contracts shared across capture, ASR, and drafting."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SpeakerRole(StrEnum):
    """The capture stream establishes these roles without diarization."""

    USER = "user"
    REMOTE = "remote"


class TranscriptEventType(StrEnum):
    """Talkies transcript events that affect transcript state."""

    PARTIAL = "partial"
    ENDPOINT = "endpoint"
    FINAL = "final"


class TurnState(StrEnum):
    """Lifecycle states for a detected conversation turn."""

    SPEAKING = "speaking"
    CANDIDATE_END = "candidate_end"
    FINALIZED = "finalized"
    REOPENED = "reopened"


class GenerationStatus(StrEnum):
    """Lifecycle states for a background draft job."""

    QUEUED = "queued"
    RUNNING = "running"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    COMPLETED = "completed"
    FAILED = "failed"


class InsightKind(StrEnum):
    """Background text outputs that must never replace a reply draft."""

    COMMENTARY = "commentary"
    SUMMARY = "summary"


@dataclass(frozen=True, slots=True)
class WordTiming:
    """A word timing supplied by an ASR backend when available."""

    word: str
    start_ms: int | None
    end_ms: int | None
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class AudioFrame:
    """One runtime-owned PCM frame with immutable capture identity and timing."""

    session_id: str
    stream_id: str
    speaker_role: SpeakerRole
    sequence: int
    captured_at_monotonic: float
    sample_rate_hz: int
    channels: int
    samples: bytes


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    """A normalized event from one ASR stream."""

    session_id: str
    stream_id: str
    utterance_id: str
    revision: int
    speaker_role: SpeakerRole
    source_event_type: TranscriptEventType
    asr_model: str
    text: str
    is_final: bool
    audio_seconds: float
    words: tuple[WordTiming, ...]
    started_at_ms: int | None = None
    ended_at_ms: int | None = None
    confidence: float | None = None
    language: str | None = None


@dataclass(frozen=True, slots=True)
class ASRStreamStats:
    """Aggregate terminal statistics for one ASR stream."""

    session_id: str
    stream_id: str
    speaker_role: SpeakerRole
    asr_model: str
    audio_seconds: float
    frames: int
    canceled: bool


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    """The latest stable or partial text for one stream segment."""

    stream_id: str
    speaker_role: SpeakerRole
    revision: int
    text: str
    is_final: bool


@dataclass(frozen=True, slots=True)
class TranscriptSnapshot:
    """An immutable, ordered view passed to a draft provider."""

    revision: int
    lines: tuple[TranscriptLine, ...]
    running_summary: str = ""


@dataclass(frozen=True, slots=True)
class TurnEvent:
    """A state transition emitted by the turn manager."""

    turn_id: str
    speaker_role: SpeakerRole
    state: TurnState
    transcript_revision: int


@dataclass(frozen=True, slots=True)
class DraftRequest:
    """The current text-only conversation state sent to a draft provider."""

    generation_id: str
    trigger_turn_id: str
    context_revision: int
    transcript: TranscriptSnapshot
    deadline_seconds: float


@dataclass(frozen=True, slots=True)
class DraftResult:
    """A display-only reply suggestion for the current conversation state."""

    generation_id: str
    trigger_turn_id: str
    context_revision: int
    status: GenerationStatus
    text: str


@dataclass(frozen=True, slots=True)
class InsightRequest:
    """A text-only background job tied to an immutable transcript revision."""

    generation_id: str
    kind: InsightKind
    trigger_turn_id: str
    context_revision: int
    transcript: TranscriptSnapshot
    deadline_seconds: float


@dataclass(frozen=True, slots=True)
class InsightResult:
    """A completed commentary or summary that cannot affect reply state."""

    generation_id: str
    kind: InsightKind
    trigger_turn_id: str
    context_revision: int
    status: GenerationStatus
    text: str


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """One idempotent CLI timeline record linked to its source turn."""

    turn_id: str
    speaker_role: SpeakerRole
    transcript_revision: int
    text: str
