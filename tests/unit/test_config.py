from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from two_x_brainz.config import Settings
from two_x_brainz.constants import (
    ENV_AIGATE_COACH_MODEL,
    ENV_AIGATE_COACH_REASONING_EFFORT,
    ENV_AIGATE_REASONING_EFFORT,
    ENV_AIGATE_REPLY_MODEL,
    ENV_AIGATE_REPLY_REASONING_EFFORT,
    ENV_AIGATE_SUMMARY_MODEL,
    ENV_AIGATE_SUMMARY_REASONING_EFFORT,
    ENV_AIGATE_TOKEN,
    ENV_AIGATE_URL,
    ENV_AUDIO_CONFIG_FILE,
    ENV_LOG_DIRECTORY,
    ENV_SESSION_BRIEF,
    ENV_WEB_RESEARCH_ENABLED,
)
from two_x_brainz.errors import ConfigurationError


class SettingsTests(unittest.TestCase):
    def test_defaults_are_valid(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_environment()

        self.assertEqual(settings.talkies_model, "nemotron-3.5-asr-0.6b")
        self.assertIsNone(settings.aigate_token)
        self.assertEqual(settings.aigate_reasoning_effort, "none")
        self.assertEqual(
            settings.talkies_ws_url,
            "ws://localhost:4000/talkies/v1/audio/transcriptions/stream",
        )

    def test_derives_talkies_route_and_shares_the_aigate_token(self) -> None:
        environment = {
            ENV_AIGATE_URL: "https://gateway.example.test/prefix/v1",
            ENV_AIGATE_TOKEN: "gateway-token",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_environment()

        self.assertEqual(
            settings.talkies_ws_url,
            "wss://gateway.example.test/prefix/talkies/v1/audio/transcriptions/stream",
        )
        self.assertEqual(settings.talkies_token, "gateway-token")

    def test_rejects_url_credentials_and_queries(self) -> None:
        environment = {
            ENV_AIGATE_URL: "http://aigate:4000?token=unsafe",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaises(ConfigurationError),
        ):
            Settings.from_environment()

    def test_requires_an_aigate_v1_api_root(self) -> None:
        with (
            patch.dict(
                os.environ,
                {ENV_AIGATE_URL: "https://gateway.example.test/api"},
                clear=True,
            ),
            self.assertRaisesRegex(ConfigurationError, "end in /v1"),
        ):
            Settings.from_environment()

    def test_rejects_relative_audio_selection_config_path(self) -> None:
        environment = {ENV_AUDIO_CONFIG_FILE: "audio-selection.json"}
        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaises(ConfigurationError),
        ):
            Settings.from_environment()

    def test_log_directory_builds_the_standard_rotating_log_path(self) -> None:
        environment = {ENV_LOG_DIRECTORY: "/mounted/session-logs"}
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_environment()

        self.assertEqual(settings.log_file, Path("/mounted/session-logs/2xbrainz.log"))

    def test_rejects_relative_log_directory(self) -> None:
        environment = {ENV_LOG_DIRECTORY: "session-logs"}
        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaises(ConfigurationError),
        ):
            Settings.from_environment()

    def test_rejects_invalid_web_research_flag(self) -> None:
        with (
            patch.dict(
                os.environ,
                {ENV_WEB_RESEARCH_ENABLED: "perhaps"},
                clear=True,
            ),
            self.assertRaisesRegex(ConfigurationError, "true or false"),
        ):
            Settings.from_environment()

    def test_rejects_invalid_reasoning_effort(self) -> None:
        with (
            patch.dict(
                os.environ,
                {ENV_AIGATE_REASONING_EFFORT: "maximum-ish"},
                clear=True,
            ),
            self.assertRaisesRegex(ConfigurationError, "AIGATE_REASONING_EFFORT"),
        ):
            Settings.from_environment()

    def test_loads_independent_first_run_flow_models(self) -> None:
        environment = {
            ENV_AIGATE_REPLY_MODEL: "cerebras-model",
            ENV_AIGATE_COACH_MODEL: "coach-model",
            ENV_AIGATE_SUMMARY_MODEL: "summary-model",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_environment()

        self.assertEqual(settings.aigate_reply_model, "cerebras-model")
        self.assertEqual(settings.aigate_coach_model, "coach-model")
        self.assertEqual(settings.aigate_summary_model, "summary-model")

    def test_loads_independent_first_run_reasoning_efforts(self) -> None:
        environment = {
            ENV_AIGATE_REPLY_REASONING_EFFORT: "minimal",
            ENV_AIGATE_COACH_REASONING_EFFORT: "low",
            ENV_AIGATE_SUMMARY_REASONING_EFFORT: "high",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_environment()

        self.assertEqual(settings.aigate_reply_reasoning_effort, "minimal")
        self.assertEqual(settings.aigate_coach_reasoning_effort, "low")
        self.assertEqual(settings.aigate_summary_reasoning_effort, "high")

    def test_rejects_invalid_flow_reasoning_effort(self) -> None:
        with (
            patch.dict(
                os.environ,
                {ENV_AIGATE_REPLY_REASONING_EFFORT: "maximum-ish"},
                clear=True,
            ),
            self.assertRaisesRegex(
                ConfigurationError,
                "AIGATE_REPLY_REASONING_EFFORT",
            ),
        ):
            Settings.from_environment()

    def test_enables_web_research_only_when_explicitly_requested(self) -> None:
        with patch.dict(
            os.environ,
            {ENV_WEB_RESEARCH_ENABLED: "true"},
            clear=True,
        ):
            settings = Settings.from_environment()

        self.assertTrue(settings.web_research_enabled)

    def test_loads_a_bounded_optional_session_brief(self) -> None:
        with patch.dict(
            os.environ,
            {ENV_SESSION_BRIEF: "Technical interview for a product role."},
            clear=True,
        ):
            settings = Settings.from_environment()

        self.assertEqual(
            settings.session_brief,
            "Technical interview for a product role.",
        )

    def test_rejects_an_oversized_session_brief(self) -> None:
        with (
            patch.dict(
                os.environ,
                {ENV_SESSION_BRIEF: "x" * 4_001},
                clear=True,
            ),
            self.assertRaisesRegex(ConfigurationError, "SESSION_BRIEF"),
        ):
            Settings.from_environment()

    def test_repr_omits_the_shared_aigate_token(self) -> None:
        # The log redactor matches on field NAMES and recurses only into dicts,
        # lists and tuples, so a whole Settings reaches json.dumps(default=str)
        # and is rendered by this repr. Without repr=False on the token fields
        # that path writes both credentials to every log sink.
        environment = {
            ENV_AIGATE_TOKEN: "gateway-token",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_environment()

        rendered = repr(settings)

        self.assertNotIn("gateway-token", rendered)
        self.assertIn("aigate_url=", rendered)
        self.assertNotIn("talkies_token", rendered)
        self.assertNotIn("session_brief", rendered)
