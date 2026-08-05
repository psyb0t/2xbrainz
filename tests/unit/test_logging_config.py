from __future__ import annotations

import json
import logging
import stat
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch

from two_x_brainz.logging_config import allocate_session_log_file, configure_logging


class LoggingConfigurationTests(unittest.TestCase):
    def test_session_log_allocator_uses_utc_prefix_and_avoids_collisions(self) -> None:
        timestamp = datetime(2026, 8, 4, 21, 14, 8, 123456, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            base_log_file = Path(directory) / "2xbrainz.log"

            first_log_file = allocate_session_log_file(
                base_log_file,
                timestamp=timestamp,
            )
            second_log_file = allocate_session_log_file(
                base_log_file,
                timestamp=timestamp,
            )

            self.assertEqual(
                first_log_file.name,
                "20260804T211408123456Z_2xbrainz.log",
            )
            self.assertEqual(
                second_log_file.name,
                "20260804T211408123456Z_2xbrainz-1.log",
            )
            self.assertTrue(first_log_file.is_file())
            self.assertTrue(second_log_file.is_file())
            self.assertEqual(
                stat.S_IMODE(first_log_file.stat().st_mode),
                0o600,
            )
            self.assertEqual(
                stat.S_IMODE(Path(directory).stat().st_mode),
                0o700,
            )

    def test_rotating_log_persists_redacted_structured_runtime_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_file = Path(directory) / "2xbrainz.log"
            with (
                patch("two_x_brainz.logging_config.DEFAULT_LOG_ROTATION_BYTES", 1),
                patch("two_x_brainz.logging_config.DEFAULT_LOG_BACKUP_COUNT", 1),
            ):
                configure_logging("INFO", log_file)
                logging.getLogger("two_x_brainz.runtime").info(
                    "live runtime event",
                    extra={
                        "event": {
                            "kind": "timeline",
                            "text": "A retained transcript event.",
                            "token": "secret-value",
                        }
                    },
                )
                logging.getLogger("two_x_brainz.runtime").info(
                    "live runtime event",
                    extra={"event": {"kind": "draft", "text": "A suggestion."}},
                )
                logging.shutdown()
                logging.getLogger().handlers.clear()

            active_records = _records(log_file)
            backup_records = _records(log_file.with_suffix(".log.1"))
            all_records = [*active_records, *backup_records]
            self.assertTrue(all_records)
            self.assertTrue(log_file.with_suffix(".log.1").is_file())
            self.assertEqual(stat.S_IMODE(log_file.stat().st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE(log_file.with_suffix(".log.1").stat().st_mode),
                0o600,
            )
            self.assertTrue(
                any(record.get("msg") == "live runtime event" for record in all_records)
            )
            self.assertTrue(
                any(_event_has_redacted_token(record) for record in all_records)
            )


def _records(log_file: Path) -> list[dict[str, object]]:
    if not log_file.is_file():
        return []
    return [json.loads(line) for line in log_file.read_text().splitlines()]


def _event_has_redacted_token(record: dict[str, object]) -> bool:
    raw_event = record.get("event")
    if not isinstance(raw_event, dict):
        return False
    event = cast(dict[str, object], raw_event)
    return event.get("token") == "[REDACTED]"
