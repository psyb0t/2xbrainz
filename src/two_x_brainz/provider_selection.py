"""Validated runtime AIGate model settings."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from two_x_brainz.constants import (
    AIGATE_REASONING_EFFORTS,
    MAX_AIGATE_MODEL_ID_CHARACTERS,
)
from two_x_brainz.errors import ConfigurationError


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
