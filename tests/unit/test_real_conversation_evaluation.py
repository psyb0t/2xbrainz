from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from two_x_brainz.evaluation import TimedRecord, load_scenario
from two_x_brainz.fixture_trace import FixtureTrace

_SCRIPT = Path("tests/integration/real_conversation_evaluation.py")
_SCENARIO = Path("tests/fixtures/slang-interrupted-project-chat.json")


def _load_module() -> Any:
    integration_directory = str(_SCRIPT.parent.resolve())
    sys.path.insert(0, integration_directory)
    try:
        specification = importlib.util.spec_from_file_location(
            "real_conversation_evaluation",
            _SCRIPT,
        )
        assert specification is not None
        assert specification.loader is not None
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(integration_directory)


_EVALUATION = _load_module()


class PairBarrierTests(unittest.TestCase):
    def test_releases_only_after_both_streams_arrive(self) -> None:
        async def run() -> None:
            barrier = _EVALUATION._PairBarrier()
            first = asyncio.create_task(barrier.wait())
            await asyncio.sleep(0)
            self.assertFalse(first.done())

            second = asyncio.create_task(barrier.wait())
            await asyncio.gather(first, second)

        asyncio.run(run())


class EvaluationRecorderTests(unittest.TestCase):
    def test_records_provider_activity_without_duplicate_trace_kind(self) -> None:
        with TemporaryDirectory() as directory:
            trace = FixtureTrace(Path(directory), "recorder")
            recorder = _EVALUATION._ObservationRecorder(trace)

            recorder.activity_sink()(
                {
                    "phase": "request_started",
                    "flow_id": "flow-1",
                    "generation_id": "generation-1",
                    "context_revision": 1,
                    "output_kind": "draft",
                    "model": "test-model",
                }
            )
            trace.close()

            lines = trace.path.read_text(encoding="utf-8").splitlines()
            event = json.loads(lines[-1])
            self.assertEqual(event["kind"], "provider_activity")
            self.assertEqual(event["flow_id"], "flow-1")
            self.assertEqual(recorder.phase_count("request_started"), 1)

    def test_records_non_provider_observation_for_trace_reconstruction(self) -> None:
        with TemporaryDirectory() as directory:
            trace = FixtureTrace(Path(directory), "recorder")
            recorder = _EVALUATION._ObservationRecorder(trace)

            recorder.record(
                {
                    "kind": "evaluation_interruption",
                    "turn_id": "turn-2",
                    "context_revision": 2,
                }
            )
            trace.close()

            lines = trace.path.read_text(encoding="utf-8").splitlines()
            event = json.loads(lines[-1])
            self.assertEqual(event["kind"], "evaluation_observation")
            self.assertEqual(event["record"]["turn_id"], "turn-2")

    def test_research_gate_requires_completed_findings(self) -> None:
        records = [
            TimedRecord(
                sequence=1,
                elapsed_ms=1,
                record={
                    "kind": "provider_activity",
                    "phase": "request_completed",
                    "output_kind": "research",
                    "output": "Verified findings from the primary specification.",
                },
            )
        ]

        _EVALUATION._assert_research_completed(records)

        records[0].record["output"] = "NO_NEW_RESEARCH"
        with self.assertRaisesRegex(
            _EVALUATION.ConversationEvaluationError,
            "did not complete agentic research",
        ):
            _EVALUATION._assert_research_completed(records)


class EvaluationArtifactTests(unittest.TestCase):
    def test_fixture_model_overrides_reach_all_runtime_flows(self) -> None:
        settings = _EVALUATION.Settings.from_environment()
        environment = {
            _EVALUATION._TALKIES_MODEL_ENV: "custom-asr",
            _EVALUATION._DRAFT_MODEL_ENV: "custom-reply",
            _EVALUATION._COMMENTARY_MODEL_ENV: "custom-coach",
            _EVALUATION._SUMMARY_MODEL_ENV: "custom-story",
            _EVALUATION._RESEARCH_MODEL_ENV: "claudebox-sonnet",
        }

        with patch.dict(os.environ, environment, clear=False):
            updated = _EVALUATION._settings_with_fixture_models(settings)

        self.assertEqual(updated.talkies_model, "custom-asr")
        self.assertEqual(updated.aigate_reply_model, "custom-reply")
        self.assertEqual(updated.aigate_coach_model, "custom-coach")
        self.assertEqual(updated.aigate_summary_model, "custom-story")
        self.assertEqual(updated.aigate_research_model, "claudebox-sonnet")

    def test_fixture_model_override_rejects_empty_or_oversized_values(self) -> None:
        invalid_values = (
            " ",
            "m" * (_EVALUATION.MAX_AIGATE_MODEL_ID_CHARACTERS + 1),
        )
        for value in invalid_values:
            with (
                self.subTest(value_length=len(value)),
                patch.dict(
                    os.environ,
                    {_EVALUATION._SUMMARY_MODEL_ENV: value},
                    clear=False,
                ),
                self.assertRaisesRegex(
                    _EVALUATION.ConversationEvaluationError,
                    "TWOXBRAINZ_FIXTURE_SUMMARY_MODEL is invalid",
                ),
            ):
                _EVALUATION._fixture_model(
                    _EVALUATION._SUMMARY_MODEL_ENV,
                    "default-story",
                )

    def test_repeat_count_defaults_to_three_and_validates_bounds(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_EVALUATION._repeat_count(), 3)

        for value in ("1", "5"):
            with (
                self.subTest(value=value),
                patch.dict(
                    os.environ,
                    {_EVALUATION._REPEATS_ENV: value},
                    clear=True,
                ),
            ):
                self.assertEqual(_EVALUATION._repeat_count(), int(value))

        for value in ("0", "6", "bad"):
            with (
                self.subTest(value=value),
                patch.dict(
                    os.environ,
                    {_EVALUATION._REPEATS_ENV: value},
                    clear=True,
                ),
                self.assertRaises(_EVALUATION.ConversationEvaluationError),
            ):
                _EVALUATION._repeat_count()

    def test_aggregates_attempt_results_and_writes_exclusively(self) -> None:
        attempts = [
            {
                "maximum_concurrent_provider_flows": 3,
                "mean_word_error_rate": 0.1,
            },
            {
                "maximum_concurrent_provider_flows": 4,
                "mean_word_error_rate": 0.2,
            },
        ]
        aggregate = _EVALUATION._aggregate_attempt_results(attempts)

        self.assertEqual(
            aggregate["minimum_maximum_concurrent_provider_flows"],
            3,
        )
        self.assertAlmostEqual(aggregate["mean_word_error_rate"], 0.15)
        with TemporaryDirectory() as directory:
            path = Path(directory)
            artifact = _EVALUATION._write_aggregate_artifact(
                path,
                "synthetic-scenario",
                attempts,
                aggregate,
            )
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            self.assertEqual(payload["attempt_count"], 2)
            with self.assertRaises(_EVALUATION.ConversationEvaluationError):
                _EVALUATION._write_aggregate_artifact(
                    path,
                    "synthetic-scenario",
                    attempts,
                    aggregate,
                )

    def test_writes_every_reference_and_recognized_transcript_exclusively(
        self,
    ) -> None:
        scenario = load_scenario(_SCENARIO)
        recognized = {turn.identifier: turn.text for turn in scenario.turns}
        rates = {turn.identifier: 0.0 for turn in scenario.turns}

        with TemporaryDirectory() as directory:
            path = Path(directory)
            _EVALUATION._write_transcript_artifact(
                path,
                scenario,
                recognized,
                rates,
            )
            payload = json.loads(
                (path / "transcripts.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(payload["turns"]), len(scenario.turns))
            self.assertEqual(payload["turns"][0]["word_error_rate"], 0.0)

            with self.assertRaisesRegex(
                _EVALUATION.ConversationEvaluationError,
                "write transcript artifact",
            ):
                _EVALUATION._write_transcript_artifact(
                    path,
                    scenario,
                    recognized,
                    rates,
                )

    def test_voice_selection_is_per_role_and_rejects_blank_override(self) -> None:
        scenario = load_scenario(_SCENARIO)
        with patch.dict(
            os.environ,
            {
                _EVALUATION._USER_VOICE_ENV: "user-voice",
                _EVALUATION._REMOTE_VOICE_ENV: "remote-voice",
            },
        ):
            self.assertEqual(
                _EVALUATION._voice_for_turn(scenario.turns[0]),
                "user-voice",
            )
            self.assertEqual(
                _EVALUATION._voice_for_turn(scenario.turns[1]),
                "remote-voice",
            )

        with (
            patch.dict(
                os.environ,
                {_EVALUATION._USER_VOICE_ENV: " "},
            ),
            self.assertRaisesRegex(
                _EVALUATION.ConversationEvaluationError,
                "is invalid",
            ),
        ):
            _EVALUATION._voice_for_turn(scenario.turns[0])


if __name__ == "__main__":
    unittest.main()
