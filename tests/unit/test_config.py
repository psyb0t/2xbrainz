from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from two_x_brainz.config import AIGateMode, Settings
from two_x_brainz.constants import (
    ENV_AIGATE_MODE,
    ENV_AIGATE_TOKEN,
    ENV_AIGATE_URL,
    ENV_REMOTE_TEXT_ENABLED,
    ENV_TALKIES_TOKEN,
    ENV_TALKIES_WS_URL,
)
from two_x_brainz.errors import ConfigurationError


class SettingsTests(unittest.TestCase):
    def test_defaults_are_valid(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_environment()

        self.assertEqual(settings.talkies_model, "nemotron-3.5-asr-0.6b")
        self.assertIsNone(settings.aigate_token)
        self.assertEqual(settings.aigate_mode, AIGateMode.LOCAL)

    def test_remote_mode_requires_explicit_opt_in(self) -> None:
        environment = {ENV_AIGATE_MODE: "remote"}
        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaises(ConfigurationError),
        ):
            Settings.from_environment()

    def test_remote_mode_accepts_explicit_opt_in(self) -> None:
        environment = {
            ENV_AIGATE_MODE: "remote",
            ENV_REMOTE_TEXT_ENABLED: "true",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_environment()

        self.assertEqual(settings.aigate_mode, AIGateMode.REMOTE)

    def test_rejects_unknown_aigate_mode(self) -> None:
        environment = {ENV_AIGATE_MODE: "unexpected"}
        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaises(ConfigurationError),
        ):
            Settings.from_environment()

    def test_rejects_url_credentials_and_queries(self) -> None:
        environment = {
            ENV_TALKIES_WS_URL: "ws://user:password@talkies:8000/stream",
            ENV_AIGATE_URL: "http://aigate:4000?token=unsafe",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaises(ConfigurationError),
        ):
            Settings.from_environment()

    def test_reuses_aigate_token_only_for_same_talkies_authority(self) -> None:
        environment = {
            ENV_AIGATE_URL: "https://gateway.example.test",
            ENV_AIGATE_TOKEN: "gateway-token",
            ENV_TALKIES_WS_URL: (
                "wss://gateway.example.test/v1/audio/transcriptions/stream"
            ),
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_environment()

        self.assertEqual(settings.talkies_token, "gateway-token")

    def test_does_not_forward_aigate_token_to_different_talkies_authority(self) -> None:
        environment = {
            ENV_AIGATE_URL: "https://gateway.example.test",
            ENV_AIGATE_TOKEN: "gateway-token",
            ENV_TALKIES_WS_URL: "wss://talkies.example.test/v1/audio/transcriptions/stream",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_environment()

        self.assertIsNone(settings.talkies_token)

    def test_dedicated_talkies_token_takes_precedence(self) -> None:
        environment = {
            ENV_AIGATE_TOKEN: "gateway-token",
            ENV_TALKIES_TOKEN: "talkies-token",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_environment()

        self.assertEqual(settings.talkies_token, "talkies-token")

    def test_repr_omits_both_tokens(self) -> None:
        # The log redactor matches on field NAMES and recurses only into dicts,
        # lists and tuples, so a whole Settings reaches json.dumps(default=str)
        # and is rendered by this repr. Without repr=False on the token fields
        # that path writes both credentials to every log sink.
        environment = {
            ENV_AIGATE_TOKEN: "gateway-token",
            ENV_TALKIES_TOKEN: "talkies-token",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_environment()

        rendered = repr(settings)

        self.assertNotIn("gateway-token", rendered)
        self.assertNotIn("talkies-token", rendered)
        self.assertIn("aigate_url=", rendered)

    def test_repr_omits_talkies_token_derived_from_aigate_token(self) -> None:
        # Token reuse copies the AIGate value into talkies_token, so hiding only
        # the field it was read from would still disclose it.
        environment = {
            ENV_AIGATE_URL: "https://gateway.example.test",
            ENV_AIGATE_TOKEN: "gateway-token",
            ENV_TALKIES_WS_URL: (
                "wss://gateway.example.test/v1/audio/transcriptions/stream"
            ),
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_environment()

        self.assertEqual(settings.talkies_token, "gateway-token")
        self.assertNotIn("gateway-token", repr(settings))
