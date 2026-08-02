"""Configuration loading and boundary validation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

from two_x_brainz.constants import (
    DEFAULT_AIGATE_MODE,
    DEFAULT_AIGATE_URL,
    DEFAULT_LOG_FILE,
    DEFAULT_LOG_LEVEL,
    DEFAULT_TALKIES_MODEL,
    DEFAULT_TALKIES_WS_URL,
    ENV_AIGATE_MODE,
    ENV_AIGATE_MODEL,
    ENV_AIGATE_TOKEN,
    ENV_AIGATE_URL,
    ENV_LOG_FILE,
    ENV_LOG_LEVEL,
    ENV_REMOTE_TEXT_ENABLED,
    ENV_TALKIES_MODEL,
    ENV_TALKIES_TOKEN,
    ENV_TALKIES_WS_URL,
    REMOTE_TEXT_ENABLED_VALUE,
)
from two_x_brainz.errors import ConfigurationError

_VALID_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})
_TALKIES_SCHEMES = frozenset({"ws", "wss"})
_AIGATE_SCHEMES = frozenset({"http", "https"})
_DEFAULT_URL_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


class AIGateMode(StrEnum):
    """Whether the configured text gateway is local or transmits externally."""

    LOCAL = "local"
    REMOTE = "remote"


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings; credentials are never serialized for display."""

    talkies_ws_url: str
    talkies_model: str
    # repr=False is what makes the docstring above true. The log redactor keys off
    # field NAMES and only walks dicts, lists and tuples, so a Settings passed
    # whole -- logger.info("...", extra={"settings": settings}) -- reaches
    # json.dumps(default=str) intact and serializes via this repr. Dropping the
    # tokens from it closes that path at the source.
    talkies_token: str | None = field(repr=False)
    aigate_url: str
    aigate_mode: AIGateMode
    aigate_model: str | None
    aigate_token: str | None = field(repr=False)
    log_level: str
    log_file: Path

    @classmethod
    def from_environment(cls) -> Settings:
        """Load settings from the process environment and fail fast on errors."""
        talkies_ws_url = _read_url(
            ENV_TALKIES_WS_URL,
            DEFAULT_TALKIES_WS_URL,
            _TALKIES_SCHEMES,
        )
        aigate_url = _read_url(ENV_AIGATE_URL, DEFAULT_AIGATE_URL, _AIGATE_SCHEMES)
        aigate_mode = _read_aigate_mode()
        _require_remote_opt_in(aigate_mode)
        talkies_model = _read_required_text(ENV_TALKIES_MODEL, DEFAULT_TALKIES_MODEL)
        aigate_model = _read_optional_text(ENV_AIGATE_MODEL)
        aigate_token = _read_optional_text(ENV_AIGATE_TOKEN)
        talkies_token = _read_optional_text(ENV_TALKIES_TOKEN)
        talkies_token = _resolve_talkies_token(
            talkies_token=talkies_token,
            aigate_token=aigate_token,
            talkies_url=talkies_ws_url,
            aigate_url=aigate_url,
        )
        log_level = _read_required_text(ENV_LOG_LEVEL, DEFAULT_LOG_LEVEL).upper()
        if log_level not in _VALID_LOG_LEVELS:
            raise ConfigurationError(f"{ENV_LOG_LEVEL} must be a standard log level")

        log_file = Path(_read_required_text(ENV_LOG_FILE, DEFAULT_LOG_FILE))
        if not log_file.is_absolute():
            raise ConfigurationError(f"{ENV_LOG_FILE} must be an absolute path")

        return cls(
            talkies_ws_url=talkies_ws_url,
            talkies_model=talkies_model,
            talkies_token=talkies_token,
            aigate_url=aigate_url,
            aigate_mode=aigate_mode,
            aigate_model=aigate_model,
            aigate_token=aigate_token,
            log_level=log_level,
            log_file=log_file,
        )


def _read_url(name: str, default: str, schemes: frozenset[str]) -> str:
    value = _read_required_text(name, default)
    parsed = urlparse(value)
    if parsed.scheme not in schemes or not parsed.hostname:
        allowed = ", ".join(sorted(schemes))
        raise ConfigurationError(f"{name} must be an absolute {allowed} URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError(
            f"{name} must not contain credentials, query, or fragment"
        )
    return value.rstrip("/")


def _read_required_text(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    if not value:
        raise ConfigurationError(f"{name} must not be empty")
    return value


def _read_optional_text(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _resolve_talkies_token(
    *,
    talkies_token: str | None,
    aigate_token: str | None,
    talkies_url: str,
    aigate_url: str,
) -> str | None:
    if talkies_token is not None or aigate_token is None:
        return talkies_token
    if not _same_url_authority(talkies_url, aigate_url):
        return None
    return aigate_token


def _same_url_authority(left_url: str, right_url: str) -> bool:
    left = urlparse(left_url)
    right = urlparse(right_url)
    return left.hostname == right.hostname and _url_port(
        left.scheme, left.port
    ) == _url_port(right.scheme, right.port)


def _url_port(scheme: str, port: int | None) -> int | None:
    if port is not None:
        return port
    return _DEFAULT_URL_PORTS.get(scheme)


def _read_aigate_mode() -> AIGateMode:
    value = _read_required_text(ENV_AIGATE_MODE, DEFAULT_AIGATE_MODE).lower()
    try:
        return AIGateMode(value)
    except ValueError as error:
        raise ConfigurationError(
            f"{ENV_AIGATE_MODE} must be local or remote"
        ) from error


def _require_remote_opt_in(aigate_mode: AIGateMode) -> None:
    if aigate_mode is AIGateMode.LOCAL:
        return
    remote_text_enabled = os.environ.get(ENV_REMOTE_TEXT_ENABLED, "").strip().lower()
    if remote_text_enabled == REMOTE_TEXT_ENABLED_VALUE:
        return
    raise ConfigurationError(
        f"{ENV_REMOTE_TEXT_ENABLED}=true is required when {ENV_AIGATE_MODE}=remote"
    )
