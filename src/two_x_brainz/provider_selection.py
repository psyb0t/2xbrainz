"""Validated local persistence for runtime AIGate model settings."""

from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from two_x_brainz.constants import (
    AIGATE_REASONING_EFFORTS,
    MAX_AIGATE_MODEL_ID_CHARACTERS,
    MAX_PROVIDER_SELECTION_CONFIG_BYTES,
    PROVIDER_SELECTION_CONFIG_SCHEMA_VERSION,
)
from two_x_brainz.errors import ConfigurationError

_SCHEMA_VERSION_KEY = "schema_version"
_FLOWS_KEY = "flows"
_MODEL_KEY = "model"
_REASONING_EFFORT_KEY = "reasoning_effort"
_CONFIG_KEYS = frozenset({_SCHEMA_VERSION_KEY, _FLOWS_KEY})
_ASSIGNMENT_KEYS = frozenset({_MODEL_KEY, _REASONING_EFFORT_KEY})
_CONFIG_FILE_MODE = 0o600
_CONFIG_DIRECTORY_MODE = 0o700
_NO_FOLLOW_OPEN_FLAG = getattr(os, "O_NOFOLLOW", 0)


class ProviderFlow(StrEnum):
    """The three independent LLM jobs visible in the operator console."""

    DRAFT = "draft"
    COMMENTARY = "commentary"
    SUMMARY = "summary"


@dataclass(frozen=True, slots=True)
class ProviderAssignment:
    """One validated model and reasoning setting for a single flow."""

    model: str
    reasoning_effort: str

    def __post_init__(self) -> None:
        if not self.model or len(self.model) > MAX_AIGATE_MODEL_ID_CHARACTERS:
            raise ConfigurationError("AIGate model selection is invalid")
        if self.reasoning_effort not in AIGATE_REASONING_EFFORTS:
            raise ConfigurationError("AIGate reasoning effort is invalid")


@dataclass(frozen=True, slots=True)
class ProviderSelection:
    """Explicit assignments for reply, private coaching, and story generation."""

    draft: ProviderAssignment
    commentary: ProviderAssignment
    summary: ProviderAssignment

    @classmethod
    def uniform(cls, model: str, reasoning_effort: str) -> ProviderSelection:
        """Use one validated initial assignment for all three flows."""
        assignment = ProviderAssignment(model, reasoning_effort)
        return cls(assignment, assignment, assignment)

    def assignment(self, flow: ProviderFlow) -> ProviderAssignment:
        """Return the assignment for one validated flow."""
        if flow is ProviderFlow.DRAFT:
            return self.draft
        if flow is ProviderFlow.COMMENTARY:
            return self.commentary
        return self.summary

    def replace(
        self,
        flow: ProviderFlow,
        assignment: ProviderAssignment,
    ) -> ProviderSelection:
        """Return a selection with exactly one flow changed."""
        if flow is ProviderFlow.DRAFT:
            return ProviderSelection(assignment, self.commentary, self.summary)
        if flow is ProviderFlow.COMMENTARY:
            return ProviderSelection(self.draft, assignment, self.summary)
        return ProviderSelection(self.draft, self.commentary, assignment)

    def models(self) -> tuple[str, str, str]:
        """Return every configured model for inventory validation."""
        return (self.draft.model, self.commentary.model, self.summary.model)


class ProviderSelectionStore:
    """Read and atomically persist non-secret provider preferences."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> ProviderSelection | None:
        """Return a validated selection, ignoring stale or malformed state."""
        try:
            descriptor = os.open(self._path, os.O_RDONLY | _NO_FOLLOW_OPEN_FLAG)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno == errno.ELOOP:
                return None
            raise ConfigurationError("read provider selection configuration") from error
        try:
            file_status = os.fstat(descriptor)
            if not stat.S_ISREG(file_status.st_mode):
                return None
            config_file = os.fdopen(descriptor, "rb")
            descriptor = -1
            with config_file:
                if file_status.st_size > MAX_PROVIDER_SELECTION_CONFIG_BYTES:
                    return None
                raw = config_file.read(MAX_PROVIDER_SELECTION_CONFIG_BYTES + 1)
        except OSError as error:
            raise ConfigurationError("read provider selection configuration") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(raw) > MAX_PROVIDER_SELECTION_CONFIG_BYTES:
            return None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return _selection_from_payload(payload)

    def save(self, selection: ProviderSelection) -> None:
        """Atomically replace the config with owner-only permissions."""
        try:
            self._path.parent.mkdir(
                mode=_CONFIG_DIRECTORY_MODE,
                parents=True,
                exist_ok=True,
            )
        except OSError as error:
            raise ConfigurationError("create provider selection directory") from error
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
            )
            temporary_path = Path(temporary_name)
            os.fchmod(descriptor, _CONFIG_FILE_MODE)
            with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
                json.dump(
                    {
                        _SCHEMA_VERSION_KEY: PROVIDER_SELECTION_CONFIG_SCHEMA_VERSION,
                        _FLOWS_KEY: {
                            flow.value: _assignment_payload(selection.assignment(flow))
                            for flow in ProviderFlow
                        },
                    },
                    config_file,
                    separators=(",", ":"),
                )
                config_file.flush()
                os.fsync(config_file.fileno())
            os.replace(temporary_path, self._path)
            os.chmod(self._path, _CONFIG_FILE_MODE)
        except OSError as error:
            raise ConfigurationError(
                "write provider selection configuration"
            ) from error
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


def _selection_from_payload(payload: object) -> ProviderSelection | None:
    if not isinstance(payload, dict):
        return None
    config = cast(dict[str, object], payload)
    if (
        frozenset(config) != _CONFIG_KEYS
        or config.get(_SCHEMA_VERSION_KEY) != PROVIDER_SELECTION_CONFIG_SCHEMA_VERSION
    ):
        return None
    raw_flows = config.get(_FLOWS_KEY)
    if not isinstance(raw_flows, dict):
        return None
    flows = cast(dict[str, object], raw_flows)
    if frozenset(flows) != frozenset(flow.value for flow in ProviderFlow):
        return None
    assignments = {
        flow: _assignment_from_payload(flows.get(flow.value)) for flow in ProviderFlow
    }
    if any(assignment is None for assignment in assignments.values()):
        return None
    return ProviderSelection(
        draft=cast(ProviderAssignment, assignments[ProviderFlow.DRAFT]),
        commentary=cast(
            ProviderAssignment,
            assignments[ProviderFlow.COMMENTARY],
        ),
        summary=cast(ProviderAssignment, assignments[ProviderFlow.SUMMARY]),
    )


def _assignment_from_payload(payload: object) -> ProviderAssignment | None:
    if not isinstance(payload, dict):
        return None
    config = cast(dict[str, object], payload)
    if frozenset(config) != _ASSIGNMENT_KEYS:
        return None
    model = config.get(_MODEL_KEY)
    reasoning_effort = config.get(_REASONING_EFFORT_KEY)
    if not isinstance(model, str) or not isinstance(reasoning_effort, str):
        return None
    try:
        return ProviderAssignment(model=model, reasoning_effort=reasoning_effort)
    except ConfigurationError:
        return None


def _assignment_payload(assignment: ProviderAssignment) -> dict[str, str]:
    return {
        _MODEL_KEY: assignment.model,
        _REASONING_EFFORT_KEY: assignment.reasoning_effort,
    }
