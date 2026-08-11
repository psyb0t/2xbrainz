"""Real audio-to-Claudebox interruption and workspace-research proof."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from live_talkies_tts_fixture import synthesize_wav
from real_aigate_prompts import (
    _find_research_checkout,  # pyright: ignore[reportPrivateUsage]
)

from two_x_brainz.audio import WavFixture, load_wav_fixture
from two_x_brainz.audio_selection import AudioSelection
from two_x_brainz.capture import CaptureFrameMonitor
from two_x_brainz.claudebox import ClaudeboxReplyClient
from two_x_brainz.config import Settings
from two_x_brainz.constants import (
    DEFAULT_CLAUDEBOX_REPLACEMENT_DEADLINE,
    DEFAULT_FRAME_BYTES,
    VAD_SILENCE_WINDOW_COUNT,
)
from two_x_brainz.contracts import (
    DraftRequest,
    DraftResult,
    GenerationStatus,
    SpeakerRole,
)
from two_x_brainz.coordinator import ConversationCoordinator
from two_x_brainz.fixture_trace import FixtureTrace, FixtureTraceError
from two_x_brainz.runtime import (
    _ACTIVE_TERMINAL,  # pyright: ignore[reportPrivateUsage]
    _consume_stream,  # pyright: ignore[reportPrivateUsage]
)
from two_x_brainz.talkies import TalkiesClient, TalkiesStreamConfig

_TRACE_DIRECTORY_ENV = "TWOXBRAINZ_FIXTURE_TRACE_DIR"
_WORK_DIRECTORY_ENV = "TWOXBRAINZ_FIXTURE_WORK_DIR"
_RESEARCH_MODEL_ENV = "TWOXBRAINZ_FIXTURE_RESEARCH_MODEL"
_TALKIES_MODEL_ENV = "TWOXBRAINZ_FIXTURE_TALKIES_MODEL"
_TRACE_LABEL = "audio-interrupted-claudebox-research"
_FIRST_REMOTE_TEXT = (
    "Research the GitHub repository called AI Gate and tell me what problem it solves."
)
_SECOND_REMOTE_TEXT = (
    "The exact link is GitHub dot com slash P S Y B zero T slash AI Gate. "
    "Check its actual files and main capabilities."
)
_FIRST_TRANSCRIPT_MARKERS = (
    ("github", "git hub"),
    ("repository", "repo"),
    ("ai gate", "aigate", "i gate"),
)
_SECOND_TRANSCRIPT_MARKERS = (
    ("actual files", "files"),
    ("capabilities", "features"),
    ("github", "git hub"),
)
_REPLY_MARKERS = ("aigate", "gateway", "provider", "model", "api")
_SILENCE_FRAME_COUNT = VAD_SILENCE_WINDOW_COUNT + 20
_RESEARCH_START_TIMEOUT_SECONDS = 55
_FIXTURE_TIMEOUT_SECONDS = 280


class InterruptedAudioResearchError(RuntimeError):
    """The real interrupted-research fixture did not meet its contract."""


class _ActivityRecorder:
    def __init__(self, trace: FixtureTrace) -> None:
        self.activities: list[dict[str, object]] = []
        self.research_started = asyncio.Event()
        self._trace = trace

    def record(self, activity: Mapping[str, object]) -> None:
        copied = dict(activity)
        self.activities.append(copied)
        self._trace.event("provider_activity", **copied)
        if copied.get("phase") == "native_research_started":
            self.research_started.set()


class _TracingDraftProvider:
    def __init__(
        self,
        client: ClaudeboxReplyClient,
        trace: FixtureTrace,
    ) -> None:
        self._client = client
        self._trace = trace
        self.requests: list[DraftRequest] = []

    async def start_session(self) -> str:
        return await self._client.start_session()

    async def draft(self, request: DraftRequest) -> DraftResult:
        self.requests.append(request)
        self._trace.event(
            "draft_request",
            generation_id=request.generation_id,
            context_revision=request.context_revision,
            transcript_lines=tuple(
                {
                    "speaker_role": line.speaker_role.value,
                    "text": line.text,
                    "is_final": line.is_final,
                }
                for line in request.transcript.lines
            ),
        )
        return await self._client.draft(request)


class _RecordingPresentation:
    def __init__(self, trace: FixtureTrace) -> None:
        self.records: list[dict[str, object]] = []
        self._trace = trace

    @property
    def interactive(self) -> bool:
        return False

    async def open(self) -> AudioSelection | None:
        return None

    async def close(self) -> None:
        return None

    def consume(self, record: dict[str, object]) -> None:
        copied = dict(record)
        self.records.append(copied)
        self._trace.event("runtime_record", record=copied)

    def set_audio_level(self, speaker_role: str, percent: int) -> None:
        del speaker_role, percent

    async def control_lines(self) -> AsyncIterator[str]:
        if False:
            yield ""


def main() -> int:
    try:
        trace_path = asyncio.run(asyncio.wait_for(_run(), _FIXTURE_TIMEOUT_SECONDS))
    except (FixtureTraceError, InterruptedAudioResearchError, TimeoutError) as error:
        print(f"error: interrupted audio research failed: {error}", file=sys.stderr)
        return 1
    print(
        '{"kind":"audio_interrupted_claudebox_research","result":"passed",'
        f'"trace_file":"{trace_path}"}}'
    )
    return 0


async def _run() -> Path:
    settings = _fixture_settings(Settings.from_environment())
    if settings.aigate_token is None:
        raise InterruptedAudioResearchError("AIGate token is required")
    trace = FixtureTrace(
        _required_directory(_TRACE_DIRECTORY_ENV),
        _TRACE_LABEL,
        secret_values=(settings.aigate_token,),
    )
    try:
        trace_path = await _run_with_trace(settings, trace)
        trace.event("fixture_passed")
        return trace_path
    except Exception as error:
        trace.failure(error)
        raise
    finally:
        trace.close()


async def _run_with_trace(settings: Settings, trace: FixtureTrace) -> Path:
    work_directory = _required_directory(_WORK_DIRECTORY_ENV)
    first_path = work_directory / "first-remote.wav"
    second_path = work_directory / "second-remote.wav"
    await asyncio.gather(
        asyncio.to_thread(
            synthesize_wav,
            settings,
            _FIRST_REMOTE_TEXT,
            first_path,
            SpeakerRole.REMOTE,
            trace,
        ),
        asyncio.to_thread(
            synthesize_wav,
            settings,
            _SECOND_REMOTE_TEXT,
            second_path,
            SpeakerRole.REMOTE,
            trace,
        ),
    )
    first_audio, second_audio = await asyncio.gather(
        asyncio.to_thread(load_wav_fixture, first_path),
        asyncio.to_thread(load_wav_fixture, second_path),
    )
    recorder = _ActivityRecorder(trace)
    client = ClaudeboxReplyClient(
        base_url=settings.aigate_url,
        model=settings.aigate_research_model,
        token=settings.aigate_token,
        reasoning_effort=settings.aigate_research_reasoning_effort,
        activity_sink=recorder.record,
        output_kind="research",
    )
    provider = _TracingDraftProvider(client, trace)
    coordinator = ConversationCoordinator(
        provider,
        research_provider=provider,
        draft_generation_deadline=DEFAULT_CLAUDEBOX_REPLACEMENT_DEADLINE,
        research_enabled=False,
    )
    workspace_session_id = await coordinator.start_session()
    if workspace_session_id is None:
        raise InterruptedAudioResearchError("Claudebox workspace was not created")
    trace.event(
        "claudebox_workspace_started",
        workspace_session_id=workspace_session_id,
    )
    session_id = str(uuid4())
    capture_monitor = CaptureFrameMonitor(
        session_id=session_id,
        stream_id="fixture-remote",
        speaker_role=SpeakerRole.REMOTE,
    )
    presentation = _RecordingPresentation(trace)
    terminal_token = _ACTIVE_TERMINAL.set(presentation)
    try:
        client_config = TalkiesStreamConfig(
            url=settings.talkies_ws_url,
            model=settings.talkies_model,
            token=settings.talkies_token,
        )
        await _consume_stream(
            client=TalkiesClient(client_config),
            coordinator=coordinator,
            session_id=session_id,
            stream_id="fixture-remote",
            speaker_role=SpeakerRole.REMOTE,
            frames=capture_monitor.annotate(
                _interrupted_audio_frames(first_audio, second_audio, recorder, trace)
            ),
            capture_monitor=capture_monitor,
        )
        final_draft = await coordinator.wait_for_idle()
    finally:
        _ACTIVE_TERMINAL.reset(terminal_token)
        await coordinator.stop()

    checkout = await asyncio.to_thread(
        _find_research_checkout,
        settings,
        workspace_session_id,
    )
    _assert_contract(
        activities=recorder.activities,
        requests=provider.requests,
        records=presentation.records,
        final_draft=final_draft,
        workspace_session_id=workspace_session_id,
    )
    trace.event(
        "contract_verified",
        workspace_session_id=workspace_session_id,
        checkout=checkout,
        final_reply=final_draft.text if final_draft is not None else "",
        request_count=len(provider.requests),
    )
    return trace.path


async def _interrupted_audio_frames(
    first_audio: WavFixture,
    second_audio: WavFixture,
    recorder: _ActivityRecorder,
    trace: FixtureTrace,
) -> AsyncIterator[bytes]:
    for frame in _pcm_frames(first_audio.pcm16le):
        yield frame
        await asyncio.sleep(0.02)
    for _ in range(_SILENCE_FRAME_COUNT):
        yield bytes(DEFAULT_FRAME_BYTES)
        await asyncio.sleep(0.02)
    trace.event("waiting_for_first_research")
    try:
        await asyncio.wait_for(
            recorder.research_started.wait(),
            timeout=_RESEARCH_START_TIMEOUT_SECONDS,
        )
    except TimeoutError as error:
        raise InterruptedAudioResearchError(
            "first audio turn did not start native repository research"
        ) from error
    trace.event("second_audio_released")
    for frame in _pcm_frames(second_audio.pcm16le):
        yield frame
        await asyncio.sleep(0.02)
    for _ in range(_SILENCE_FRAME_COUNT):
        yield bytes(DEFAULT_FRAME_BYTES)
        await asyncio.sleep(0.02)


def _pcm_frames(pcm: bytes) -> tuple[bytes, ...]:
    complete_bytes = len(pcm) - len(pcm) % DEFAULT_FRAME_BYTES
    return tuple(
        pcm[offset : offset + DEFAULT_FRAME_BYTES]
        for offset in range(0, complete_bytes, DEFAULT_FRAME_BYTES)
    )


def _assert_contract(
    *,
    activities: list[dict[str, object]],
    requests: list[DraftRequest],
    records: list[dict[str, object]],
    final_draft: DraftResult | None,
    workspace_session_id: str,
) -> None:
    _assert_activity_lifecycle(activities, workspace_session_id)
    if len(requests) < 2:
        raise InterruptedAudioResearchError(
            "interruption did not produce replacement Claudebox research"
        )
    first_text = _remote_text(requests[0])
    final_text = _remote_text(requests[-1])
    _assert_marker_groups(first_text, _FIRST_TRANSCRIPT_MARKERS, "first transcript")
    _assert_marker_groups(final_text, _FIRST_TRANSCRIPT_MARKERS, "final transcript")
    _assert_marker_groups(final_text, _SECOND_TRANSCRIPT_MARKERS, "final transcript")
    if not any(
        record.get("kind") == "transcript"
        and record.get("speaker_role") == SpeakerRole.REMOTE.value
        and record.get("type") == "partial"
        for record in records
    ):
        raise InterruptedAudioResearchError(
            "real Talkies emitted no partial transcript"
        )
    if final_draft is None or final_draft.status is not GenerationStatus.COMPLETED:
        raise InterruptedAudioResearchError("replacement reply did not complete")
    normalized_reply = final_draft.text.casefold()
    if sum(marker in normalized_reply for marker in _REPLY_MARKERS) < 2:
        raise InterruptedAudioResearchError(
            "replacement reply lacks grounded AIGate capability markers"
        )


def _assert_activity_lifecycle(
    activities: list[dict[str, object]],
    workspace_session_id: str,
) -> None:
    phases = [activity.get("phase") for activity in activities]
    required = (
        "request_started",
        "native_research_started",
        "request_cancelled",
        "request_started",
        "native_research_started",
        "native_research_completed",
        "request_completed",
    )
    position = 0
    for phase in phases:
        if position < len(required) and phase == required[position]:
            position += 1
    if position != len(required):
        raise InterruptedAudioResearchError(
            f"provider lifecycle is incomplete: {phases}"
        )
    workspace_ids = {
        activity.get("workspace_session_id")
        for activity in activities
        if activity.get("workspace_session_id") is not None
    }
    if workspace_ids != {workspace_session_id}:
        raise InterruptedAudioResearchError(
            "provider generations did not share one Claudebox workspace"
        )
    cancelled_generations = {
        activity.get("generation_id")
        for activity in activities
        if activity.get("phase") == "request_cancelled"
    }
    completed_generations = {
        activity.get("generation_id")
        for activity in activities
        if activity.get("phase") == "request_completed"
    }
    if cancelled_generations & completed_generations:
        raise InterruptedAudioResearchError(
            "the cancelled generation was incorrectly published as completed"
        )


def _remote_text(request: DraftRequest) -> str:
    return "\n".join(
        line.text
        for line in request.transcript.lines
        if line.speaker_role is SpeakerRole.REMOTE and line.text.strip()
    ).casefold()


def _assert_marker_groups(
    text: str,
    groups: tuple[tuple[str, ...], ...],
    label: str,
) -> None:
    missing = [group for group in groups if not any(marker in text for marker in group)]
    if missing:
        raise InterruptedAudioResearchError(f"{label} is missing markers: {missing}")


def _fixture_settings(settings: Settings) -> Settings:
    research_model = os.environ.get(
        _RESEARCH_MODEL_ENV,
        settings.aigate_research_model,
    ).strip()
    talkies_model = os.environ.get(
        _TALKIES_MODEL_ENV,
        settings.talkies_model,
    ).strip()
    if not research_model or not talkies_model:
        raise InterruptedAudioResearchError("fixture models must not be empty")
    return replace(
        settings,
        aigate_research_model=research_model,
        aigate_research_reasoning_effort="high",
        talkies_model=talkies_model,
    )


def _required_directory(name: str) -> Path:
    value = os.environ.get(name, "").strip()
    if not value:
        raise InterruptedAudioResearchError(f"{name} is required")
    path = Path(value)
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise InterruptedAudioResearchError(f"{name} must be a real directory")
    return path


if __name__ == "__main__":
    raise SystemExit(main())
