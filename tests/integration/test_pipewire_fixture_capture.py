from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from collections.abc import AsyncIterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from two_x_brainz.audio import load_wav_fixture
from two_x_brainz.capture import PipeWireSource
from two_x_brainz.constants import DEFAULT_FRAME_BYTES
from two_x_brainz.errors import CaptureError

_FIXTURE_PATH = Path("tests/fixtures/commons-audio-cc0.wav")
_EXECUTABLE_TEMP_DIRECTORY = Path("/work-env")


class PipeWireFixtureCaptureIntegrationTests(unittest.TestCase):
    def test_two_fake_devices_emit_normalized_cc0_speech_concurrently(self) -> None:
        asyncio.run(self._assert_fake_devices_stream_pcm())

    def test_fake_device_with_incomplete_pcm_fails_at_capture_boundary(self) -> None:
        asyncio.run(self._assert_incomplete_pcm_is_rejected())

    def test_fake_device_without_pcm_fails_at_capture_boundary(self) -> None:
        asyncio.run(self._assert_empty_pcm_is_rejected())

    async def _assert_fake_devices_stream_pcm(self) -> None:
        fixture = load_wav_fixture(_FIXTURE_PATH)
        with (
            _fake_pw_record(fixture.pcm16le) as command,
            patch(
                "two_x_brainz.capture.PIPEWIRE_RECORD_COMMAND",
                str(command),
            ),
        ):
            microphone_frames, system_frames = await asyncio.gather(
                _collect(PipeWireSource("fake-microphone").frames()),
                _collect(PipeWireSource("fake-system").frames()),
            )

        self.assertEqual(b"".join(microphone_frames), fixture.pcm16le)
        self.assertEqual(microphone_frames, system_frames)
        self.assertTrue(
            all(len(frame) == DEFAULT_FRAME_BYTES for frame in microphone_frames)
        )

    async def _assert_incomplete_pcm_is_rejected(self) -> None:
        fixture = load_wav_fixture(_FIXTURE_PATH)
        with (
            _fake_pw_record(fixture.pcm16le[:-1]) as command,
            patch(
                "two_x_brainz.capture.PIPEWIRE_RECORD_COMMAND",
                str(command),
            ),
            self.assertRaisesRegex(CaptureError, "odd-length"),
        ):
            await _collect(PipeWireSource("fake-microphone").frames())

    async def _assert_empty_pcm_is_rejected(self) -> None:
        with (
            _fake_pw_record(b"") as command,
            patch(
                "two_x_brainz.capture.PIPEWIRE_RECORD_COMMAND",
                str(command),
            ),
            self.assertRaises(CaptureError),
        ):
            await _collect(PipeWireSource("fake-microphone").frames())


@contextmanager
def _fake_pw_record(pcm16le: bytes) -> Iterator[Path]:
    temporary_directory_argument = (
        str(_EXECUTABLE_TEMP_DIRECTORY) if _EXECUTABLE_TEMP_DIRECTORY.is_dir() else None
    )
    with tempfile.TemporaryDirectory(
        dir=temporary_directory_argument
    ) as temporary_directory:
        directory = Path(temporary_directory)
        pcm_path = directory / "fixture.pcm"
        command_path = directory / "pw-record"
        pcm_path.write_bytes(pcm16le)
        command_path.write_text('#!/bin/sh\ncat "$TWOXBRAINZ_TEST_PCM"\n')
        command_path.chmod(0o700)
        with patch.dict(
            os.environ,
            {"TWOXBRAINZ_TEST_PCM": str(pcm_path)},
        ):
            yield command_path


async def _collect(frames: AsyncIterable[bytes]) -> tuple[bytes, ...]:
    return tuple([frame async for frame in frames])
