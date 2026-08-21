from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from two_x_brainz.config import Settings
from two_x_brainz.constants import (
    DEFAULT_AIGATE_COACH_MODEL,
    DEFAULT_AIGATE_FAST_REPLY_MODEL,
    DEFAULT_AIGATE_REPLY_MODEL,
    DEFAULT_AIGATE_RESEARCH_MODEL,
    DEFAULT_AIGATE_SUMMARY_MODEL,
    DEFAULT_TALKIES_MODEL,
    ENV_AIGATE_TOKEN,
    ENV_AIGATE_URL,
    ENV_LOG_DIRECTORY,
)
from two_x_brainz.errors import ConfigurationError


class SettingsTests(unittest.TestCase):
    def test_safe_runtime_defaults_are_owned_by_code(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_environment()

        self.assertEqual(settings.talkies_model, DEFAULT_TALKIES_MODEL)
        self.assertEqual(settings.aigate_reply_model, DEFAULT_AIGATE_REPLY_MODEL)
        self.assertEqual(
            settings.aigate_fast_reply_model, DEFAULT_AIGATE_FAST_REPLY_MODEL
        )
        self.assertEqual(settings.aigate_coach_model, DEFAULT_AIGATE_COACH_MODEL)
        self.assertEqual(settings.aigate_summary_model, DEFAULT_AIGATE_SUMMARY_MODEL)
        self.assertEqual(settings.aigate_research_model, DEFAULT_AIGATE_RESEARCH_MODEL)
        self.assertEqual(settings.aigate_reply_reasoning_effort, "medium")
        self.assertEqual(settings.aigate_fast_reply_reasoning_effort, "none")
        self.assertEqual(settings.aigate_coach_reasoning_effort, "none")
        self.assertEqual(settings.aigate_summary_reasoning_effort, "none")
        self.assertEqual(settings.aigate_research_reasoning_effort, "high")
        self.assertTrue(settings.web_research_enabled)
        self.assertIsNone(settings.session_brief)

    def test_removed_safe_environment_values_cannot_override_code_defaults(
        self,
    ) -> None:
        removed_environment = {
            "TWOXBRAINZ_TALKIES_MODEL": "unsafe-env-asr",
            "TWOXBRAINZ_AIGATE_REPLY_MODEL": "unsafe-env-reply",
            "TWOXBRAINZ_AIGATE_COACH_REASONING_EFFORT": "high",
            "TWOXBRAINZ_SESSION_BRIEF": "unsafe env context",
            "TWOXBRAINZ_WEB_RESEARCH_ENABLED": "false",
            "TWOXBRAINZ_AUDIO_CONFIG_FILE": "/tmp/legacy.json",
        }
        with patch.dict(os.environ, removed_environment, clear=True):
            settings = Settings.from_environment()

        self.assertEqual(settings.talkies_model, DEFAULT_TALKIES_MODEL)
        self.assertEqual(settings.aigate_reply_model, DEFAULT_AIGATE_REPLY_MODEL)
        self.assertEqual(settings.aigate_coach_reasoning_effort, "none")
        self.assertIsNone(settings.session_brief)
        self.assertTrue(settings.web_research_enabled)

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

    def test_rejects_url_credentials_queries_and_non_v1_roots(self) -> None:
        for invalid_url in (
            "http://aigate:4000?token=unsafe",
            "https://gateway.example.test/api",
        ):
            with (
                self.subTest(invalid_url=invalid_url),
                patch.dict(os.environ, {ENV_AIGATE_URL: invalid_url}, clear=True),
                self.assertRaises(ConfigurationError),
            ):
                Settings.from_environment()

    def test_log_directory_builds_the_standard_rotating_log_path(self) -> None:
        with patch.dict(
            os.environ,
            {ENV_LOG_DIRECTORY: "/mounted/session-logs"},
            clear=True,
        ):
            settings = Settings.from_environment()

        self.assertEqual(settings.log_file, Path("/mounted/session-logs/2xbrainz.log"))

    def test_rejects_relative_log_directory(self) -> None:
        with (
            patch.dict(
                os.environ,
                {ENV_LOG_DIRECTORY: "session-logs"},
                clear=True,
            ),
            self.assertRaises(ConfigurationError),
        ):
            Settings.from_environment()

    def test_repr_omits_the_shared_aigate_token(self) -> None:
        with patch.dict(
            os.environ,
            {ENV_AIGATE_TOKEN: "gateway-token"},
            clear=True,
        ):
            rendered = repr(Settings.from_environment())

        self.assertNotIn("gateway-token", rendered)
        self.assertIn("aigate_url=", rendered)
        self.assertNotIn("talkies_token", rendered)
        self.assertNotIn("session_brief", rendered)
