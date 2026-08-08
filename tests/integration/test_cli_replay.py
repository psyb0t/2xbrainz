from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path


class CLIReplayIntegrationTests(unittest.TestCase):
    def test_replay_emits_the_documented_json_line_contract(self) -> None:
        repository_root = Path(__file__).parents[2]
        fixture = repository_root / "examples" / "conversation.jsonl"
        result = subprocess.run(
            ["2xbrainz", "replay", "--events", str(fixture)],
            check=False,
            capture_output=True,
            cwd=repository_root,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        records = [json.loads(line) for line in result.stdout.splitlines()]

        self.assertEqual(
            [record["kind"] for record in records],
            [
                "transcript",
                "turn",
                "transcript",
                "turn",
                "transcript",
                "turn",
                "timeline",
                "draft",
                "commentary",
                "summary",
            ],
        )
        self.assertEqual(
            set(records[0]),
            {
                "schema_version",
                "kind",
                "speaker_role",
                "type",
                "revision",
                "asr_model",
                "started_at_ms",
                "ended_at_ms",
                "text",
                "is_final",
                "confidence",
                "language",
                "words",
                "audio_seconds",
            },
        )
        self.assertEqual(records[0]["started_at_ms"], 100)
        self.assertEqual(records[0]["ended_at_ms"], 1_200)
        self.assertEqual(records[0]["confidence"], 0.9)
        self.assertEqual(records[0]["language"], "en")
        self.assertEqual(
            records[0]["words"],
            [
                {
                    "word": "Could",
                    "start_ms": 100,
                    "end_ms": 1_200,
                    "confidence": 0.95,
                }
            ],
        )
        self.assertEqual(
            set(records[1]),
            {
                "schema_version",
                "kind",
                "turn_id",
                "speaker_role",
                "state",
                "transcript_revision",
            },
        )
        self.assertEqual(records[1]["state"], "speaking")
        self.assertEqual(records[3]["state"], "candidate_end")
        self.assertEqual(records[5]["state"], "finalized")
        self.assertEqual(
            set(records[6]),
            {
                "schema_version",
                "kind",
                "turn_id",
                "speaker_role",
                "transcript_revision",
                "text",
            },
        )
        self.assertEqual(
            set(records[7]),
            {
                "schema_version",
                "kind",
                "generation_id",
                "trigger_turn_id",
                "status",
                "text",
                "context_revision",
            },
        )
        self.assertEqual(
            set(records[8]),
            {
                "schema_version",
                "kind",
                "generation_id",
                "trigger_turn_id",
                "status",
                "text",
                "context_revision",
            },
        )
        self.assertEqual(set(records[9]), set(records[8]))
        self.assertTrue(all(record["schema_version"] == 1 for record in records))
        self.assertEqual(records[5]["turn_id"], records[6]["turn_id"])
        self.assertEqual(records[5]["turn_id"], records[7]["trigger_turn_id"])
        self.assertEqual(records[5]["turn_id"], records[8]["trigger_turn_id"])
        self.assertEqual(records[5]["turn_id"], records[9]["trigger_turn_id"])

    def test_doctor_reports_aigate_configuration_without_tokens(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "TWOXBRAINZ_AIGATE_REPLY_MODEL": "reply-model",
                "TWOXBRAINZ_AIGATE_COACH_MODEL": "coach-model",
                "TWOXBRAINZ_AIGATE_SUMMARY_MODEL": "summary-model",
                "TWOXBRAINZ_SESSION_BRIEF": "Private local framing text.",
            }
        )
        environment.pop("TWOXBRAINZ_AIGATE_TOKEN", None)
        result = subprocess.run(
            ["2xbrainz", "doctor"],
            check=False,
            capture_output=True,
            env=environment,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["aigate_reply_model"], "claudebox-sonnet")
        self.assertEqual(status["aigate_coach_model"], "pibox-zai-glm-5-turbo")
        self.assertEqual(status["aigate_summary_model"], "groq-gpt-oss-120b")
        self.assertFalse(status["aigate_token_configured"])
        self.assertFalse(status["session_brief_configured"])
        self.assertNotIn("Private local framing text.", result.stdout)

    def test_overlap_replay_suppresses_remote_reply_draft(self) -> None:
        repository_root = Path(__file__).parents[2]
        fixture = repository_root / "examples" / "overlap.jsonl"
        result = subprocess.run(
            ["2xbrainz", "replay", "--events", str(fixture)],
            check=False,
            capture_output=True,
            cwd=repository_root,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        records = [json.loads(line) for line in result.stdout.splitlines()]

        self.assertNotIn("draft", [record["kind"] for record in records])
        self.assertEqual(
            [
                record["speaker_role"]
                for record in records
                if record["kind"] == "timeline"
            ],
            ["remote", "user"],
        )
