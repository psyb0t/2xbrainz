from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from two_x_brainz.errors import ProtocolError
from two_x_brainz.evaluation import (
    DurationSummary,
    OutputExpectation,
    TimedRecord,
    apply_live_quality_gates,
    evaluate_observations,
    load_scenario,
    observations_from_trace,
    parse_scenario,
    provider_flow_timings,
    scenario_word_error_rates,
    score_text,
    write_report,
)
from two_x_brainz.fixture_trace import FixtureTrace

_SCENARIO_PATH = Path("tests/fixtures/slang-interrupted-project-chat.json")


def _add_unknown_field(payload: dict[str, Any]) -> None:
    payload["unknown"] = True


def _duplicate_turn_id(payload: dict[str, Any]) -> None:
    payload["turns"][1]["id"] = "user-restart"


def _replace_speaker_role(payload: dict[str, Any]) -> None:
    payload["turns"][0]["speaker_role"] = "other"


def _remove_summary_output(payload: dict[str, Any]) -> None:
    payload["outputs"].pop("summary")


def test_loads_realistic_eight_turn_scenario() -> None:
    scenario = load_scenario(_SCENARIO_PATH)

    assert scenario.slug == "slang-interrupted-project-chat"
    assert len(scenario.turns) == 8
    assert {turn.speaker_role for turn in scenario.turns} == {"user", "remote"}
    assert scenario.turns[3].interrupt_after_phase == "output_streaming"
    assert scenario.turns[5].interrupt_after_phase == "tool_started"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (_add_unknown_field, "scenario fields"),
        (_duplicate_turn_id, "turn IDs"),
        (_replace_speaker_role, "speaker role"),
        (_remove_summary_output, "outputs fields"),
    ],
)
def test_rejects_invalid_scenario_boundaries(
    mutation: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    payload: dict[str, Any] = json.loads(_SCENARIO_PATH.read_text(encoding="utf-8"))
    mutation(payload)

    with pytest.raises(ProtocolError, match=message):
        parse_scenario(payload)


def test_rejects_symlinked_scenario(tmp_path: Path) -> None:
    link = tmp_path / "scenario.json"
    link.symlink_to(_SCENARIO_PATH.resolve())

    with pytest.raises(ProtocolError, match="regular file"):
        load_scenario(link)


def test_duration_summary_uses_nearest_rank_p95() -> None:
    result = DurationSummary.from_samples([40, 10, 30, 20])

    assert result == DurationSummary(4, 10, 25.0, 40, 40)
    assert DurationSummary.from_samples([]) is None
    with pytest.raises(ProtocolError, match="negative"):
        DurationSummary.from_samples([-1])


def test_provider_timing_ignores_empty_stream_markers() -> None:
    observations = (
        TimedRecord(
            1,
            100,
            {
                "kind": "provider_activity",
                "phase": "request_started",
                "flow_id": "flow-1",
                "generation_id": "generation-1",
                "context_revision": 1,
                "output_kind": "draft",
                "model": "model-1",
            },
        ),
        TimedRecord(
            2,
            110,
            {
                "kind": "provider_activity",
                "phase": "output_streaming",
                "flow_id": "flow-1",
                "output": "",
            },
        ),
        TimedRecord(
            3,
            145,
            {
                "kind": "provider_activity",
                "phase": "output_streaming",
                "flow_id": "flow-1",
                "output": "Visible",
            },
        ),
        TimedRecord(
            4,
            150,
            {
                "kind": "provider_activity",
                "phase": "request_completed",
                "flow_id": "flow-1",
            },
        ),
    )

    timings = provider_flow_timings(observations)

    assert timings[0].first_output_ms == 145


def test_score_text_accepts_alternatives_and_rejects_stale_claim() -> None:
    expectation = OutputExpectation(
        marker_groups=(("thursday",), ("before lunch", "lunch")),
        forbidden_markers=("friday is the final date",),
    )

    passing = score_text("Thursday works; the numbers land at lunch.", expectation)
    failing = score_text("Friday is the final date.", expectation)

    assert passing.passed is True
    assert failing.passed is False
    assert failing.forbidden_hits == ("friday is the final date",)


def test_score_text_normalizes_unicode_compound_hyphens() -> None:
    expectation = OutputExpectation(
        marker_groups=(("failover",), ("machine readable",)),
        forbidden_markers=(),
    )

    result = score_text(
        "The fail‑over test covers a machine-readable link set.",  # noqa: RUF001
        expectation,
    )

    assert result.passed is True
    assert result.missing_groups == ()


@pytest.mark.parametrize(
    ("output_kind", "text", "forbidden_marker"),
    [
        (
            "commentary",
            "You still owe the remote a clean one-line pitch.",
            "still owe the remote a clean",
        ),
        (
            "summary",
            "The gateway uses one API and failover, but the pitch remains open.",
            "pitch remains open",
        ),
    ],
)
def test_scenario_rejects_answered_request_marked_open(
    output_kind: str,
    text: str,
    forbidden_marker: str,
) -> None:
    scenario = load_scenario(_SCENARIO_PATH)

    result = score_text(text, scenario.outputs[output_kind])

    assert result.passed is False
    assert forbidden_marker in result.forbidden_hits


def test_evaluation_proves_three_overlapping_flows_and_quality() -> None:
    report = evaluate_observations(
        _passing_observations(),
        load_scenario(_SCENARIO_PATH),
    )

    assert report.passed is True
    assert report.timeline_turn_count == 8
    assert report.provider_flow_count == 3
    assert report.maximum_concurrent_provider_flows == 3
    assert report.overlapping_provider_pairs == 3
    assert report.provider_start_spread_ms == 2
    assert report.provider_durations == DurationSummary(3, 36, 39, 40, 40)
    assert report.first_output_latencies == DurationSummary(3, 9, 9, 9, 9)


def test_evaluation_rejects_duplicate_terminal_generation() -> None:
    scenario = load_scenario(_SCENARIO_PATH)
    observations = list(_passing_observations())
    observations.append(
        TimedRecord(
            sequence=observations[-1].sequence + 1,
            elapsed_ms=observations[-1].elapsed_ms + 1,
            record={
                "kind": "draft",
                "generation_id": "generation-draft",
                "status": "completed",
                "text": "duplicate",
            },
        )
    )

    report = evaluate_observations(tuple(observations), scenario)

    assert report.passed is False
    assert report.duplicate_terminal_generation_ids == ("generation-draft",)


def test_evaluation_rejects_failed_provider_flow_even_after_later_success() -> None:
    scenario = load_scenario(_SCENARIO_PATH)
    observations = list(_passing_observations())
    next_sequence = observations[-1].sequence + 1
    observations.extend(
        (
            TimedRecord(
                sequence=next_sequence,
                elapsed_ms=53,
                record={
                    "kind": "provider_activity",
                    "phase": "request_started",
                    "flow_id": "flow-commentary-failed",
                    "generation_id": "generation-commentary-failed",
                    "context_revision": 9,
                    "output_kind": "commentary",
                    "model": "model-commentary",
                },
            ),
            TimedRecord(
                sequence=next_sequence + 1,
                elapsed_ms=54,
                record={
                    "kind": "provider_activity",
                    "phase": "request_failed",
                    "flow_id": "flow-commentary-failed",
                },
            ),
        )
    )

    report = evaluate_observations(tuple(observations), scenario)

    assert report.passed is False
    assert report.failed_provider_flow_count == 1
    assert report.failed_provider_flow_ids == ("flow-commentary-failed",)


def test_evaluation_measures_interruption_cancellation_and_replacement() -> None:
    report = evaluate_observations(
        _interruption_observations(),
        load_scenario(_SCENARIO_PATH),
    )

    assert report.passed is True
    assert report.stale_provider_output_count == 0
    assert report.interruption_to_cancellation_latencies == DurationSummary(
        1, 3, 3, 3, 3
    )
    assert report.cancellation_to_replacement_latencies == DurationSummary(
        1, 2, 2, 2, 2
    )
    assert report.replacement_context_revisions == (3,)


def test_evaluation_rejects_stale_output_after_replacement_started() -> None:
    report = evaluate_observations(
        _interruption_observations(stale_output=True),
        load_scenario(_SCENARIO_PATH),
    )

    assert report.passed is False
    assert report.stale_provider_output_count == 1


def test_provider_timing_requires_generation_and_context_metadata() -> None:
    observation = TimedRecord(
        1,
        0,
        {
            "kind": "provider_activity",
            "phase": "request_started",
            "flow_id": "flow-without-context",
            "output_kind": "draft",
            "model": "model-draft",
        },
    )

    with pytest.raises(ProtocolError, match="provider generation ID"):
        provider_flow_timings((observation,))


def test_live_quality_gates_require_wer_research_and_interruption_recovery() -> None:
    scenario = load_scenario(_SCENARIO_PATH)
    report = evaluate_observations(_passing_observations(), scenario)
    exact_rates = {turn.identifier: 0.0 for turn in scenario.turns}

    failed = apply_live_quality_gates(
        report,
        scenario,
        exact_rates,
        research_tool_completed=False,
    )

    assert failed.passed is False
    assert failed.research_tool_completed is False
    assert failed.mean_word_error_rate == 0.0


def test_live_quality_gates_reject_excessive_turn_word_error_rate() -> None:
    scenario = load_scenario(_SCENARIO_PATH)
    report = evaluate_observations(_passing_observations(), scenario)
    rates = {turn.identifier: 0.0 for turn in scenario.turns}
    rates[scenario.turns[0].identifier] = 0.51

    failed = apply_live_quality_gates(
        report,
        scenario,
        rates,
        research_tool_completed=True,
    )

    assert failed.passed is False
    assert failed.maximum_word_error_rate == 0.51


def test_trace_loader_unwraps_live_and_direct_provider_activity(tmp_path: Path) -> None:
    trace = FixtureTrace(tmp_path, "evaluation")
    trace.event(
        "live_json_record",
        record={"kind": "timeline", "speaker_role": "user", "text": "hello"},
    )
    trace.event(
        "provider_activity",
        phase="request_started",
        flow_id="flow-1",
        generation_id="generation-1",
        context_revision=1,
        output_kind="draft",
        model="model-1",
    )
    trace.close()

    observations = observations_from_trace(trace.path)

    assert [observation.record["kind"] for observation in observations] == [
        "timeline",
        "provider_activity",
    ]


def test_trace_loader_rejects_non_contiguous_sequence(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text(
        '{"sequence":1,"elapsed_ms":0,"kind":"fixture_trace_started"}\n'
        '{"sequence":3,"elapsed_ms":1,"kind":"fixture_passed"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ProtocolError, match="sequence"):
        observations_from_trace(path)


def test_scenario_word_error_rates_require_every_turn() -> None:
    scenario = load_scenario(_SCENARIO_PATH)
    exact = {turn.identifier: turn.text for turn in scenario.turns}

    assert scenario_word_error_rates(scenario, exact) == {
        turn.identifier: 0.0 for turn in scenario.turns
    }
    exact.pop(scenario.turns[0].identifier)
    with pytest.raises(ProtocolError, match="turn IDs"):
        scenario_word_error_rates(scenario, exact)


def test_report_writer_is_exclusive_and_contains_metrics(tmp_path: Path) -> None:
    report = evaluate_observations(
        _passing_observations(),
        load_scenario(_SCENARIO_PATH),
    )

    json_path, markdown_path = write_report(
        tmp_path,
        report,
        word_error_rates={"turn": 0.25},
    )

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["mean_word_error_rate"] == 0.25
    assert "Maximum concurrent provider flows: 3" in markdown_path.read_text(
        encoding="utf-8"
    )
    with pytest.raises(ProtocolError, match="write evaluation report"):
        write_report(tmp_path, report)


def _passing_observations() -> tuple[TimedRecord, ...]:
    records: list[TimedRecord] = []

    def add(elapsed_ms: int, record: dict[str, object]) -> None:
        records.append(TimedRecord(len(records) + 1, elapsed_ms, record))

    for index in range(8):
        add(index, {"kind": "timeline", "turn_id": f"turn-{index}"})
    for index, kind in enumerate(("draft", "commentary", "summary")):
        add(
            10 + index,
            {
                "kind": "provider_activity",
                "phase": "request_started",
                "flow_id": f"flow-{kind}",
                "generation_id": f"generation-{kind}",
                "context_revision": 8,
                "output_kind": kind,
                "model": f"model-{kind}",
            },
        )
    for index, kind in enumerate(("draft", "commentary", "summary")):
        add(
            19 + index,
            {
                "kind": "provider_activity",
                "phase": "output_streaming",
                "flow_id": f"flow-{kind}",
                "output": "visible",
            },
        )
    for terminal_ms, kind in (
        (47, "commentary"),
        (50, "draft"),
        (51, "summary"),
    ):
        add(
            terminal_ms,
            {
                "kind": "provider_activity",
                "phase": "request_completed",
                "flow_id": f"flow-{kind}",
            },
        )
    output_text = {
        "draft": (
            "Thursday: finish the failover test and send numbers before lunch. "
            "Mention linkset."
        ),
        "commentary": (
            "Keep the Thursday failover commitment clear and verify the linkset "
            "research."
        ),
        "summary": (
            "RelayCrate is the gateway. Thursday failover numbers are due before "
            "lunch; RFC 9264 linkset is under review."
        ),
    }
    for kind in ("draft", "commentary", "summary"):
        add(
            52,
            {
                "kind": kind,
                "generation_id": f"generation-{kind}",
                "status": "completed",
                "text": output_text[kind],
            },
        )
    return tuple(records)


def _interruption_observations(
    *,
    stale_output: bool = False,
) -> tuple[TimedRecord, ...]:
    records: list[TimedRecord] = []

    def add(elapsed_ms: int, record: dict[str, object]) -> None:
        records.append(TimedRecord(len(records) + 1, elapsed_ms, record))

    for index in range(8):
        add(index, {"kind": "timeline", "turn_id": f"turn-{index}"})
    add(
        10,
        {
            "kind": "provider_activity",
            "phase": "request_started",
            "flow_id": "flow-old",
            "generation_id": "generation-old",
            "context_revision": 2,
            "output_kind": "draft",
            "model": "model-draft",
        },
    )
    add(20, {"kind": "evaluation_interruption", "context_revision": 3})
    add(
        23,
        {
            "kind": "provider_activity",
            "phase": "request_cancelled",
            "flow_id": "flow-old",
        },
    )
    add(
        25,
        {
            "kind": "provider_activity",
            "phase": "request_started",
            "flow_id": "flow-new",
            "generation_id": "generation-new",
            "context_revision": 3,
            "output_kind": "draft",
            "model": "model-draft",
        },
    )
    if stale_output:
        add(
            26,
            {
                "kind": "provider_activity",
                "phase": "output_streaming",
                "flow_id": "flow-old",
                "output": "obsolete",
            },
        )
    add(
        27,
        {
            "kind": "provider_activity",
            "phase": "output_streaming",
            "flow_id": "flow-new",
            "output": "current",
        },
    )
    add(
        30,
        {
            "kind": "provider_activity",
            "phase": "request_completed",
            "flow_id": "flow-new",
        },
    )
    for kind, text in {
        "draft": (
            "Thursday: finish the failover test and send numbers before lunch. "
            "Mention linkset."
        ),
        "commentary": (
            "Keep the Thursday failover commitment clear and verify the linkset "
            "research."
        ),
        "summary": (
            "RelayCrate is the gateway. Thursday failover numbers are due before "
            "lunch; RFC 9264 linkset is under review."
        ),
    }.items():
        add(
            31,
            {
                "kind": kind,
                "generation_id": f"terminal-{kind}",
                "status": "completed",
                "text": text,
            },
        )
    return tuple(records)
