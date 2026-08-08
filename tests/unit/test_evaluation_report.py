from __future__ import annotations

import json
from pathlib import Path

import pytest

from two_x_brainz.errors import ProtocolError
from two_x_brainz.evaluation_report import regenerate_suite_report


def test_regenerates_suite_report_without_changing_json_sources(tmp_path: Path) -> None:
    suite = _suite(tmp_path, attempt_count=2)
    source_paths = (suite / "aggregate.json", *(suite.glob("attempt-*/scorecard.json")))
    source_bytes = {path: path.read_bytes() for path in source_paths}

    report_path = regenerate_suite_report(tmp_path, suite.name)

    report = report_path.read_text(encoding="utf-8")
    assert "Result: **PASS**" in report
    assert "Attempts: 2" in report
    assert "| 1 | 4 | 16 | 0.125 | 3 ms | 2 ms | 0 | yes |" in report
    assert "| 2 | 4 | 16 | 0.125 | 3 ms | 2 ms | 0 | yes |" in report
    assert {path: path.read_bytes() for path in source_paths} == source_bytes


@pytest.mark.parametrize("run_name", ["", ".", "..", "../escape", "nested/run"])
def test_rejects_invalid_or_traversing_run_names(
    tmp_path: Path,
    run_name: str,
) -> None:
    with pytest.raises(ProtocolError, match="run name"):
        regenerate_suite_report(tmp_path, run_name)


def test_rejects_symlinked_suite_and_inconsistent_attempt_count(
    tmp_path: Path,
) -> None:
    target = _suite(tmp_path, attempt_count=1)
    symlink = tmp_path / "linked-suite"
    symlink.symlink_to(target, target_is_directory=True)
    with pytest.raises(ProtocolError, match="suite directory"):
        regenerate_suite_report(tmp_path, symlink.name)

    aggregate_path = target / "aggregate.json"
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    aggregate["attempt_count"] = 2
    aggregate_path.write_text(json.dumps(aggregate), encoding="utf-8")
    with pytest.raises(ProtocolError, match="attempt directories"):
        regenerate_suite_report(tmp_path, target.name)


def test_rejects_malformed_or_failed_scorecard(tmp_path: Path) -> None:
    suite = _suite(tmp_path, attempt_count=1)
    scorecard_path = suite / "attempt-01" / "scorecard.json"
    scorecard_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(ProtocolError, match="valid JSON"):
        regenerate_suite_report(tmp_path, suite.name)

    scorecard_path.write_text(
        json.dumps(_scorecard(passed=False)),
        encoding="utf-8",
    )
    with pytest.raises(ProtocolError, match="did not pass"):
        regenerate_suite_report(tmp_path, suite.name)


def test_rejects_stale_output_or_missing_replacement_metrics(tmp_path: Path) -> None:
    suite = _suite(tmp_path, attempt_count=1)
    scorecard_path = suite / "attempt-01" / "scorecard.json"
    scorecard = _scorecard()
    scorecard["stale_provider_output_count"] = 1
    scorecard_path.write_text(json.dumps(scorecard), encoding="utf-8")
    with pytest.raises(ProtocolError, match="stale provider output"):
        regenerate_suite_report(tmp_path, suite.name)

    scorecard = _scorecard()
    scorecard["replacement_context_revisions"] = []
    scorecard_path.write_text(json.dumps(scorecard), encoding="utf-8")
    with pytest.raises(ProtocolError, match="replacement revisions"):
        regenerate_suite_report(tmp_path, suite.name)


def _suite(root: Path, *, attempt_count: int) -> Path:
    suite = root / "scenario-suite-123"
    suite.mkdir()
    attempts: list[dict[str, object]] = []
    for attempt in range(1, attempt_count + 1):
        attempt_directory = suite / f"attempt-{attempt:02d}"
        attempt_directory.mkdir()
        (attempt_directory / "scorecard.json").write_text(
            json.dumps(_scorecard()),
            encoding="utf-8",
        )
        attempts.append({"attempt": attempt})
    (suite / "aggregate.json").write_text(
        json.dumps(
            {
                "scenario": "scenario",
                "attempt_count": attempt_count,
                "attempts": attempts,
                "aggregate": {},
            }
        ),
        encoding="utf-8",
    )
    return suite


def _scorecard(*, passed: bool = True) -> dict[str, object]:
    return {
        "scenario": "scenario",
        "passed": passed,
        "maximum_concurrent_provider_flows": 4,
        "overlapping_provider_pairs": 16,
        "mean_word_error_rate": 0.125,
        "research_tool_completed": True,
        "stale_provider_output_count": 0,
        "interruption_to_cancellation_latencies": _duration(3),
        "cancellation_to_replacement_latencies": _duration(2),
        "replacement_context_revisions": [3, 7],
    }


def _duration(milliseconds: int) -> dict[str, int | float]:
    return {
        "count": 2,
        "minimum_ms": milliseconds,
        "median_ms": float(milliseconds),
        "p95_ms": milliseconds,
        "maximum_ms": milliseconds,
    }
