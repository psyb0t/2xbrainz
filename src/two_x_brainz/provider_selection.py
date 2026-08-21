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
    """The independent LLM jobs visible in the operator console."""

    DRAFT = "draft"
    FAST_DRAFT = "fast_draft"
    COMMENTARY = "commentary"
    SUMMARY = "summary"
    RESEARCH = "research"


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
    """Explicit assignments for every independent provider flow."""

    draft: ProviderAssignment
    fast_draft: ProviderAssignment
    commentary: ProviderAssignment
    summary: ProviderAssignment
    research: ProviderAssignment

    @classmethod
    def uniform(cls, model: str, reasoning_effort: str) -> ProviderSelection:
        """Use one validated initial assignment for all flows."""
        assignment = ProviderAssignment(model, reasoning_effort)
        return cls(
            draft=assignment,
            fast_draft=assignment,
            commentary=assignment,
            summary=assignment,
            research=assignment,
        )

    def assignment(self, flow: ProviderFlow) -> ProviderAssignment:
        """Return the assignment for one validated flow."""
        if flow is ProviderFlow.DRAFT:
            return self.draft
        if flow is ProviderFlow.FAST_DRAFT:
            return self.fast_draft
        if flow is ProviderFlow.COMMENTARY:
            return self.commentary
        if flow is ProviderFlow.SUMMARY:
            return self.summary
        return self.research

    def replace(
        self,
        flow: ProviderFlow,
        assignment: ProviderAssignment,
    ) -> ProviderSelection:
        """Return a selection with exactly one flow changed."""
        overrides = {flow.value: assignment}
        return ProviderSelection(
            draft=overrides.get(ProviderFlow.DRAFT.value, self.draft),
            fast_draft=overrides.get(ProviderFlow.FAST_DRAFT.value, self.fast_draft),
            commentary=overrides.get(ProviderFlow.COMMENTARY.value, self.commentary),
            summary=overrides.get(ProviderFlow.SUMMARY.value, self.summary),
            research=overrides.get(ProviderFlow.RESEARCH.value, self.research),
        )

    def models(self) -> tuple[str, str, str, str, str]:
        """Return every configured model for inventory validation."""
        return (
            self.draft.model,
            self.fast_draft.model,
            self.commentary.model,
            self.summary.model,
            self.research.model,
        )
