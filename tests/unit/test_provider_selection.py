from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from two_x_brainz.constants import (
    MAX_AIGATE_MODEL_ID_CHARACTERS,
    MAX_PROVIDER_SELECTION_CONFIG_BYTES,
)
from two_x_brainz.errors import ConfigurationError
from two_x_brainz.provider_selection import (
    ProviderAssignment,
    ProviderFlow,
    ProviderSelection,
    ProviderSelectionStore,
)


class ProviderSelectionStoreTests(unittest.TestCase):
    def test_round_trip_uses_owner_only_file_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "provider.json"
            store = ProviderSelectionStore(path)
            selection = ProviderSelection(
                draft=ProviderAssignment("reply-model", "minimal"),
                commentary=ProviderAssignment("coach-model", "low"),
                summary=ProviderAssignment("story-model", "high"),
            )

            store.save(selection)

            self.assertEqual(store.load(), selection)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_malformed_extra_and_symlinked_configs_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "provider.json"
            store = ProviderSelectionStore(path)
            path.write_text("{bad-json", encoding="utf-8")
            self.assertIsNone(store.load())

            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "model": "model-a",
                        "reasoning_effort": "high",
                        "unexpected": True,
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNone(store.load())

            path.unlink()
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            path.symlink_to(target)
            self.assertIsNone(store.load())

    def test_invalid_selection_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "reasoning effort"):
            ProviderAssignment("model-a", "extreme")
        with self.assertRaisesRegex(ConfigurationError, "model selection"):
            ProviderAssignment(
                "m" * (MAX_AIGATE_MODEL_ID_CHARACTERS + 1),
                "none",
            )

    def test_oversized_and_non_regular_configs_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            oversized = root / "oversized.json"
            oversized.write_bytes(b"x" * (MAX_PROVIDER_SELECTION_CONFIG_BYTES + 1))
            directory = root / "directory.json"
            directory.mkdir()

            self.assertIsNone(ProviderSelectionStore(oversized).load())
            self.assertIsNone(ProviderSelectionStore(directory).load())

    def test_partial_v2_config_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "provider.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "flows": {
                            "draft": {
                                "model": "model-a",
                                "reasoning_effort": "none",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertIsNone(ProviderSelectionStore(path).load())

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
