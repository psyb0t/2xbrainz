from __future__ import annotations

import asyncio
import json
import stat
import tempfile
import unittest
from pathlib import Path

from two_x_brainz.audio_selection import (
    AudioSelection,
    AudioSelectionStore,
    prepare_audio_selection_setup,
)
from two_x_brainz.errors import CaptureError, ConfigurationError

_NODES = (
    {
        "id": "12",
        "name": "mic-usb",
        "media_class": "Audio/Source",
        "description": "Desk microphone",
        "default_role": "source",
    },
    {
        "id": "13",
        "name": "alsa_output.monitor",
        "media_class": "Audio/Source",
    },
    {
        "id": "42",
        "name": "speaker-usb.monitor",
        "media_class": "Audio/Source",
        "description": "Desk speakers monitor",
        "default_role": "sink",
    },
    {
        "id": "43",
        "name": "speaker-usb",
        "media_class": "Audio/Sink",
    },
)


class AudioSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self._config_path = Path(self._temporary_directory.name) / "audio.json"
        self._store = AudioSelectionStore(self._config_path)

    def test_first_selection_saves_stable_names_with_private_mode(self) -> None:
        setup = self._setup()

        self.assertIsNone(setup.selection)
        selection = setup.select(0, 0)

        self.assertEqual(
            selection,
            AudioSelection(mic_node="mic-usb", system_node="speaker-usb.monitor"),
        )
        self.assertEqual(self._store.load(), selection)
        self.assertEqual(stat.S_IMODE(self._config_path.stat().st_mode), 0o600)
        self.assertEqual(
            json.loads(self._config_path.read_text()),
            {
                "schema_version": 1,
                "mic_node": "mic-usb",
                "system_node": "speaker-usb.monitor",
            },
        )

    def test_available_saved_selection_skips_the_setup_screen(self) -> None:
        saved_selection = AudioSelection("mic-usb", "speaker-usb.monitor")
        self._store.save(saved_selection)

        setup = self._setup()

        self.assertEqual(setup.selection, saved_selection)

    def test_stale_saved_selection_reopens_the_selector(self) -> None:
        self._store.save(AudioSelection("missing-mic", "speaker-usb.monitor"))

        setup = self._setup()
        selection = setup.select(0, 0)

        self.assertEqual(selection.mic_node, "mic-usb")
        self.assertEqual(self._store.load(), selection)

    def test_invalid_setup_index_does_not_persist_a_bad_value(self) -> None:
        setup = self._setup()

        with self.assertRaisesRegex(CaptureError, "not available"):
            setup.select(99, 0)

        self.assertIsNone(self._store.load())

    def test_explicit_paired_ids_are_validated_and_saved(self) -> None:
        setup = self._setup(
            mic_node="12",
            system_node="42",
        )

        self.assertEqual(setup.selection, AudioSelection("12", "42"))
        self.assertEqual(self._store.load(), setup.selection)

    def test_setup_shows_friendly_labels_and_default_recommendations(self) -> None:
        setup = self._setup()

        self.assertEqual(
            setup.microphones[0].setup_label,
            "Desk microphone [DEFAULT]\n  node: mic-usb [12]",
        )
        self.assertEqual(
            setup.system_monitors[0].setup_label,
            "Desk speakers monitor [DEFAULT]\n  node: speaker-usb.monitor [42]",
        )

    def test_direct_system_sink_is_available_when_no_monitor_is_visible(self) -> None:
        nodes = tuple(node for node in _NODES if not node["name"].endswith(".monitor"))

        setup = prepare_audio_selection_setup(
            nodes=nodes,
            store=self._store,
            mic_node=None,
            system_node=None,
        )

        self.assertEqual(setup.system_monitors[0].name, "speaker-usb")
        selection = setup.select(0, 0)
        self.assertEqual(selection.system_node, "speaker-usb")
        self.assertTrue(selection.system_capture_sink)

    def test_recordable_monitor_source_does_not_request_sink_capture(self) -> None:
        selection = self._setup().select(0, 0)

        self.assertEqual(selection.system_node, "speaker-usb.monitor")
        self.assertFalse(selection.system_capture_sink)

    def test_unpaired_explicit_override_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "supplied together"):
            self._setup(mic_node="12")

    def test_invalid_saved_config_reopens_the_selector(self) -> None:
        self._config_path.write_text("{not-json")

        setup = self._setup()

        self.assertIsNone(setup.selection)

    def test_symlinked_saved_config_is_ignored(self) -> None:
        target = self._config_path.with_name("target.json")
        target.write_text("{}")
        self._config_path.symlink_to(target)

        self.assertIsNone(self._store.load())

    def test_no_compatible_system_output_opens_recoverable_setup(self) -> None:
        nodes = tuple(
            node
            for node in _NODES
            if node["media_class"] != "Audio/Sink"
            and not node["name"].endswith(".monitor")
        )

        setup = prepare_audio_selection_setup(
            nodes=nodes,
            store=self._store,
            mic_node=None,
            system_node=None,
        )

        self.assertEqual(setup.system_monitors, ())
        self.assertFalse(setup.selection_available)

    def test_refresh_marks_a_disconnected_selection_unavailable_then_recovers(
        self,
    ) -> None:
        setup = self._setup()
        selection = setup.select(0, 0)

        setup.refresh([])
        self.assertEqual(setup.selection, selection)
        self.assertFalse(setup.selection_available)

        setup.refresh(_NODES)
        self.assertTrue(setup.selection_available)

    def test_replacement_selection_wakes_capture_workers(self) -> None:
        async def exercise() -> None:
            setup = self._setup()
            setup.select(0, 0)
            revision = setup.revision
            changed = asyncio.create_task(setup.wait_for_change(revision))
            setup.refresh(
                (
                    *_NODES,
                    {
                        "id": "50",
                        "name": "backup-mic",
                        "media_class": "Audio/Source",
                    },
                    {
                        "id": "51",
                        "name": "backup-output.monitor",
                        "media_class": "Audio/Source",
                    },
                )
            )

            setup.select(1, 1)

            self.assertEqual(await changed, revision + 1)

        asyncio.run(exercise())

    def _setup(
        self,
        *,
        mic_node: str | None = None,
        system_node: str | None = None,
    ):
        return prepare_audio_selection_setup(
            nodes=_NODES,
            store=self._store,
            mic_node=mic_node,
            system_node=system_node,
        )
