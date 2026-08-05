from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from two_x_brainz.cli import main
from two_x_brainz.config import Settings


class CLIAudioSelectionTests(unittest.TestCase):
    def test_live_defaults_to_saved_or_interactive_selection(self) -> None:
        prepare, run_live = self._run_live_command(["2xbrainz", "live"])

        self.assertIsNone(prepare.call_args.kwargs["mic_node"])
        self.assertIsNone(prepare.call_args.kwargs["system_node"])
        self.assertNotEqual(run_live.call_args.args[0].log_file, _settings().log_file)
        self.assertEqual(run_live.call_args.kwargs["output"], "tui")

    def test_live_accepts_web_output_and_paired_overrides(self) -> None:
        prepare, run_live = self._run_live_command(
            [
                "2xbrainz",
                "live",
                "--output",
                "web",
                "--web-port",
                "9000",
                "--mic-node",
                "mic-usb",
                "--system-node",
                "speaker-usb",
            ]
        )

        self.assertEqual(prepare.call_args.kwargs["mic_node"], "mic-usb")
        self.assertEqual(prepare.call_args.kwargs["system_node"], "speaker-usb")
        self.assertEqual(run_live.call_args.kwargs["output"], "web")
        self.assertEqual(run_live.call_args.kwargs["web_port"], 9000)

    def test_live_rejects_removed_select_audio_argument(self) -> None:
        self._assert_parse_rejected(["2xbrainz", "live", "--select-audio"])

    def test_live_rejects_a_non_loopback_web_port_range(self) -> None:
        self._assert_parse_rejected(
            ["2xbrainz", "live", "--output", "web", "--web-port", "80"]
        )

    def _assert_parse_rejected(self, argv: list[str]) -> None:
        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
            main()

    def _run_live_command(self, argv: list[str]) -> tuple[MagicMock, AsyncMock]:
        settings = _settings()
        audio_setup = MagicMock()
        with (
            patch.object(sys, "argv", argv),
            patch(
                "two_x_brainz.cli.Settings.from_environment",
                return_value=settings,
            ),
            patch("two_x_brainz.cli.configure_logging"),
            patch(
                "two_x_brainz.cli.allocate_session_log_file",
                return_value=Path("/tmp/session.log"),
            ),
            patch(
                "two_x_brainz.cli.list_pipewire_nodes",
                new_callable=AsyncMock,
                return_value=[],
            ) as list_nodes,
            patch(
                "two_x_brainz.cli.prepare_audio_selection_setup",
                return_value=audio_setup,
            ) as prepare,
            patch(
                "two_x_brainz.cli.run_live",
                new_callable=AsyncMock,
            ) as run_live,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        list_nodes.assert_awaited_once_with()
        run_live.assert_awaited_once()
        self.assertEqual(run_live.call_args.args[0].log_file, Path("/tmp/session.log"))
        self.assertEqual(run_live.call_args.args[1:], (audio_setup,))
        return prepare, run_live


def _settings() -> Settings:
    return Settings(
        talkies_ws_url="ws://talkies:8000/v1/audio/transcriptions/stream",
        talkies_model="fixture-model",
        talkies_token=None,
        aigate_url="http://aigate:4000/v1",
        aigate_model=None,
        aigate_token=None,
        log_level="INFO",
        log_file=Path("/tmp/2xbrainz-test.log"),
        audio_config_file=Path("/tmp/2xbrainz-audio.json"),
    )
