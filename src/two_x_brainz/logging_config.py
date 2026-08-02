"""Structured, redacted logging configured by the CLI entry point."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, cast

from two_x_brainz.constants import (
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_ROTATION_BYTES,
    LOG_REDACTION_VALUE,
)

_SCOPE: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "log_scope", default=None
)
_SENSITIVE_KEY = re.compile(
    r"password|token|secret|api[_-]?key|authorization|cookie", re.IGNORECASE
)


def with_scope(**values: str) -> contextvars.Token[dict[str, str] | None]:
    """Add safe correlation fields to the active log context."""
    return _SCOPE.set({**(_SCOPE.get() or {}), **values})


class ScopeFilter(logging.Filter):
    """Attach context-local correlation fields to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in (_SCOPE.get() or {}).items():
            setattr(record, key, value)
        return True


class RedactingJsonFormatter(logging.Formatter):
    """Render JSON logs while removing sensitive nested values."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "file": record.filename,
            "line": record.lineno,
            "func": record.funcName,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_RECORD_KEYS and not key.startswith("_"):
                payload[key] = value
        return json.dumps(_redact(payload), default=str, separators=(",", ":"))


def configure_logging(level: str, log_file: Path) -> None:
    """Configure stderr and rotating JSON logs before application work starts."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = RedactingJsonFormatter()
    scope_filter = ScopeFilter()
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stderr),
        RotatingFileHandler(
            log_file,
            maxBytes=DEFAULT_LOG_ROTATION_BYTES,
            backupCount=DEFAULT_LOG_BACKUP_COUNT,
            encoding="utf-8",
        ),
    ]
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(scope_filter)

    logging.basicConfig(level=level, handlers=handlers, force=True)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        raw_mapping = cast(dict[object, Any], value)
        redacted: dict[str, Any] = {}
        for key, item in raw_mapping.items():
            if not isinstance(key, str):
                continue
            redacted[key] = (
                LOG_REDACTION_VALUE if _SENSITIVE_KEY.search(key) else _redact(item)
            )
        return redacted
    if isinstance(value, list):
        raw_values = cast(list[Any], value)
        return [_redact(item) for item in raw_values]
    if isinstance(value, tuple):
        raw_values = cast(tuple[Any, ...], value)
        return tuple(_redact(item) for item in raw_values)
    return value


_STANDARD_LOG_RECORD_KEYS = frozenset(logging.makeLogRecord({}).__dict__)
