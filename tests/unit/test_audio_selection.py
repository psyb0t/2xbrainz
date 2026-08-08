from __future__ import annotations

import asyncio
import unittest

from two_x_brainz.audio_selection import (
    AudioSelection,
    prepare_audio_selection_setup,
)
from two_x_brainz.errors import CaptureError

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
    def test_first_selection_uses_stable_names_without_writing_a_file(self) -> None:
        setup = self._setup()

        self.assertIsNone(setup.selection)
        selection = setup.select(0, 0)

        self.assertEqual(
            selection,
            AudioSelection(mic_node="mic-usb", system_node="speaker-usb.monitor"),
        )

    def test_invalid_setup_index_does_not_change_selection(self) -> None:
        setup = self._setup()

        with self.assertRaisesRegex(CaptureError, "not available"):
            setup.select(99, 0)

        self.assertIsNone(setup.selection)

    def test_browser_persisted_node_names_are_validated(self) -> None:
        setup = self._setup()

        selection = setup.select_nodes("mic-usb", "speaker-usb.monitor")

        self.assertEqual(
            selection,
            AudioSelection("mic-usb", "speaker-usb.monitor"),
        )
        with self.assertRaisesRegex(CaptureError, "not available"):
            setup.select_nodes("missing", "speaker-usb.monitor")

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
        )

        self.assertEqual(setup.system_monitors[0].name, "speaker-usb")
        selection = setup.select(0, 0)
        self.assertEqual(selection.system_node, "speaker-usb")
        self.assertTrue(selection.system_capture_sink)

    def test_recordable_monitor_source_does_not_request_sink_capture(self) -> None:
        selection = self._setup().select(0, 0)

        self.assertEqual(selection.system_node, "speaker-usb.monitor")
        self.assertFalse(selection.system_capture_sink)

    def test_no_compatible_system_output_opens_recoverable_setup(self) -> None:
        nodes = tuple(
            node
            for node in _NODES
            if node["media_class"] != "Audio/Sink"
            and not node["name"].endswith(".monitor")
        )

        setup = prepare_audio_selection_setup(
            nodes=nodes,
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

    def test_runtime_change_wakes_capture_workers(self) -> None:
        async def exercise() -> None:
            setup = self._setup()
            revision = setup.revision
            changed = asyncio.create_task(setup.wait_for_change(revision))
            setup.notify_runtime_change()
            self.assertEqual(await changed, revision + 1)

        asyncio.run(exercise())

    def _setup(self):
        return prepare_audio_selection_setup(nodes=_NODES)
