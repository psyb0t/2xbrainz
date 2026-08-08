"""Deterministic scoring and timing for conversation evaluation fixtures."""

from __future__ import annotations

import json
import math
import statistics
import unicodedata
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import cast

from two_x_brainz.benchmark import word_error_rate
from two_x_brainz.errors import ProtocolError

_SCENARIO_SCHEMA_VERSION = 1
_MAX_SCENARIO_BYTES = 64 * 1024
_MAX_SCENARIO_TURNS = 32
_MAX_TURN_TEXT_CHARACTERS = 2_000
_MAX_MARKERS_PER_GROUP = 8
_MAX_MARKER_CHARACTERS = 120
_OUTPUT_KINDS = ("draft", "commentary", "summary")
_MINIMUM_REAL_PROVIDER_CONCURRENCY = 3
_MINIMUM_REAL_OVERLAPPING_PAIRS = 3
_MAXIMUM_REAL_MEAN_WORD_ERROR_RATE = 0.25
_MAXIMUM_REAL_TURN_WORD_ERROR_RATE = 0.50
_TERMINAL_PROVIDER_PHASES = frozenset(
    {"request_completed", "request_cancelled", "request_failed"}
)
_TERMINAL_GENERATION_STATUSES = frozenset(
    {"completed", "cancelled", "failed", "superseded"}
)
_VISIBLE_PROVIDER_PHASES = frozenset(
    {"reasoning_streaming", "output_streaming", "tool_started", "tool_completed"}
)


@dataclass(frozen=True, slots=True)
class ScenarioTurn:
    """One ordered fictional utterance and its ASR quality anchors."""

    identifier: str
    speaker_role: str
    text: str
    marker_groups: tuple[tuple[str, ...], ...]
    interrupt_after_phase: str | None = None


@dataclass(frozen=True, slots=True)
class OutputExpectation:
    """Semantic anchors required from one terminal provider output."""

    marker_groups: tuple[tuple[str, ...], ...]
    forbidden_markers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConversationScenario:
    """Validated source of truth for one reproducible evaluation."""

    slug: str
    turns: tuple[ScenarioTurn, ...]
    outputs: dict[str, OutputExpectation]


@dataclass(frozen=True, slots=True)
class TimedRecord:
    """One production record on a monotonic evaluation timeline."""

    sequence: int
    elapsed_ms: int
    record: dict[str, object]


@dataclass(frozen=True, slots=True)
class DurationSummary:
    """Stable descriptive statistics for a bounded timing sample."""

    count: int
    minimum_ms: int
    median_ms: float
    p95_ms: int
    maximum_ms: int

    @classmethod
    def from_samples(cls, samples: list[int]) -> DurationSummary | None:
        if not samples:
            return None
        if any(sample < 0 for sample in samples):
            raise ProtocolError("evaluation timing samples must not be negative")
        ordered = sorted(samples)
        p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
        return cls(
            count=len(ordered),
            minimum_ms=ordered[0],
            median_ms=statistics.median(ordered),
            p95_ms=ordered[p95_index],
            maximum_ms=ordered[-1],
        )


@dataclass(frozen=True, slots=True)
class ProviderFlowTiming:
    """One provider request interval and its first-stream milestones."""

    flow_id: str
    generation_id: str
    context_revision: int
    output_kind: str
    model: str
    started_ms: int
    terminal_ms: int | None = None
    terminal_phase: str | None = None
    first_reasoning_ms: int | None = None
    first_output_ms: int | None = None


@dataclass(frozen=True, slots=True)
class QualityResult:
    """Hard marker result for one output or transcript."""

    passed: bool
    missing_groups: tuple[tuple[str, ...], ...]
    forbidden_hits: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """Mechanical, quality, concurrency, and timing result."""

    scenario: str
    passed: bool
    record_count: int
    timeline_turn_count: int
    terminal_generation_count: int
    duplicate_terminal_generation_ids: tuple[str, ...]
    provider_flow_count: int
    cancelled_provider_flow_count: int
    failed_provider_flow_count: int
    failed_provider_flow_ids: tuple[str, ...]
    incomplete_provider_flow_ids: tuple[str, ...]
    stale_provider_output_count: int
    interruption_to_cancellation_latencies: DurationSummary | None
    cancellation_to_replacement_latencies: DurationSummary | None
    replacement_context_revisions: tuple[int, ...]
    maximum_concurrent_provider_flows: int
    overlapping_provider_pairs: int
    provider_start_spread_ms: int | None
    provider_durations: DurationSummary | None
    first_reasoning_latencies: DurationSummary | None
    first_output_latencies: DurationSummary | None
    output_quality: dict[str, QualityResult]
    mean_word_error_rate: float | None = None
    maximum_word_error_rate: float | None = None
    research_tool_completed: bool | None = None

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-compatible report without provider prompts."""
        return cast(dict[str, object], asdict(self))


def load_scenario(path: Path) -> ConversationScenario:
    """Load a bounded, regular JSON scenario and reject unknown structure."""
    if path.is_symlink() or not path.is_file():
        raise ProtocolError("evaluation scenario must be a regular file")
    try:
        size = path.stat().st_size
        if size <= 0 or size > _MAX_SCENARIO_BYTES:
            raise ProtocolError("evaluation scenario size is invalid")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as error:
        raise ProtocolError("evaluation scenario must be UTF-8") from error
    except json.JSONDecodeError as error:
        raise ProtocolError("evaluation scenario must contain valid JSON") from error
    except OSError as error:
        raise ProtocolError("read evaluation scenario") from error
    return parse_scenario(payload)


def parse_scenario(payload: object) -> ConversationScenario:
    """Validate the complete scenario contract at its JSON boundary."""
    root = _object(payload, "evaluation scenario")
    _exact_keys(root, {"schema_version", "slug", "turns", "outputs"}, "scenario")
    if root["schema_version"] != _SCENARIO_SCHEMA_VERSION:
        raise ProtocolError("evaluation scenario schema version is unsupported")
    turn_payloads = _array(root["turns"], "scenario turns")
    if not 2 <= len(turn_payloads) <= _MAX_SCENARIO_TURNS:
        raise ProtocolError("evaluation scenario turn count is invalid")
    turns = tuple(_parse_turn(value) for value in turn_payloads)
    identifiers = [turn.identifier for turn in turns]
    if len(set(identifiers)) != len(identifiers):
        raise ProtocolError("evaluation scenario turn IDs must be unique")
    output_payload = _object(root["outputs"], "scenario outputs")
    _exact_keys(output_payload, set(_OUTPUT_KINDS), "scenario outputs")
    outputs = {
        kind: _parse_output_expectation(output_payload[kind], kind)
        for kind in _OUTPUT_KINDS
    }
    return ConversationScenario(
        slug=_bounded_text(root["slug"], "scenario slug", 80),
        turns=turns,
        outputs=outputs,
    )


def observations_from_trace(path: Path) -> tuple[TimedRecord, ...]:
    """Read flushed fixture JSONL and unwrap records in arrival order."""
    if path.is_symlink() or not path.is_file():
        raise ProtocolError("evaluation trace must be a regular file")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ProtocolError("read evaluation trace") from error
    observations: list[TimedRecord] = []
    previous_sequence = 0
    previous_elapsed_ms = -1
    for line_number, line in enumerate(lines, start=1):
        try:
            event = _object(json.loads(line), "evaluation trace event")
        except json.JSONDecodeError as error:
            raise ProtocolError(
                f"evaluation trace line {line_number} is invalid JSON"
            ) from error
        sequence = event.get("sequence")
        elapsed_ms = event.get("elapsed_ms")
        if not isinstance(sequence, int) or sequence != previous_sequence + 1:
            raise ProtocolError("evaluation trace sequence is not contiguous")
        if not isinstance(elapsed_ms, int) or elapsed_ms < previous_elapsed_ms:
            raise ProtocolError("evaluation trace elapsed time regressed")
        previous_sequence = sequence
        previous_elapsed_ms = elapsed_ms
        record = _trace_record(event)
        if record is not None:
            observations.append(TimedRecord(sequence, elapsed_ms, record))
    return tuple(observations)


def evaluate_observations(
    observations: tuple[TimedRecord, ...],
    scenario: ConversationScenario,
) -> EvaluationReport:
    """Score terminal outputs, event integrity, overlap, and latency."""
    _validate_observations(observations)
    flow_timings = provider_flow_timings(observations)
    interruption_latencies = _interruption_to_cancellation_latencies(
        observations,
        flow_timings,
    )
    replacement_latencies, replacement_revisions = _cancellation_to_replacement_metrics(
        flow_timings
    )
    stale_output_count = _stale_provider_output_count(observations, flow_timings)
    incomplete = tuple(
        timing.flow_id for timing in flow_timings if timing.terminal_ms is None
    )
    complete = tuple(
        timing for timing in flow_timings if timing.terminal_ms is not None
    )
    durations: list[int] = []
    for timing in complete:
        if timing.terminal_ms is None:
            continue
        durations.append(timing.terminal_ms - timing.started_ms)
    reasoning_latencies = [
        timing.first_reasoning_ms - timing.started_ms
        for timing in flow_timings
        if timing.first_reasoning_ms is not None
    ]
    output_latencies = [
        timing.first_output_ms - timing.started_ms
        for timing in flow_timings
        if timing.first_output_ms is not None
    ]
    terminal_ids, duplicates = _terminal_generation_ids(observations)
    quality = {
        kind: score_text(_latest_completed_text(observations, kind), expectation)
        for kind, expectation in scenario.outputs.items()
    }
    starts = [timing.started_ms for timing in flow_timings]
    maximum_concurrency = _maximum_concurrency(complete)
    overlapping_pairs = _overlapping_pairs(complete)
    failed_flow_ids = tuple(
        timing.flow_id
        for timing in complete
        if timing.terminal_phase == "request_failed"
    )
    passed = (
        bool(flow_timings)
        and not duplicates
        and not incomplete
        and not failed_flow_ids
        and stale_output_count == 0
        and all(result.passed for result in quality.values())
    )
    return EvaluationReport(
        scenario=scenario.slug,
        passed=passed,
        record_count=len(observations),
        timeline_turn_count=sum(
            observation.record.get("kind") == "timeline" for observation in observations
        ),
        terminal_generation_count=len(terminal_ids),
        duplicate_terminal_generation_ids=duplicates,
        provider_flow_count=len(flow_timings),
        cancelled_provider_flow_count=sum(
            timing.terminal_phase == "request_cancelled" for timing in complete
        ),
        failed_provider_flow_count=len(failed_flow_ids),
        failed_provider_flow_ids=failed_flow_ids,
        incomplete_provider_flow_ids=incomplete,
        stale_provider_output_count=stale_output_count,
        interruption_to_cancellation_latencies=DurationSummary.from_samples(
            interruption_latencies
        ),
        cancellation_to_replacement_latencies=DurationSummary.from_samples(
            replacement_latencies
        ),
        replacement_context_revisions=replacement_revisions,
        maximum_concurrent_provider_flows=maximum_concurrency,
        overlapping_provider_pairs=overlapping_pairs,
        provider_start_spread_ms=max(starts) - min(starts) if starts else None,
        provider_durations=DurationSummary.from_samples(durations),
        first_reasoning_latencies=DurationSummary.from_samples(reasoning_latencies),
        first_output_latencies=DurationSummary.from_samples(output_latencies),
        output_quality=quality,
    )


def apply_live_quality_gates(
    report: EvaluationReport,
    scenario: ConversationScenario,
    word_error_rates: dict[str, float],
    *,
    research_tool_completed: bool,
) -> EvaluationReport:
    """Apply real ASR, interruption, research, and concurrency hard gates."""
    expected_ids = {turn.identifier for turn in scenario.turns}
    if set(word_error_rates) != expected_ids:
        raise ProtocolError("word error rate turn IDs do not match the scenario")
    if not word_error_rates or any(
        not math.isfinite(rate) or rate < 0 for rate in word_error_rates.values()
    ):
        raise ProtocolError("word error rates are invalid")
    mean_rate = statistics.mean(word_error_rates.values())
    maximum_rate = max(word_error_rates.values())
    required_interruptions = sum(
        turn.interrupt_after_phase is not None for turn in scenario.turns
    )
    measured_interruptions = (
        report.interruption_to_cancellation_latencies.count
        if report.interruption_to_cancellation_latencies is not None
        else 0
    )
    measured_replacements = (
        report.cancellation_to_replacement_latencies.count
        if report.cancellation_to_replacement_latencies is not None
        else 0
    )
    live_passed = (
        report.passed
        and report.timeline_turn_count == len(scenario.turns)
        and report.cancelled_provider_flow_count >= required_interruptions
        and measured_interruptions >= required_interruptions
        and measured_replacements >= required_interruptions
        and len(report.replacement_context_revisions) >= required_interruptions
        and report.maximum_concurrent_provider_flows
        >= _MINIMUM_REAL_PROVIDER_CONCURRENCY
        and report.overlapping_provider_pairs >= _MINIMUM_REAL_OVERLAPPING_PAIRS
        and mean_rate <= _MAXIMUM_REAL_MEAN_WORD_ERROR_RATE
        and maximum_rate <= _MAXIMUM_REAL_TURN_WORD_ERROR_RATE
        and research_tool_completed
    )
    return replace(
        report,
        passed=live_passed,
        mean_word_error_rate=mean_rate,
        maximum_word_error_rate=maximum_rate,
        research_tool_completed=research_tool_completed,
    )


def provider_flow_timings(
    observations: tuple[TimedRecord, ...],
) -> tuple[ProviderFlowTiming, ...]:
    """Pair provider lifecycle records without assuming completion order."""
    states: dict[str, ProviderFlowTiming] = {}
    order: list[str] = []
    for observation in observations:
        record = observation.record
        if record.get("kind") != "provider_activity":
            continue
        flow_id = _bounded_text(record.get("flow_id"), "provider flow ID", 80)
        phase = _bounded_text(record.get("phase"), "provider phase", 64)
        if phase == "request_started":
            if flow_id in states:
                raise ProtocolError("provider flow started more than once")
            output_kind = _bounded_text(
                record.get("output_kind"), "provider output kind", 32
            )
            if output_kind not in _OUTPUT_KINDS:
                raise ProtocolError("provider activity has an unknown output kind")
            states[flow_id] = ProviderFlowTiming(
                flow_id=flow_id,
                generation_id=_bounded_text(
                    record.get("generation_id"), "provider generation ID", 80
                ),
                context_revision=_non_negative_integer(
                    record.get("context_revision"),
                    "provider context revision",
                ),
                output_kind=output_kind,
                model=_bounded_text(record.get("model"), "provider model", 160),
                started_ms=observation.elapsed_ms,
            )
            order.append(flow_id)
            continue
        state = states.get(flow_id)
        if state is None:
            raise ProtocolError("provider activity preceded request start")
        changes: dict[str, object] = {}
        if (
            phase == "reasoning_streaming"
            and state.first_reasoning_ms is None
            and _has_visible_stream_text(record.get("reasoning"))
        ):
            changes["first_reasoning_ms"] = observation.elapsed_ms
        if (
            phase == "output_streaming"
            and state.first_output_ms is None
            and _has_visible_stream_text(record.get("output"))
        ):
            changes["first_output_ms"] = observation.elapsed_ms
        if phase in _TERMINAL_PROVIDER_PHASES:
            if state.terminal_ms is not None:
                raise ProtocolError("provider flow emitted multiple terminal phases")
            changes["terminal_ms"] = observation.elapsed_ms
            changes["terminal_phase"] = phase
        if changes:
            states[flow_id] = _replace_flow_timing(state, changes)
    return tuple(states[flow_id] for flow_id in order)


def _interruption_to_cancellation_latencies(
    observations: tuple[TimedRecord, ...],
    flows: tuple[ProviderFlowTiming, ...],
) -> list[int]:
    latencies: list[int] = []
    for observation in observations:
        if observation.record.get("kind") != "evaluation_interruption":
            continue
        context_revision = _non_negative_integer(
            observation.record.get("context_revision"),
            "interruption context revision",
        )
        cancellation_times = [
            flow.terminal_ms
            for flow in flows
            if flow.terminal_phase == "request_cancelled"
            and flow.context_revision < context_revision
            and flow.terminal_ms is not None
            and flow.terminal_ms >= observation.elapsed_ms
        ]
        if cancellation_times:
            latencies.append(min(cancellation_times) - observation.elapsed_ms)
    return latencies


def _cancellation_to_replacement_metrics(
    flows: tuple[ProviderFlowTiming, ...],
) -> tuple[list[int], tuple[int, ...]]:
    latencies: list[int] = []
    revisions: list[int] = []
    for cancelled in flows:
        if (
            cancelled.terminal_phase != "request_cancelled"
            or cancelled.terminal_ms is None
        ):
            continue
        replacements = [
            candidate
            for candidate in flows
            if candidate.output_kind == cancelled.output_kind
            and candidate.context_revision > cancelled.context_revision
            and candidate.started_ms >= cancelled.terminal_ms
        ]
        if not replacements:
            continue
        replacement = min(replacements, key=lambda candidate: candidate.started_ms)
        latencies.append(replacement.started_ms - cancelled.terminal_ms)
        revisions.append(replacement.context_revision)
    return latencies, tuple(revisions)


def _stale_provider_output_count(
    observations: tuple[TimedRecord, ...],
    flows: tuple[ProviderFlowTiming, ...],
) -> int:
    by_identifier = {flow.flow_id: flow for flow in flows}
    count = 0
    for observation in observations:
        record = observation.record
        if (
            record.get("kind") != "provider_activity"
            or record.get("phase") not in _VISIBLE_PROVIDER_PHASES
        ):
            continue
        flow_id = _bounded_text(record.get("flow_id"), "provider flow ID", 80)
        flow = by_identifier.get(flow_id)
        if flow is None:
            raise ProtocolError("provider activity preceded request start")
        if flow.terminal_ms is not None and observation.elapsed_ms > flow.terminal_ms:
            count += 1
            continue
        if any(
            candidate.output_kind == flow.output_kind
            and candidate.context_revision > flow.context_revision
            and candidate.started_ms <= observation.elapsed_ms
            for candidate in flows
        ):
            count += 1
    return count


def _has_visible_stream_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def score_text(text: str, expectation: OutputExpectation) -> QualityResult:
    """Score marker groups without requiring brittle exact prose."""
    normalized_forms = _normalized_text_forms(text)
    missing = tuple(
        group
        for group in expectation.marker_groups
        if not any(_marker_is_present(marker, normalized_forms) for marker in group)
    )
    forbidden = tuple(
        marker
        for marker in expectation.forbidden_markers
        if _marker_is_present(marker, normalized_forms)
    )
    return QualityResult(not missing and not forbidden, missing, forbidden)


def _marker_is_present(marker: str, text_forms: frozenset[str]) -> bool:
    return any(
        marker_form in text_form
        for marker_form in _normalized_text_forms(marker)
        for text_form in text_forms
    )


def _normalized_text_forms(text: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    dash_removed = "".join(
        "" if unicodedata.category(character) == "Pd" else character
        for character in normalized
    )
    dash_spaced = "".join(
        " " if unicodedata.category(character) == "Pd" else character
        for character in normalized
    )
    return frozenset(
        {
            " ".join(dash_removed.split()),
            " ".join(dash_spaced.split()),
        }
    )


def scenario_word_error_rates(
    scenario: ConversationScenario,
    recognized_by_turn: dict[str, str],
) -> dict[str, float]:
    """Calculate per-turn WER and reject missing or unknown results."""
    expected_ids = {turn.identifier for turn in scenario.turns}
    if set(recognized_by_turn) != expected_ids:
        raise ProtocolError("recognized turn IDs do not match the scenario")
    return {
        turn.identifier: word_error_rate(turn.text, recognized_by_turn[turn.identifier])
        for turn in scenario.turns
    }


def write_report(
    directory: Path,
    report: EvaluationReport,
    *,
    word_error_rates: dict[str, float] | None = None,
) -> tuple[Path, Path]:
    """Create exclusive JSON and Markdown scorecards."""
    if not directory.is_dir():
        raise ProtocolError("evaluation report directory is unavailable")
    payload = report.as_dict()
    if word_error_rates is not None:
        payload["word_error_rates"] = word_error_rates
        payload["mean_word_error_rate"] = statistics.mean(word_error_rates.values())
    json_path = directory / "scorecard.json"
    markdown_path = directory / "report.md"
    try:
        with json_path.open("x", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True)
            file.write("\n")
        with markdown_path.open("x", encoding="utf-8") as file:
            file.write(_markdown_report(payload))
    except OSError as error:
        raise ProtocolError("write evaluation report") from error
    return json_path, markdown_path


def _parse_turn(value: object) -> ScenarioTurn:
    turn = _object(value, "scenario turn")
    required = {"id", "speaker_role", "text", "marker_groups"}
    allowed = required | {"interrupt_after_phase"}
    if not required.issubset(turn) or not set(turn).issubset(allowed):
        raise ProtocolError("scenario turn fields are invalid")
    speaker_role = _bounded_text(turn["speaker_role"], "speaker role", 16)
    if speaker_role not in {"user", "remote"}:
        raise ProtocolError("scenario speaker role is invalid")
    interrupt_value = turn.get("interrupt_after_phase")
    interrupt_phase = (
        _bounded_text(interrupt_value, "interrupt phase", 64)
        if interrupt_value is not None
        else None
    )
    return ScenarioTurn(
        identifier=_bounded_text(turn["id"], "turn ID", 80),
        speaker_role=speaker_role,
        text=_bounded_text(turn["text"], "turn text", _MAX_TURN_TEXT_CHARACTERS),
        marker_groups=_marker_groups(turn["marker_groups"], "turn markers"),
        interrupt_after_phase=interrupt_phase,
    )


def _parse_output_expectation(value: object, kind: str) -> OutputExpectation:
    expectation = _object(value, f"{kind} expectation")
    _exact_keys(
        expectation,
        {"marker_groups", "forbidden_markers"},
        f"{kind} expectation",
    )
    forbidden = tuple(
        _bounded_text(marker, f"{kind} forbidden marker", _MAX_MARKER_CHARACTERS)
        for marker in _array(
            expectation["forbidden_markers"], f"{kind} forbidden markers"
        )
    )
    return OutputExpectation(
        _marker_groups(expectation["marker_groups"], f"{kind} markers"),
        forbidden,
    )


def _marker_groups(value: object, description: str) -> tuple[tuple[str, ...], ...]:
    payload = _array(value, description)
    if not payload:
        raise ProtocolError(f"{description} must not be empty")
    groups: list[tuple[str, ...]] = []
    for group_value in payload:
        group = _array(group_value, f"{description} group")
        if not 1 <= len(group) <= _MAX_MARKERS_PER_GROUP:
            raise ProtocolError(f"{description} group size is invalid")
        groups.append(
            tuple(
                _bounded_text(marker, description, _MAX_MARKER_CHARACTERS)
                for marker in group
            )
        )
    return tuple(groups)


def _trace_record(event: dict[str, object]) -> dict[str, object] | None:
    if event.get("kind") == "live_json_record":
        return _object(event.get("record"), "live JSON record")
    if event.get("kind") == "provider_activity":
        return {**event, "kind": "provider_activity"}
    if event.get("kind") == "evaluation_observation":
        return _object(event.get("record"), "evaluation observation")
    return None


def _validate_observations(observations: tuple[TimedRecord, ...]) -> None:
    previous_sequence = 0
    previous_elapsed_ms = -1
    for observation in observations:
        if observation.sequence <= previous_sequence:
            raise ProtocolError("evaluation observation sequence is not increasing")
        if observation.elapsed_ms < previous_elapsed_ms:
            raise ProtocolError("evaluation observation elapsed time regressed")
        previous_sequence = observation.sequence
        previous_elapsed_ms = observation.elapsed_ms


def _terminal_generation_ids(
    observations: tuple[TimedRecord, ...],
) -> tuple[set[str], tuple[str, ...]]:
    terminal_ids: set[str] = set()
    duplicates: set[str] = set()
    for observation in observations:
        record = observation.record
        if record.get("kind") not in _OUTPUT_KINDS:
            continue
        if record.get("status") not in _TERMINAL_GENERATION_STATUSES:
            continue
        generation_id = _bounded_text(
            record.get("generation_id"), "terminal generation ID", 80
        )
        if generation_id in terminal_ids:
            duplicates.add(generation_id)
        terminal_ids.add(generation_id)
    return terminal_ids, tuple(sorted(duplicates))


def _latest_completed_text(
    observations: tuple[TimedRecord, ...],
    kind: str,
) -> str:
    for observation in reversed(observations):
        record = observation.record
        if record.get("kind") != kind or record.get("status") != "completed":
            continue
        text = record.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return ""


def _replace_flow_timing(
    timing: ProviderFlowTiming,
    changes: dict[str, object],
) -> ProviderFlowTiming:
    return replace(
        timing,
        terminal_ms=cast(int | None, changes.get("terminal_ms", timing.terminal_ms)),
        terminal_phase=cast(
            str | None,
            changes.get("terminal_phase", timing.terminal_phase),
        ),
        first_reasoning_ms=cast(
            int | None,
            changes.get("first_reasoning_ms", timing.first_reasoning_ms),
        ),
        first_output_ms=cast(
            int | None,
            changes.get("first_output_ms", timing.first_output_ms),
        ),
    )


def _maximum_concurrency(flows: tuple[ProviderFlowTiming, ...]) -> int:
    milestones: list[tuple[int, int]] = []
    for flow in flows:
        if flow.terminal_ms is None:
            continue
        milestones.extend(((flow.started_ms, 1), (flow.terminal_ms, -1)))
    active = 0
    maximum = 0
    for _, change in sorted(milestones, key=lambda item: (item[0], -item[1])):
        active += change
        maximum = max(maximum, active)
    return maximum


def _overlapping_pairs(flows: tuple[ProviderFlowTiming, ...]) -> int:
    overlaps = 0
    for index, first in enumerate(flows):
        if first.terminal_ms is None:
            continue
        for second in flows[index + 1 :]:
            if second.terminal_ms is None:
                continue
            if max(first.started_ms, second.started_ms) < min(
                first.terminal_ms, second.terminal_ms
            ):
                overlaps += 1
    return overlaps


def _markdown_report(payload: dict[str, object]) -> str:
    status = "PASS" if payload["passed"] else "FAIL"
    return f"""# Conversation evaluation — {payload["scenario"]}

- Result: **{status}**
- Timeline turns: {payload["timeline_turn_count"]}
- Provider flows: {payload["provider_flow_count"]}
- Cancelled provider flows: {payload["cancelled_provider_flow_count"]}
- Failed provider flows: {payload["failed_provider_flow_count"]}
- Stale provider output events: {payload["stale_provider_output_count"]}
- Interruption-to-cancellation: {payload["interruption_to_cancellation_latencies"]}
- Cancellation-to-replacement: {payload["cancellation_to_replacement_latencies"]}
- Replacement context revisions: {payload["replacement_context_revisions"]}
- Maximum concurrent provider flows: {payload["maximum_concurrent_provider_flows"]}
- Overlapping provider pairs: {payload["overlapping_provider_pairs"]}
- Provider start spread: {payload["provider_start_spread_ms"]} ms
- Mean word error rate: {payload["mean_word_error_rate"]}
- Maximum turn word error rate: {payload["maximum_word_error_rate"]}
- Research tool completed: {payload["research_tool_completed"]}
"""


def _object(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProtocolError(f"{description} must be an object")
    mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise ProtocolError(f"{description} keys must be text")
    return cast(dict[str, object], value)


def _array(value: object, description: str) -> list[object]:
    if not isinstance(value, list):
        raise ProtocolError(f"{description} must be an array")
    return cast(list[object], value)


def _exact_keys(
    value: dict[str, object],
    expected: set[str],
    description: str,
) -> None:
    if set(value) != expected:
        raise ProtocolError(f"{description} fields are invalid")


def _bounded_text(value: object, description: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{description} must be text")
    text = value.strip()
    if not text or len(text) > maximum or "\x00" in text:
        raise ProtocolError(f"{description} is invalid")
    return text


def _non_negative_integer(value: object, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProtocolError(f"{description} must be a non-negative integer")
    return value
