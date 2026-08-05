"""Structured, redacted logging configured by the CLI entry point."""

from __future__ import annotations

import contextvars
import io
import json
import logging
import os
import re
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
_SESSION_LOG_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
_SESSION_LOG_SEPARATOR = "_"
_SESSION_LOG_COLLISION_SEPARATOR = "-"
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self) -> io.TextIOWrapper:
        descriptor = os.open(
            self.baseFilename,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            _PRIVATE_FILE_MODE,
        )
        os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        return cast(
            io.TextIOWrapper,
            open(
                descriptor,
                mode=self.mode,
                encoding=self.encoding,
                errors=self.errors,
                closefd=True,
            ),
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


def allocate_session_log_file(
    base_log_file: Path,
    *,
    timestamp: datetime | None = None,
) -> Path:
    """Reserve a unique UTC-prefixed log file for one live session."""
    _create_log_directory(base_log_file.parent)
    session_timestamp = (timestamp or datetime.now(UTC)).astimezone(UTC)
    prefix = session_timestamp.strftime(_SESSION_LOG_TIMESTAMP_FORMAT)
    stem = base_log_file.stem
    suffix = base_log_file.suffix
    collision_index = 0

    while True:
        collision_suffix = (
            ""
            if collision_index == 0
            else f"{_SESSION_LOG_COLLISION_SEPARATOR}{collision_index}"
        )
        candidate = base_log_file.with_name(
            f"{prefix}{_SESSION_LOG_SEPARATOR}{stem}{collision_suffix}{suffix}"
        )
        try:
            descriptor = os.open(
                candidate,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                _PRIVATE_FILE_MODE,
            )
            os.close(descriptor)
        except FileExistsError:
            collision_index += 1
            continue
        return candidate


def configure_logging(level: str, log_file: Path) -> None:
    """Configure the persistent rotating JSON log before application work starts."""
    _create_log_directory(log_file.parent)
    formatter = RedactingJsonFormatter()
    scope_filter = ScopeFilter()
    handlers: list[logging.Handler] = [
        _PrivateRotatingFileHandler(
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


def _create_log_directory(directory: Path) -> None:
    if directory.exists():
        return
    directory.mkdir(parents=True, mode=_PRIVATE_DIRECTORY_MODE)
    directory.chmod(_PRIVATE_DIRECTORY_MODE)


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
