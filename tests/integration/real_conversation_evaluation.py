"""Opt-in generated-speech evaluation against real Talkies and AIGate."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from live_talkies_tts_fixture import TTS_VOICE_ENV, synthesize_wav

from two_x_brainz.aigate import AIGateClient
from two_x_brainz.audio import WavFixture, load_wav_fixture
from two_x_brainz.config import Settings
from two_x_brainz.constants import (
    DEFAULT_CHANNELS,
    DEFAULT_FRAME_BYTES,
    DEFAULT_FRAME_DURATION_MS,
    DEFAULT_SAMPLE_RATE_HZ,
    MAX_AIGATE_MODEL_ID_CHARACTERS,
)
from two_x_brainz.contracts import (
    ASRStreamStats,
    AudioFrame,
    DraftResult,
    InsightResult,
    SpeakerRole,
    TranscriptEvent,
    TranscriptEventType,
)
from two_x_brainz.coordinator import ConversationCoordinator
from two_x_brainz.errors import ProtocolError, TwoXBrainzError
from two_x_brainz.evaluation import (
    ConversationScenario,
    EvaluationReport,
    ScenarioTurn,
    TimedRecord,
    apply_live_quality_gates,
    evaluate_observations,
    load_scenario,
    scenario_word_error_rates,
    write_report,
)
from two_x_brainz.fixture_trace import FixtureTrace, FixtureTraceError

_SCENARIO_ENV = "TWOXBRAINZ_EVALUATION_SCENARIO"
_TRACE_DIRECTORY_ENV = "TWOXBRAINZ_FIXTURE_TRACE_DIR"
_WORK_DIRECTORY_ENV = "TWOXBRAINZ_FIXTURE_WORK_DIR"
_USER_VOICE_ENV = "TWOXBRAINZ_EVALUATION_USER_VOICE"
_REMOTE_VOICE_ENV = "TWOXBRAINZ_EVALUATION_REMOTE_VOICE"
_REPEATS_ENV = "TWOXBRAINZ_EVALUATION_REPEATS"
_TALKIES_MODEL_ENV = "TWOXBRAINZ_FIXTURE_TALKIES_MODEL"
_DRAFT_MODEL_ENV = "TWOXBRAINZ_FIXTURE_DRAFT_MODEL"
_COMMENTARY_MODEL_ENV = "TWOXBRAINZ_FIXTURE_COMMENTARY_MODEL"
_SUMMARY_MODEL_ENV = "TWOXBRAINZ_FIXTURE_SUMMARY_MODEL"
_DEFAULT_USER_VOICE = "af_heart"
_DEFAULT_REMOTE_VOICE = "am_michael"
_DEFAULT_REPEATS = 3
_MAX_REPEATS = 5
_PAIR_COUNT = 2
_PAIR_TIMEOUT_SECONDS = 180
_PROVIDER_PHASE_TIMEOUT_SECONDS = 60
_PROVIDER_IDLE_TIMEOUT_SECONDS = 180
_SESSION_ID = "real-conversation-evaluation"


class ConversationEvaluationError(RuntimeError):
    """The real generated conversation failed an evaluation contract."""


class _PairBarrier:
    def __init__(self) -> None:
        self._arrived = 0
        self._release = asyncio.Event()

    async def wait(self) -> None:
        self._arrived += 1
        if self._arrived == _PAIR_COUNT:
            self._release.set()
        await self._release.wait()


class _ObservationRecorder:
    def __init__(self, trace: FixtureTrace) -> None:
        self._trace = trace
        self._started_ns = time.monotonic_ns()
        self._phase_events: dict[str, asyncio.Event] = {}
        self.records: list[TimedRecord] = []

    def record(self, record: Mapping[str, object]) -> None:
        retained = dict(record)
        self.records.append(
            TimedRecord(
                sequence=len(self.records) + 1,
                elapsed_ms=(time.monotonic_ns() - self._started_ns) // 1_000_000,
                record=retained,
            )
        )
        if retained.get("kind") == "provider_activity":
            self._trace.event(
                "provider_activity",
                **{key: value for key, value in retained.items() if key != "kind"},
            )
        else:
            self._trace.event("evaluation_observation", record=retained)
        phase = retained.get("phase")
        if isinstance(phase, str):
            self._phase_events.setdefault(phase, asyncio.Event()).set()

    def activity_sink(self) -> Callable[[Mapping[str, object]], None]:
        return lambda activity: self.record({"kind": "provider_activity", **activity})

    def phase_count(self, phase: str) -> int:
        return sum(record.record.get("phase") == phase for record in self.records)

    async def wait_for_new_phase(self, phase: str, previous_count: int) -> None:
        async with asyncio.timeout(_PROVIDER_PHASE_TIMEOUT_SECONDS):
            while self.phase_count(phase) <= previous_count:
                event = self._phase_events.setdefault(phase, asyncio.Event())
                event.clear()
                if self.phase_count(phase) > previous_count:
                    return
                await event.wait()


def main() -> int:
    try:
        result = asyncio.run(_run())
    except (
        ConversationEvaluationError,
        FixtureTraceError,
        TimeoutError,
        TwoXBrainzError,
    ) as error:
        print(
            f"error: real conversation evaluation failed: {type(error).__name__}: "
            f"{error}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


async def _run() -> dict[str, object]:
    settings = _settings_with_fixture_models(Settings.from_environment())
    scenario = load_scenario(_scenario_path())
    repeat_count = _repeat_count()
    suite_directory = _create_run_directory(
        _trace_directory(),
        f"{scenario.slug}-suite",
    )
    attempts: list[dict[str, object]] = []
    for attempt_number in range(1, repeat_count + 1):
        run_directory = _create_attempt_directory(suite_directory, attempt_number)
        trace = FixtureTrace(
            run_directory,
            "events",
            secret_values=(
                settings.aigate_token or "",
                settings.talkies_token or "",
            ),
        )
        try:
            attempt = await _run_with_trace(
                settings,
                scenario,
                run_directory,
                trace,
            )
        except (
            ConversationEvaluationError,
            TimeoutError,
            TwoXBrainzError,
        ) as error:
            trace.failure(error)
            raise
        finally:
            trace.close()
        attempts.append({**attempt, "attempt": attempt_number})
    aggregate = _aggregate_attempt_results(attempts)
    aggregate_path = _write_aggregate_artifact(
        suite_directory,
        scenario.slug,
        attempts,
        aggregate,
    )
    return {
        "kind": "real_conversation_evaluation_suite",
        "result": "passed",
        "scenario": scenario.slug,
        "suite_directory": str(suite_directory),
        "attempt_count": repeat_count,
        "attempts": attempts,
        "aggregate": aggregate,
        "aggregate_file": str(aggregate_path),
    }


def _settings_with_fixture_models(settings: Settings) -> Settings:
    return replace(
        settings,
        talkies_model=_fixture_model(_TALKIES_MODEL_ENV, settings.talkies_model),
        aigate_reply_model=_fixture_model(
            _DRAFT_MODEL_ENV,
            settings.aigate_reply_model,
        ),
        aigate_coach_model=_fixture_model(
            _COMMENTARY_MODEL_ENV,
            settings.aigate_coach_model,
        ),
        aigate_summary_model=_fixture_model(
            _SUMMARY_MODEL_ENV,
            settings.aigate_summary_model,
        ),
    )


def _fixture_model(name: str, default: str) -> str:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    value = raw_value.strip()
    if not value or len(value) > MAX_AIGATE_MODEL_ID_CHARACTERS:
        raise ConversationEvaluationError(f"{name} is invalid")
    return value


async def _run_with_trace(
    settings: Settings,
    scenario: ConversationScenario,
    run_directory: Path,
    trace: FixtureTrace,
) -> dict[str, object]:
    if settings.aigate_token is None or settings.talkies_token is None:
        raise ConversationEvaluationError("AIGate token is required")
    work_directory = Path(os.environ.get(_WORK_DIRECTORY_ENV, "/tmp"))
    if not work_directory.is_dir():
        raise ConversationEvaluationError("evaluation work directory is unavailable")
    trace.event(
        "evaluation_started",
        scenario=scenario.slug,
        turn_count=len(scenario.turns),
        talkies_model=settings.talkies_model,
        reply_model=settings.aigate_reply_model,
        coach_model=settings.aigate_coach_model,
        summary_model=settings.aigate_summary_model,
    )
    with tempfile.TemporaryDirectory(
        prefix="2xbrainz-evaluation-",
        dir=work_directory,
    ) as temporary_name:
        audio_paths = await _synthesize_turns(
            settings,
            scenario,
            Path(temporary_name),
            trace,
        )
        recognized = await _transcribe_turns(
            settings,
            scenario,
            audio_paths,
            trace,
        )
    word_error_rates = scenario_word_error_rates(scenario, recognized)
    _write_transcript_artifact(run_directory, scenario, recognized, word_error_rates)
    recorder = _ObservationRecorder(trace)
    report = await _evaluate_provider_flows(
        settings,
        scenario,
        recognized,
        recorder,
        trace,
    )
    research_completed = _research_completed(recorder.records)
    report = apply_live_quality_gates(
        report,
        scenario,
        word_error_rates,
        research_tool_completed=research_completed,
    )
    json_path, markdown_path = write_report(
        run_directory,
        report,
        word_error_rates=word_error_rates,
    )
    _assert_research_completed(recorder.records)
    trace.event(
        "evaluation_completed",
        passed=report.passed,
        mean_word_error_rate=sum(word_error_rates.values()) / len(word_error_rates),
        maximum_concurrent_provider_flows=(report.maximum_concurrent_provider_flows),
        scorecard=str(json_path),
    )
    if not report.passed:
        raise ConversationEvaluationError("conversation quality gates failed")
    return {
        "kind": "real_conversation_evaluation",
        "result": "passed",
        "run_directory": str(run_directory),
        "trace_file": str(trace.path),
        "scorecard": str(json_path),
        "report": str(markdown_path),
        "maximum_concurrent_provider_flows": (report.maximum_concurrent_provider_flows),
        "mean_word_error_rate": sum(word_error_rates.values()) / len(word_error_rates),
    }


async def _synthesize_turns(
    settings: Settings,
    scenario: ConversationScenario,
    directory: Path,
    trace: FixtureTrace,
) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for turn in scenario.turns:
        path = directory / f"{turn.identifier}.wav"
        voice = _voice_for_turn(turn)
        with _temporary_environment(TTS_VOICE_ENV, voice):
            await asyncio.to_thread(
                synthesize_wav,
                settings,
                turn.text,
                path,
                SpeakerRole(turn.speaker_role),
                trace,
            )
        fixture = load_wav_fixture(path)
        trace.event(
            "evaluation_audio_generated",
            turn_id=turn.identifier,
            speaker_role=turn.speaker_role,
            voice=voice,
            wav_bytes=path.stat().st_size,
            audio_seconds=fixture.duration_seconds,
        )
        paths[turn.identifier] = path
    return paths


async def _transcribe_turns(
    settings: Settings,
    scenario: ConversationScenario,
    paths: dict[str, Path],
    trace: FixtureTrace,
) -> dict[str, str]:
    from two_x_brainz.talkies import TalkiesClient, TalkiesStreamConfig

    client = TalkiesClient(
        TalkiesStreamConfig(
            url=settings.talkies_ws_url,
            model=settings.talkies_model,
            token=settings.talkies_token,
        )
    )
    device = await client.verify_configured_model()
    max_concurrency = await client.configured_model_max_concurrency()
    trace.event(
        "evaluation_asr_concurrency_verified",
        model=settings.talkies_model,
        device=device,
        max_concurrency=max_concurrency,
    )
    if max_concurrency < _PAIR_COUNT:
        raise ConversationEvaluationError(
            "Talkies model does not advertise two concurrent requests"
        )
    recognized: dict[str, str] = {}
    for offset in range(0, len(scenario.turns), _PAIR_COUNT):
        pair = scenario.turns[offset : offset + _PAIR_COUNT]
        if len(pair) != _PAIR_COUNT:
            raise ConversationEvaluationError("scenario must contain complete pairs")
        barrier = _PairBarrier()
        async with asyncio.timeout(_PAIR_TIMEOUT_SECONDS):
            results = await asyncio.gather(
                *(
                    _transcribe_turn(
                        client,
                        turn,
                        load_wav_fixture(paths[turn.identifier]),
                        barrier,
                        trace,
                    )
                    for turn in pair
                )
            )
        for result in results:
            recognized.update(result)
    return recognized


async def _transcribe_turn(
    client: object,
    turn: ScenarioTurn,
    fixture: WavFixture,
    barrier: _PairBarrier,
    trace: FixtureTrace,
) -> dict[str, str]:
    from two_x_brainz.talkies import TalkiesClient

    if not isinstance(client, TalkiesClient):
        raise ConversationEvaluationError("invalid Talkies client")
    trace.event(
        "evaluation_asr_started",
        turn_id=turn.identifier,
        speaker_role=turn.speaker_role,
        audio_seconds=fixture.duration_seconds,
    )
    events = tuple(
        [
            event
            async for event in client.transcribe(
                session_id=_SESSION_ID,
                stream_id=turn.identifier,
                speaker_role=SpeakerRole(turn.speaker_role),
                frames=_audio_frames(turn, fixture, barrier),
            )
        ]
    )
    final_events = tuple(
        event
        for event in events
        if isinstance(event, TranscriptEvent)
        and event.source_event_type is TranscriptEventType.FINAL
        and event.text.strip()
    )
    statistics = tuple(event for event in events if isinstance(event, ASRStreamStats))
    if not final_events:
        raise ProtocolError(f"Talkies emitted no final text for {turn.identifier}")
    if len(statistics) != 1 or statistics[0].canceled:
        raise ProtocolError(f"Talkies emitted invalid stats for {turn.identifier}")
    recognized = final_events[-1].text.strip()
    trace.event(
        "evaluation_asr_completed",
        turn_id=turn.identifier,
        speaker_role=turn.speaker_role,
        recognized_text=recognized,
        frames=statistics[0].frames,
        audio_seconds=statistics[0].audio_seconds,
    )
    return {turn.identifier: recognized}


async def _audio_frames(
    turn: ScenarioTurn,
    fixture: WavFixture,
    barrier: _PairBarrier,
) -> AsyncIterator[AudioFrame]:
    await barrier.wait()
    frame_count = (len(fixture.pcm16le) + DEFAULT_FRAME_BYTES - 1) // (
        DEFAULT_FRAME_BYTES
    )
    pcm = fixture.pcm16le.ljust(frame_count * DEFAULT_FRAME_BYTES, b"\x00")
    for sequence in range(frame_count):
        offset = sequence * DEFAULT_FRAME_BYTES
        yield AudioFrame(
            session_id=_SESSION_ID,
            stream_id=turn.identifier,
            speaker_role=SpeakerRole(turn.speaker_role),
            sequence=sequence,
            captured_at_monotonic=time.monotonic(),
            sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
            channels=DEFAULT_CHANNELS,
            samples=pcm[offset : offset + DEFAULT_FRAME_BYTES],
        )
        await asyncio.sleep(DEFAULT_FRAME_DURATION_MS / 1_000)


async def _evaluate_provider_flows(
    settings: Settings,
    scenario: ConversationScenario,
    recognized: dict[str, str],
    recorder: _ObservationRecorder,
    trace: FixtureTrace,
) -> EvaluationReport:
    providers = {
        "draft": AIGateClient(
            base_url=settings.aigate_url,
            model=settings.aigate_reply_model,
            token=settings.aigate_token,
            web_research_enabled=True,
            reasoning_effort=settings.aigate_reply_reasoning_effort,
            streaming_enabled=True,
            activity_sink=recorder.activity_sink(),
        ),
        "commentary": AIGateClient(
            base_url=settings.aigate_url,
            model=settings.aigate_coach_model,
            token=settings.aigate_token,
            reasoning_effort=settings.aigate_coach_reasoning_effort,
            streaming_enabled=True,
            activity_sink=recorder.activity_sink(),
        ),
        "summary": AIGateClient(
            base_url=settings.aigate_url,
            model=settings.aigate_summary_model,
            token=settings.aigate_token,
            reasoning_effort=settings.aigate_summary_reasoning_effort,
            streaming_enabled=True,
            activity_sink=recorder.activity_sink(),
        ),
    }
    await asyncio.gather(
        *(provider.verify_configured_model() for provider in providers.values())
    )
    coordinator = ConversationCoordinator(
        providers["draft"],
        commentary_provider=providers["commentary"],
        summary_provider=providers["summary"],
    )
    seen_generations: set[str] = set()
    try:
        role_revisions = {SpeakerRole.USER: 0, SpeakerRole.REMOTE: 0}
        interruption_pending = False
        for turn in scenario.turns:
            role = SpeakerRole(turn.speaker_role)
            role_revisions[role] += 1
            previous_phase_count = (
                recorder.phase_count(turn.interrupt_after_phase)
                if turn.interrupt_after_phase is not None
                else 0
            )
            if interruption_pending:
                recorder.record(
                    {
                        "kind": "evaluation_interruption",
                        "turn_id": turn.identifier,
                        "context_revision": (
                            coordinator.transcript_snapshot().revision + 1
                        ),
                    }
                )
                interruption_pending = False
            update = await coordinator.ingest(
                _transcript_event(
                    turn,
                    recognized[turn.identifier],
                    role_revisions[role],
                    settings.talkies_model,
                )
            )
            if update.timeline is not None:
                recorder.record(
                    {
                        "kind": "timeline",
                        "turn_id": update.timeline.turn_id,
                        "speaker_role": update.timeline.speaker_role.value,
                        "text": update.timeline.text,
                    }
                )
            if turn.interrupt_after_phase is not None:
                await recorder.wait_for_new_phase(
                    turn.interrupt_after_phase,
                    previous_phase_count,
                )
                interruption_pending = True
                continue
            async with asyncio.timeout(_PROVIDER_IDLE_TIMEOUT_SECONDS):
                draft = await coordinator.wait_for_idle()
            _record_results(
                recorder,
                draft,
                coordinator.drain_completed_insights(),
                seen_generations,
            )
        async with asyncio.timeout(_PROVIDER_IDLE_TIMEOUT_SECONDS):
            draft = await coordinator.wait_for_idle()
        _record_results(
            recorder,
            draft,
            coordinator.drain_completed_insights(),
            seen_generations,
        )
    finally:
        await coordinator.stop()
    report = evaluate_observations(tuple(recorder.records), scenario)
    trace.event(
        "provider_evaluation_scored",
        passed=report.passed,
        flow_count=report.provider_flow_count,
        maximum_concurrent_flows=report.maximum_concurrent_provider_flows,
        overlapping_pairs=report.overlapping_provider_pairs,
    )
    return report


def _record_results(
    recorder: _ObservationRecorder,
    draft: DraftResult | None,
    insights: tuple[InsightResult, ...],
    seen_generations: set[str],
) -> None:
    if draft is not None and draft.generation_id not in seen_generations:
        seen_generations.add(draft.generation_id)
        recorder.record(
            {
                "kind": "draft",
                "generation_id": draft.generation_id,
                "status": draft.status.value,
                "text": draft.text,
                "context_revision": draft.context_revision,
            }
        )
    for insight in insights:
        if insight.generation_id in seen_generations:
            continue
        seen_generations.add(insight.generation_id)
        recorder.record(
            {
                "kind": insight.kind.value,
                "generation_id": insight.generation_id,
                "status": insight.status.value,
                "text": insight.text,
                "context_revision": insight.context_revision,
            }
        )


def _transcript_event(
    turn: ScenarioTurn,
    text: str,
    revision: int,
    asr_model: str,
) -> TranscriptEvent:
    role = SpeakerRole(turn.speaker_role)
    return TranscriptEvent(
        session_id=_SESSION_ID,
        stream_id=turn.identifier,
        utterance_id=turn.identifier,
        revision=revision,
        speaker_role=role,
        source_event_type=TranscriptEventType.FINAL,
        asr_model=asr_model,
        text=text,
        is_final=True,
        audio_seconds=1.0,
        words=(),
    )


def _assert_research_completed(records: list[TimedRecord]) -> None:
    if _research_completed(records):
        return
    raise ConversationEvaluationError("reply flow did not complete web research")


def _research_completed(records: list[TimedRecord]) -> bool:
    return any(
        record.record.get("phase") == "tool_completed"
        and record.record.get("output_kind") == "draft"
        and record.record.get("tool") in {"research_web", "fetch_url"}
        for record in records
    )


def _write_transcript_artifact(
    directory: Path,
    scenario: ConversationScenario,
    recognized: dict[str, str],
    word_error_rates: dict[str, float],
) -> None:
    payload = {
        "scenario": scenario.slug,
        "turns": [
            {
                "id": turn.identifier,
                "speaker_role": turn.speaker_role,
                "reference": turn.text,
                "recognized": recognized[turn.identifier],
                "word_error_rate": word_error_rates[turn.identifier],
            }
            for turn in scenario.turns
        ],
    }
    try:
        with (directory / "transcripts.json").open("x", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True)
            file.write("\n")
    except OSError as error:
        raise ConversationEvaluationError("write transcript artifact") from error


def _voice_for_turn(turn: ScenarioTurn) -> str:
    name = _USER_VOICE_ENV if turn.speaker_role == "user" else _REMOTE_VOICE_ENV
    default = (
        _DEFAULT_USER_VOICE if turn.speaker_role == "user" else _DEFAULT_REMOTE_VOICE
    )
    voice = os.environ.get(name, default).strip()
    if not voice or len(voice) > 80:
        raise ConversationEvaluationError(f"{name} is invalid")
    return voice


@contextmanager
def _temporary_environment(name: str, value: str) -> Iterator[None]:
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def _scenario_path() -> Path:
    value = os.environ.get(_SCENARIO_ENV, "").strip()
    if not value:
        raise ConversationEvaluationError("evaluation scenario path is required")
    return Path(value)


def _trace_directory() -> Path:
    value = os.environ.get(_TRACE_DIRECTORY_ENV, "").strip()
    if not value:
        raise ConversationEvaluationError("evaluation trace directory is required")
    directory = Path(value)
    if not directory.is_dir():
        raise ConversationEvaluationError("evaluation trace directory is unavailable")
    return directory


def _repeat_count() -> int:
    raw_value = os.environ.get(_REPEATS_ENV, str(_DEFAULT_REPEATS)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ConversationEvaluationError(
            f"{_REPEATS_ENV} must be an integer"
        ) from error
    if not 1 <= value <= _MAX_REPEATS:
        raise ConversationEvaluationError(
            f"{_REPEATS_ENV} must be between 1 and {_MAX_REPEATS}"
        )
    return value


def _aggregate_attempt_results(
    attempts: list[dict[str, object]],
) -> dict[str, object]:
    if not attempts:
        raise ConversationEvaluationError("evaluation suite has no attempts")
    concurrencies = [
        _nonnegative_integer(
            attempt.get("maximum_concurrent_provider_flows"),
            "maximum concurrent provider flows",
        )
        for attempt in attempts
    ]
    error_rates = [
        _unit_interval_number(
            attempt.get("mean_word_error_rate"),
            "mean word error rate",
        )
        for attempt in attempts
    ]
    return {
        "minimum_maximum_concurrent_provider_flows": min(concurrencies),
        "maximum_maximum_concurrent_provider_flows": max(concurrencies),
        "mean_word_error_rate": sum(error_rates) / len(error_rates),
        "maximum_mean_word_error_rate": max(error_rates),
    }


def _nonnegative_integer(value: object, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConversationEvaluationError(f"{description} is invalid")
    return value


def _unit_interval_number(value: object, description: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ConversationEvaluationError(f"{description} is invalid")
    number = float(value)
    if not 0 <= number <= 1:
        raise ConversationEvaluationError(f"{description} is invalid")
    return number


def _write_aggregate_artifact(
    directory: Path,
    scenario: str,
    attempts: list[dict[str, object]],
    aggregate: dict[str, object],
) -> Path:
    path = directory / "aggregate.json"
    payload = {
        "scenario": scenario,
        "attempt_count": len(attempts),
        "attempts": attempts,
        "aggregate": aggregate,
    }
    try:
        with path.open("x", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True)
            file.write("\n")
    except OSError as error:
        raise ConversationEvaluationError(
            "write evaluation aggregate artifact"
        ) from error
    return path


def _create_run_directory(parent: Path, slug: str) -> Path:
    directory = parent / f"{slug}-{time.time_ns()}"
    try:
        directory.mkdir(mode=0o700)
    except OSError as error:
        raise ConversationEvaluationError("create evaluation run directory") from error
    return directory


def _create_attempt_directory(parent: Path, attempt_number: int) -> Path:
    directory = parent / f"attempt-{attempt_number:02d}"
    try:
        directory.mkdir(mode=0o700)
    except OSError as error:
        raise ConversationEvaluationError(
            "create evaluation attempt directory"
        ) from error
    return directory


if __name__ == "__main__":
    raise SystemExit(main())
