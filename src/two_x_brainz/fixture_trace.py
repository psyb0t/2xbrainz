"""Durable, redacted JSONL traces for opt-in external integration fixtures."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from pathlib import Path
from typing import cast

from two_x_brainz.constants import LOG_REDACTION_VALUE

_SENSITIVE_KEY = re.compile(
    r"password|token|secret|api[_-]?key|authorization|cookie", re.IGNORECASE
)


class FixtureTraceError(RuntimeError):
    """A real fixture could not preserve its reconstruction evidence."""


class FixtureTrace:
    """Append ordered, secret-redacted fixture events to one local JSONL file."""

    def __init__(
        self,
        directory: Path,
        label: str,
        *,
        secret_values: Iterable[str] = (),
    ) -> None:
        if not directory.is_dir():
            raise FixtureTraceError("fixture trace directory is unavailable")
        self.path = directory / f"{label}-{time.time_ns()}.jsonl"
        self._started_at_ns = time.monotonic_ns()
        self._sequence = 0
        self._secret_values = tuple(
            secret_value for secret_value in secret_values if secret_value
        )
        try:
            self._file = self.path.open("x", encoding="utf-8")
        except OSError as error:
            raise FixtureTraceError("create fixture trace") from error
        self.event("fixture_trace_started", label=label)

    def event(self, kind: str, **fields: object) -> None:
        """Durably append one ordered event without exposing credentials."""
        self._sequence += 1
        payload: dict[str, object] = {
            "sequence": self._sequence,
            "elapsed_ms": (time.monotonic_ns() - self._started_at_ns) // 1_000_000,
            "kind": kind,
            **fields,
        }
        try:
            self._file.write(
                json.dumps(
                    _redact(payload, self._secret_values),
                    default=str,
                    separators=(",", ":"),
                )
                + "\n"
            )
            self._file.flush()
        except OSError as error:
            raise FixtureTraceError("write fixture trace") from error

    def close(self) -> None:
        """Close the trace after its terminal outcome has been recorded."""
        try:
            self._file.close()
        except OSError as error:
            raise FixtureTraceError("close fixture trace") from error

    def failure(self, error: Exception) -> None:
        """Record a redacted terminal failure that explains fixture failure."""
        self.event(
            "fixture_failed",
            error_type=type(error).__name__,
            error_message=str(error),
        )


def _redact(value: object, secret_values: tuple[str, ...]) -> object:
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        redacted: dict[str, object] = {}
        for key, item in mapping.items():
            if not isinstance(key, str):
                continue
            redacted[key] = (
                LOG_REDACTION_VALUE
                if _SENSITIVE_KEY.search(key)
                else _redact(item, secret_values)
            )
        return redacted
    if isinstance(value, list):
        values = cast(list[object], value)
        return [_redact(item, secret_values) for item in values]
    if isinstance(value, tuple):
        values = cast(tuple[object, ...], value)
        return tuple(_redact(item, secret_values) for item in values)
    if isinstance(value, str):
        return _redact_secret_values(value, secret_values)
    return value


def _redact_secret_values(value: str, secret_values: tuple[str, ...]) -> str:
    redacted_value = value
    for secret_value in secret_values:
        redacted_value = redacted_value.replace(secret_value, LOG_REDACTION_VALUE)
    return redacted_value
