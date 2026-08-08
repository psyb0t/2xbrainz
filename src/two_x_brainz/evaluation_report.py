"""Regenerate a real-evaluation suite report from bounded local artifacts."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import cast

from two_x_brainz.errors import ProtocolError

_RUN_ENVIRONMENT_VARIABLE = "TWOXBRAINZ_EVALUATION_RUN"
_TRACE_ROOT = Path(".testing/fixture-traces")
_AGGREGATE_FILENAME = "aggregate.json"
_SCORECARD_FILENAME = "scorecard.json"
_REPORT_FILENAME = "report.md"
_TEMPORARY_REPORT_FILENAME = ".report.md.tmp"
_MAXIMUM_ARTIFACT_BYTES = 1_000_000
_MAXIMUM_ATTEMPTS = 5
_MAXIMUM_RUN_NAME_CHARACTERS = 200


def regenerate_suite_report(trace_root: Path, run_name: str) -> Path:
    """Validate one suite and atomically regenerate its derived Markdown report."""
    suite_directory = _suite_directory(trace_root, run_name)
    aggregate = _read_json_object(
        suite_directory / _AGGREGATE_FILENAME,
        "evaluation aggregate",
    )
    attempt_count = _positive_integer(
        aggregate.get("attempt_count"),
        "evaluation attempt count",
    )
    if attempt_count > _MAXIMUM_ATTEMPTS:
        raise ProtocolError("evaluation attempt count exceeds the configured limit")
    scenario = _required_text(aggregate.get("scenario"), "evaluation scenario")
    attempt_directories = tuple(sorted(suite_directory.glob("attempt-*")))
    if len(attempt_directories) != attempt_count:
        raise ProtocolError("evaluation attempt directories do not match aggregate")
    scorecards = tuple(
        _read_scorecard(directory, scenario) for directory in attempt_directories
    )
    report = _render_suite_report(scenario, scorecards)
    temporary_path = suite_directory / _TEMPORARY_REPORT_FILENAME
    report_path = suite_directory / _REPORT_FILENAME
    try:
        temporary_path.write_text(report, encoding="utf-8")
        temporary_path.replace(report_path)
    except OSError as error:
        raise ProtocolError("write evaluation suite report") from error
    return report_path


def main(arguments: list[str]) -> int:
    """Regenerate the selected suite report without contacting external services."""
    if len(arguments) != 1:
        print("usage: python -m two_x_brainz.evaluation_report", file=sys.stderr)
        return 2
    run_name = os.environ.get(_RUN_ENVIRONMENT_VARIABLE, "")
    try:
        report_path = regenerate_suite_report(_TRACE_ROOT, run_name)
    except ProtocolError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(report_path)
    return 0


def _suite_directory(trace_root: Path, run_name: str) -> Path:
    if (
        not run_name
        or len(run_name) > _MAXIMUM_RUN_NAME_CHARACTERS
        or Path(run_name).name != run_name
        or run_name in {".", ".."}
        or "\x00" in run_name
    ):
        raise ProtocolError("evaluation run name is invalid")
    if trace_root.is_symlink() or not trace_root.is_dir():
        raise ProtocolError("evaluation trace root is unavailable")
    suite_directory = trace_root / run_name
    if suite_directory.is_symlink() or not suite_directory.is_dir():
        raise ProtocolError("evaluation suite directory is unavailable")
    if suite_directory.resolve().parent != trace_root.resolve():
        raise ProtocolError("evaluation suite directory escapes the trace root")
    return suite_directory


def _read_scorecard(directory: Path, scenario: str) -> dict[str, object]:
    if directory.is_symlink() or not directory.is_dir():
        raise ProtocolError("evaluation attempt directory is invalid")
    scorecard = _read_json_object(
        directory / _SCORECARD_FILENAME,
        "evaluation scorecard",
    )
    if _required_text(scorecard.get("scenario"), "scorecard scenario") != scenario:
        raise ProtocolError("evaluation scorecard scenario does not match aggregate")
    if scorecard.get("passed") is not True:
        raise ProtocolError("evaluation scorecard did not pass")
    _nonnegative_integer(
        scorecard.get("maximum_concurrent_provider_flows"),
        "maximum concurrent provider flows",
    )
    _nonnegative_integer(
        scorecard.get("overlapping_provider_pairs"),
        "overlapping provider pairs",
    )
    if scorecard.get("stale_provider_output_count") != 0:
        raise ProtocolError("evaluation scorecard contains stale provider output")
    _duration_count(
        scorecard.get("interruption_to_cancellation_latencies"),
        "interruption-to-cancellation latency",
    )
    _duration_count(
        scorecard.get("cancellation_to_replacement_latencies"),
        "cancellation-to-replacement latency",
    )
    revisions = scorecard.get("replacement_context_revisions")
    if not isinstance(revisions, list) or not revisions:
        raise ProtocolError("evaluation scorecard lacks replacement revisions")
    for revision in cast(list[object], revisions):
        _nonnegative_integer(revision, "replacement context revision")
    _unit_interval_number(
        scorecard.get("mean_word_error_rate"),
        "mean word error rate",
    )
    if scorecard.get("research_tool_completed") is not True:
        raise ProtocolError("evaluation scorecard lacks completed research")
    return scorecard


def _read_json_object(path: Path, description: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ProtocolError(f"{description} is unavailable")
    try:
        size = path.stat().st_size
        if not 1 <= size <= _MAXIMUM_ARTIFACT_BYTES:
            raise ProtocolError(f"{description} size is invalid")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise ProtocolError(f"read {description}") from error
    except json.JSONDecodeError as error:
        raise ProtocolError(f"{description} must contain valid JSON") from error
    if not isinstance(value, dict):
        raise ProtocolError(f"{description} must be an object")
    mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise ProtocolError(f"{description} must be an object")
    return cast(dict[str, object], value)


def _render_suite_report(
    scenario: str,
    scorecards: tuple[dict[str, object], ...],
) -> str:
    lines = [
        f"# Conversation evaluation suite — {scenario}",
        "",
        "- Result: **PASS**",
        f"- Attempts: {len(scorecards)}",
        "",
        (
            "| Attempt | Concurrent flows | Overlapping pairs | Mean WER | "
            "Cancel p95 | Replace p95 | Stale output | Research |"
        ),
        "|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for index, scorecard in enumerate(scorecards, start=1):
        cancellation_p95 = _duration_p95(
            scorecard["interruption_to_cancellation_latencies"]
        )
        replacement_p95 = _duration_p95(
            scorecard["cancellation_to_replacement_latencies"]
        )
        lines.append(
            "| "
            f"{index} | "
            f"{scorecard['maximum_concurrent_provider_flows']} | "
            f"{scorecard['overlapping_provider_pairs']} | "
            f"{scorecard['mean_word_error_rate']} | "
            f"{cancellation_p95} ms | "
            f"{replacement_p95} ms | "
            f"{scorecard['stale_provider_output_count']} | yes |"
        )
    lines.append("")
    return "\n".join(lines)


def _required_text(value: object, description: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ProtocolError(f"{description} is invalid")
    return value.strip()


def _positive_integer(value: object, description: str) -> int:
    number = _nonnegative_integer(value, description)
    if number == 0:
        raise ProtocolError(f"{description} must be positive")
    return number


def _nonnegative_integer(value: object, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProtocolError(f"{description} is invalid")
    return value


def _unit_interval_number(value: object, description: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ProtocolError(f"{description} is invalid")
    number = float(value)
    if not 0 <= number <= 1:
        raise ProtocolError(f"{description} is invalid")
    return number


def _duration_count(value: object, description: str) -> int:
    if not isinstance(value, dict):
        raise ProtocolError(f"{description} is invalid")
    payload = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in payload):
        raise ProtocolError(f"{description} is invalid")
    count = _positive_integer(payload.get("count"), f"{description} count")
    for field in ("minimum_ms", "p95_ms", "maximum_ms"):
        _nonnegative_integer(payload.get(field), f"{description} {field}")
    median = payload.get("median_ms")
    if (
        not isinstance(median, int | float)
        or isinstance(median, bool)
        or float(median) < 0
    ):
        raise ProtocolError(f"{description} median is invalid")
    return count


def _duration_p95(value: object) -> int:
    payload = cast(dict[str, object], value)
    return cast(int, payload["p95_ms"])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
