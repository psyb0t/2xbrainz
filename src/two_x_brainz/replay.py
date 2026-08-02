"""Strict replay-fixture loading for deterministic local verification."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from pathlib import Path
from typing import cast

from two_x_brainz.constants import MAX_REPLAY_LINE_BYTES
from two_x_brainz.contracts import (
    SpeakerRole,
    TranscriptEvent,
    TranscriptEventType,
    WordTiming,
)
from two_x_brainz.errors import ReplayError
from two_x_brainz.json_support import decode_json, require_json_object

_REQUIRED_FIELDS = frozenset({"speaker_role", "type", "revision", "text"})


def load_replay_events(
    path: Path,
    session_id: str,
    model: str,
) -> Iterator[TranscriptEvent]:
    """Load bounded JSONL transcript events from a user-selected local fixture."""
    if not path.is_file():
        raise ReplayError(f"replay fixture does not exist: {path}")
    with path.open("r", encoding="utf-8") as fixture:
        for line_number, raw_line in enumerate(fixture, start=1):
            if len(raw_line.encode("utf-8")) > MAX_REPLAY_LINE_BYTES:
                raise ReplayError(f"replay line {line_number} exceeds the size limit")
            if not raw_line.strip():
                continue
            try:
                payload = require_json_object(decode_json(raw_line))
            except json.JSONDecodeError as error:
                raise ReplayError(f"replay line {line_number} is not JSON") from error
            except ValueError as error:
                raise ReplayError(
                    f"replay line {line_number} must be an object"
                ) from error
            yield _parse_event(payload, line_number, session_id, model)


def _parse_event(
    payload: dict[str, object],
    line_number: int,
    session_id: str,
    model: str,
) -> TranscriptEvent:
    if not _REQUIRED_FIELDS.issubset(payload):
        raise ReplayError(f"replay line {line_number} is missing required fields")
    try:
        speaker_role = SpeakerRole(_require_text(payload, "speaker_role"))
        event_type = TranscriptEventType(_require_text(payload, "type"))
    except ValueError as error:
        raise ReplayError(
            f"replay line {line_number} has an unsupported enum"
        ) from error

    revision = payload["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ReplayError(f"replay line {line_number} revision is invalid")
    text = _require_text(payload, "text")
    audio_seconds = _require_nonnegative_number(
        payload,
        "audio_seconds",
        0.0,
        line_number,
    )
    words = tuple(_parse_words(payload.get("words", []), line_number))
    derived_started_at_ms, derived_ended_at_ms = _timing_bounds(words)
    started_at_ms = _optional_nonnegative_int(
        payload.get("started_at_ms"),
        line_number,
        "started_at_ms",
    )
    ended_at_ms = _optional_nonnegative_int(
        payload.get("ended_at_ms"),
        line_number,
        "ended_at_ms",
    )
    if started_at_ms is None:
        started_at_ms = derived_started_at_ms
    if ended_at_ms is None:
        ended_at_ms = derived_ended_at_ms
    if (
        started_at_ms is not None
        and ended_at_ms is not None
        and ended_at_ms < started_at_ms
    ):
        raise ReplayError(f"replay line {line_number} timing end precedes its start")
    stream_id = f"{speaker_role.value}-stream"
    return TranscriptEvent(
        session_id=session_id,
        stream_id=stream_id,
        utterance_id=f"{stream_id}:{revision}",
        revision=revision,
        speaker_role=speaker_role,
        source_event_type=event_type,
        asr_model=model,
        text=text,
        is_final=event_type is TranscriptEventType.FINAL,
        audio_seconds=audio_seconds,
        words=words,
        started_at_ms=started_at_ms,
        ended_at_ms=ended_at_ms,
        confidence=_optional_confidence(payload.get("confidence"), line_number),
        language=_optional_language(payload.get("language"), line_number),
    )


def _require_text(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise ReplayError(f"replay field {field} must be text")
    return value


def _parse_words(value: object, line_number: int) -> Iterator[WordTiming]:
    if not isinstance(value, list):
        raise ReplayError(f"replay line {line_number} words must be an array")
    words = cast(list[object], value)
    for item in words:
        try:
            word_data = require_json_object(item)
        except ValueError as error:
            raise ReplayError(
                f"replay line {line_number} word must be an object"
            ) from error
        word = _require_text(word_data, "word")
        start_ms = _optional_seconds_to_ms(word_data.get("start"), line_number)
        end_ms = _optional_seconds_to_ms(word_data.get("end"), line_number)
        if start_ms is not None and end_ms is not None and end_ms < start_ms:
            raise ReplayError(f"replay line {line_number} word end precedes its start")
        yield WordTiming(
            word=word,
            start_ms=start_ms,
            end_ms=end_ms,
            confidence=_optional_confidence(word_data.get("confidence"), line_number),
        )


def _require_nonnegative_number(
    payload: dict[str, object],
    field: str,
    default: float,
    line_number: int,
) -> float:
    value = payload.get(field, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ReplayError(f"replay line {line_number} {field} is invalid")
    return float(value)


def _optional_seconds_to_ms(value: object, line_number: int) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ReplayError(f"replay line {line_number} word timing is invalid")
    return round(float(value) * 1_000)


def _optional_nonnegative_int(
    value: object,
    line_number: int,
    field: str,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReplayError(f"replay line {line_number} {field} is invalid")
    return value


def _optional_confidence(value: object, line_number: int) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise ReplayError(f"replay line {line_number} confidence is invalid")
    return float(value)


def _optional_language(value: object, line_number: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ReplayError(f"replay line {line_number} language is invalid")
    return value


def _timing_bounds(words: tuple[WordTiming, ...]) -> tuple[int | None, int | None]:
    starts = [word.start_ms for word in words if word.start_ms is not None]
    ends = [word.end_ms for word in words if word.end_ms is not None]
    return (min(starts) if starts else None, max(ends) if ends else None)
