"""Configuration loading and boundary validation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from two_x_brainz.constants import (
    DEFAULT_AIGATE_COACH_MODEL,
    DEFAULT_AIGATE_REASONING_EFFORT,
    DEFAULT_AIGATE_REPLY_MODEL,
    DEFAULT_AIGATE_SUMMARY_MODEL,
    DEFAULT_AIGATE_URL,
    DEFAULT_LOG_FILE,
    DEFAULT_LOG_FILENAME,
    DEFAULT_LOG_LEVEL,
    DEFAULT_TALKIES_MODEL,
    DEFAULT_WEB_RESEARCH_ENABLED,
    ENV_AIGATE_TOKEN,
    ENV_AIGATE_URL,
    ENV_LOG_DIRECTORY,
    ENV_LOG_FILE,
    ENV_LOG_LEVEL,
    TALKIES_STREAM_PATH,
)
from two_x_brainz.errors import ConfigurationError

_VALID_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})
_AIGATE_SCHEMES = frozenset({"http", "https"})
_AIGATE_API_PATH_SUFFIX = "/v1"
_TALKIES_GATEWAY_PREFIX = "/talkies"
_WEBSOCKET_SCHEME_BY_HTTP_SCHEME = {"http": "ws", "https": "wss"}


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
    aigate_token: str | None = field(repr=False)
    log_level: str
    log_file: Path
    session_brief: str | None = field(default=None, repr=False)
    web_research_enabled: bool = DEFAULT_WEB_RESEARCH_ENABLED
    aigate_reply_model: str = DEFAULT_AIGATE_REPLY_MODEL
    aigate_coach_model: str = DEFAULT_AIGATE_COACH_MODEL
    aigate_summary_model: str = DEFAULT_AIGATE_SUMMARY_MODEL
    aigate_reply_reasoning_effort: str = DEFAULT_AIGATE_REASONING_EFFORT
    aigate_coach_reasoning_effort: str = DEFAULT_AIGATE_REASONING_EFFORT
    aigate_summary_reasoning_effort: str = DEFAULT_AIGATE_REASONING_EFFORT

    @classmethod
    def from_environment(cls) -> Settings:
        """Load settings from the process environment and fail fast on errors."""
        aigate_url = _read_aigate_url()
        talkies_ws_url = _talkies_stream_url(aigate_url)
        aigate_token = _read_optional_text(ENV_AIGATE_TOKEN)
        log_level = _read_required_text(ENV_LOG_LEVEL, DEFAULT_LOG_LEVEL).upper()
        if log_level not in _VALID_LOG_LEVELS:
            raise ConfigurationError(f"{ENV_LOG_LEVEL} must be a standard log level")

        log_file = _read_log_file()
        return cls(
            talkies_ws_url=talkies_ws_url,
            talkies_model=DEFAULT_TALKIES_MODEL,
            talkies_token=aigate_token,
            aigate_url=aigate_url,
            aigate_reply_model=DEFAULT_AIGATE_REPLY_MODEL,
            aigate_coach_model=DEFAULT_AIGATE_COACH_MODEL,
            aigate_summary_model=DEFAULT_AIGATE_SUMMARY_MODEL,
            aigate_reply_reasoning_effort=DEFAULT_AIGATE_REASONING_EFFORT,
            aigate_coach_reasoning_effort=DEFAULT_AIGATE_REASONING_EFFORT,
            aigate_summary_reasoning_effort=DEFAULT_AIGATE_REASONING_EFFORT,
            aigate_token=aigate_token,
            session_brief=None,
            log_level=log_level,
            log_file=log_file,
            web_research_enabled=DEFAULT_WEB_RESEARCH_ENABLED,
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


def _read_aigate_url() -> str:
    aigate_url = _read_url(ENV_AIGATE_URL, DEFAULT_AIGATE_URL, _AIGATE_SCHEMES)
    if not urlparse(aigate_url).path.rstrip("/").endswith(_AIGATE_API_PATH_SUFFIX):
        raise ConfigurationError(f"{ENV_AIGATE_URL} must end in /v1")
    return aigate_url


def _talkies_stream_url(aigate_url: str) -> str:
    parsed = urlparse(aigate_url)
    gateway_path = parsed.path.rstrip("/").removesuffix(_AIGATE_API_PATH_SUFFIX)
    return urlunparse(
        (
            _WEBSOCKET_SCHEME_BY_HTTP_SCHEME[parsed.scheme],
            parsed.netloc,
            f"{gateway_path}{_TALKIES_GATEWAY_PREFIX}{TALKIES_STREAM_PATH}",
            "",
            "",
            "",
        )
    )


def _read_required_text(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    if not value:
        raise ConfigurationError(f"{name} must not be empty")
    return value


def _read_optional_text(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _read_absolute_path(name: str, default: str) -> Path:
    path = Path(_read_required_text(name, default)).expanduser()
    if not path.is_absolute():
        raise ConfigurationError(f"{name} must be an absolute path")
    return path


def _read_log_file() -> Path:
    log_directory = _read_optional_text(ENV_LOG_DIRECTORY)
    if log_directory is not None:
        return (
            _read_absolute_path(ENV_LOG_DIRECTORY, log_directory) / DEFAULT_LOG_FILENAME
        )

    log_file = Path(_read_required_text(ENV_LOG_FILE, DEFAULT_LOG_FILE))
    if not log_file.is_absolute():
        raise ConfigurationError(f"{ENV_LOG_FILE} must be an absolute path")
    return log_file
