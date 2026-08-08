from __future__ import annotations

import unittest

from two_x_brainz.constants import MAX_AIGATE_MODEL_ID_CHARACTERS
from two_x_brainz.errors import ConfigurationError
from two_x_brainz.provider_selection import (
    ProviderAssignment,
    ProviderFlow,
    ProviderSelection,
)


class ProviderSelectionTests(unittest.TestCase):
    def test_invalid_assignment_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "reasoning effort"):
            ProviderAssignment("model-a", "extreme")
        with self.assertRaisesRegex(ConfigurationError, "model selection"):
            ProviderAssignment(
                "m" * (MAX_AIGATE_MODEL_ID_CHARACTERS + 1),
                "none",
            )

    def test_replace_changes_only_the_selected_flow(self) -> None:
        original = ProviderSelection.uniform("model-a", "none")

        updated = original.replace(
            ProviderFlow.SUMMARY,
            ProviderAssignment("model-b", "high"),
        )

        self.assertEqual(updated.draft, original.draft)
        self.assertEqual(updated.commentary, original.commentary)
        self.assertEqual(updated.summary, ProviderAssignment("model-b", "high"))


if __name__ == "__main__":
    unittest.main()
