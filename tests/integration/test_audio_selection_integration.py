from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from two_x_brainz.audio_selection import (
    AudioSelectionStore,
    prepare_audio_selection_setup,
)
from two_x_brainz.capture import list_pipewire_nodes

_PIPEWIRE_DUMP = json.dumps(
    [
        {
            "id": 12,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "node.name": "mic-usb",
                    "media.class": "Audio/Source",
                    "node.description": "Desk microphone",
                }
            },
        },
        {
            "id": 42,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "node.name": "speaker-usb.monitor",
                    "media.class": "Audio/Source",
                    "node.description": "Desk speakers monitor",
                }
            },
        },
        {
            "id": 43,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "node.name": "speaker-usb",
                    "media.class": "Audio/Sink",
                }
            },
        },
        {
            "id": 1,
            "type": "PipeWire:Interface:Metadata",
            "info": {"props": {"metadata.name": "default"}},
            "metadata": [
                {
                    "key": "default.audio.source",
                    "value": '{"name":"mic-usb"}',
                },
                {
                    "key": "default.audio.sink",
                    "value": '{"name":"speaker-usb"}',
                },
            ],
        },
    ]
)
_EXECUTABLE_TEMP_DIRECTORY = Path("/work-env")


class AudioSelectionIntegrationTests(unittest.TestCase):
    def test_fixture_pipewire_dump_persists_then_reuses_selection(self) -> None:
        with (
            _fake_pw_dump(_PIPEWIRE_DUMP) as command,
            tempfile.TemporaryDirectory() as temporary_directory,
            patch("two_x_brainz.capture._PIPEWIRE_DUMP_COMMAND", str(command)),
        ):
            nodes = asyncio.run(list_pipewire_nodes())
            store = AudioSelectionStore(Path(temporary_directory) / "audio.json")
            first_setup = prepare_audio_selection_setup(
                nodes=nodes,
                store=store,
                mic_node=None,
                system_node=None,
            )
            first_selection = first_setup.select(0, 0)
            second_setup = prepare_audio_selection_setup(
                nodes=nodes,
                store=store,
                mic_node=None,
                system_node=None,
            )
            second_selection = second_setup.selection

        self.assertEqual(first_selection, second_selection)
        self.assertEqual(first_selection.mic_node, "mic-usb")
        self.assertEqual(first_selection.system_node, "speaker-usb.monitor")
        self.assertEqual(first_selection.mic_label, "Desk microphone")
        self.assertEqual(first_selection.system_label, "Desk speakers monitor")

    def test_direct_sink_fallback_is_selectable_from_pipewire_dump(self) -> None:
        payload = json.loads(_PIPEWIRE_DUMP)
        del payload[1]
        dump = json.dumps(payload)
        with (
            _fake_pw_dump(dump) as command,
            tempfile.TemporaryDirectory() as temporary_directory,
            patch("two_x_brainz.capture._PIPEWIRE_DUMP_COMMAND", str(command)),
        ):
            nodes = asyncio.run(list_pipewire_nodes())
            setup = prepare_audio_selection_setup(
                nodes=nodes,
                store=AudioSelectionStore(Path(temporary_directory) / "audio.json"),
                mic_node=None,
                system_node=None,
            )
            selection = setup.select(0, 0)

        self.assertEqual(selection.system_node, "speaker-usb")


@contextmanager
def _fake_pw_dump(payload: str) -> Iterator[Path]:
    temporary_directory_argument = (
        str(_EXECUTABLE_TEMP_DIRECTORY) if _EXECUTABLE_TEMP_DIRECTORY.is_dir() else None
    )
    with tempfile.TemporaryDirectory(
        dir=temporary_directory_argument
    ) as temporary_directory:
        command = Path(temporary_directory) / "pw-dump"
        command.write_text(f"#!/bin/sh\nprintf '%s' '{payload}'\n")
        command.chmod(0o700)
        yield command
