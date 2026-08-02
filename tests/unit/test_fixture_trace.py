from __future__ import annotations

import json
from pathlib import Path

import pytest

from two_x_brainz.fixture_trace import FixtureTrace, FixtureTraceError


def test_fixture_trace_records_ordered_redacted_events(tmp_path: Path) -> None:
    trace = FixtureTrace(tmp_path, "synthetic-interview")
    trace.event(
        "provider_request",
        token="must-not-appear",
        nested={"authorization": "must-not-appear", "safe": "visible"},
    )
    trace.close()

    records = [
        json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()
    ]

    assert [record["sequence"] for record in records] == [1, 2]
    assert [record["kind"] for record in records] == [
        "fixture_trace_started",
        "provider_request",
    ]
    assert records[1]["token"] == "[REDACTED]"
    assert records[1]["nested"] == {
        "authorization": "[REDACTED]",
        "safe": "visible",
    }


def test_fixture_trace_rejects_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(FixtureTraceError, match="directory"):
        FixtureTrace(tmp_path / "missing", "synthetic-interview")


def test_fixture_trace_redacts_configured_secret_values(tmp_path: Path) -> None:
    secret_value = "synthetic-secret-value"
    trace = FixtureTrace(
        tmp_path,
        "synthetic-interview",
        secret_values=(secret_value,),
    )
    trace.event("diagnostic", message=f"upstream said {secret_value}")
    trace.close()

    records = [
        json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()
    ]

    assert records[1]["message"] == "upstream said [REDACTED]"


def test_fixture_trace_records_a_redacted_terminal_failure(tmp_path: Path) -> None:
    secret_value = "synthetic-secret-value"
    trace = FixtureTrace(
        tmp_path,
        "synthetic-interview",
        secret_values=(secret_value,),
    )
    trace.failure(RuntimeError(f"upstream rejected {secret_value}"))
    trace.close()

    records = [
        json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()
    ]

    assert records[-1] == {
        "sequence": 2,
        "elapsed_ms": records[-1]["elapsed_ms"],
        "kind": "fixture_failed",
        "error_type": "RuntimeError",
        "error_message": "upstream rejected [REDACTED]",
    }
