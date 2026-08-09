"""Opt-in synthetic-text contract check for the configured real AIGate model."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from two_x_brainz.aigate import AIGateClient
from two_x_brainz.claudebox import ClaudeboxReplyClient
from two_x_brainz.config import Settings
from two_x_brainz.constants import (
    BEARER_PREFIX,
    CLAUDEBOX_REPLY_MODELS,
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_PROVIDER_GENERATION_DEADLINE,
    HEADER_AUTHORIZATION,
    MAX_PROVIDER_RESPONSE_BYTES,
)
from two_x_brainz.contracts import (
    DraftRequest,
    DraftResult,
    GenerationStatus,
    InsightKind,
    InsightRequest,
    InsightResult,
    SpeakerRole,
    TranscriptEvent,
    TranscriptEventType,
    TranscriptLine,
    TranscriptSnapshot,
)
from two_x_brainz.coordinator import ConversationCoordinator
from two_x_brainz.errors import ConfigurationError, ProtocolError, RemoteServiceError
from two_x_brainz.fixture_trace import FixtureTrace, FixtureTraceError
from two_x_brainz.json_support import (
    decode_json,
    require_json_array,
    require_json_object,
)

_DRAFT_GENERATION_ID = "synthetic-draft-generation"
_COMMENTARY_GENERATION_ID = "synthetic-commentary-generation"
_SUMMARY_GENERATION_ID = "synthetic-summary-generation"
_REMOTE_TURN_ID = "synthetic-remote-turn"
_USER_TURN_ID = "synthetic-user-turn"
_TRANSCRIPT_REVISION = 2
_TRACE_DIRECTORY_ENV = "TWOXBRAINZ_FIXTURE_TRACE_DIR"
_DRAFT_MODEL_ENV = "TWOXBRAINZ_FIXTURE_DRAFT_MODEL"
_COMMENTARY_MODEL_ENV = "TWOXBRAINZ_FIXTURE_COMMENTARY_MODEL"
_SUMMARY_MODEL_ENV = "TWOXBRAINZ_FIXTURE_SUMMARY_MODEL"
_TRACE_LABEL = "real-aigate-interview"
_SYNTHETIC_SESSION_ID = "synthetic-interview-session"
_USER_STREAM_ID = "synthetic-user-stream"
_REMOTE_STREAM_ID = "synthetic-remote-stream"
_INITIAL_EVENT_REVISION = 1
_FOLLOWUP_PARTIAL_REVISION = 2
_FOLLOWUP_FINAL_REVISION = 3
_INTERVIEW_USER_TEXT = (
    "I will lead the Orchid migration from the notification worker to the queue. "
    "I will complete a Tuesday rehearsal. The unresolved risk is duplicate deliveries."
)
_INTERVIEW_REMOTE_TEXT = (
    "How will you prevent duplicate deliveries before the Tuesday rehearsal?"
)
_INTERVIEW_USER_MITIGATION_TEXT = (
    "I will add an idempotency key at the consumer boundary and test duplicate "
    "delivery recovery in staging before the Tuesday rehearsal."
)
_INTERVIEW_FINAL_REMOTE_TEXT = (
    "What evidence will show the idempotency guard works before the rehearsal?"
)
_INITIAL_STORY_ANCHORS = ("orchid", "tuesday", "duplicate")
_FINAL_STORY_ANCHORS = (*_INITIAL_STORY_ANCHORS, "idempot", "staging")
_DRAFT_STORY_MARKERS = ("duplicate", "idempot", "rehearsal", "tuesday")
_FINAL_DRAFT_EVIDENCE_MARKERS = ("evidence", "log", "test", "verify")
_FINAL_DRAFT_DELIVERY_MARKERS = (
    "duplicate",
    "idempot",
    "same message",
    "message id",
    "one notification",
)
_ALLOWED_INTERVIEW_WEEKDAY = "tuesday"
_WEEKDAY_PATTERN = re.compile(
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
_PLAIN_PROSE_PREFIXES = ("#", "-", "*", ">", "1.")
_PLAIN_PROSE_MARKERS = ("```", "**", "__", "[`", "](")
_RESEARCH_DRAFT_GENERATION_ID = "synthetic-research-draft-generation"
_RESEARCH_REMOTE_TURN_ID = "synthetic-research-remote-turn"
_RESEARCH_REMOTE_PATTERN = re.compile(
    r"^\s*url\s*=\s*https://github\.com/psyb0t/aigate(?:\.git)?\s*$",
    re.MULTILINE,
)
_SAFE_WORKSPACE_ENTRY_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_RESEARCH_REMOTE_TEXT = (
    "Before suggesting what I should say, inspect the public Git repository at "
    "https://github.com/psyb0t/aigate and explain what the project provides."
)
_RESEARCH_REPLY_MARKERS = ("aigate", "gateway", "provider", "model", "api")
_CLAUDEBOX_FILES_PATH = "/claudebox/files"
_MAX_WORKSPACE_SCAN_DEPTH = 3
_MAX_WORKSPACE_DIRECTORIES = 64


class PromptFixtureError(RuntimeError):
    """The explicit real-model prompt probe did not meet its contract."""


def main() -> int:
    try:
        trace_path = asyncio.run(_run())
    except (
        ConfigurationError,
        FixtureTraceError,
        PromptFixtureError,
        ProtocolError,
        RemoteServiceError,
    ):
        print("error: real AIGate prompt contract check failed", file=sys.stderr)
        return 1
    print(
        '{"kind":"real_aigate_prompts","result":"passed","trace_file":'
        f'"{trace_path}"}}'
    )
    return 0


async def _run() -> Path:
    settings = Settings.from_environment()
    trace = FixtureTrace(
        _trace_directory(),
        _TRACE_LABEL,
        secret_values=(settings.aigate_token or "", settings.talkies_token or ""),
    )
    try:
        return await _run_with_trace(settings, trace)
    except Exception as error:
        trace.failure(error)
        raise


async def _run_with_trace(settings: Settings, trace: FixtureTrace) -> Path:
    if settings.aigate_token is None:
        raise PromptFixtureError("AIGate token is required")
    draft_model, commentary_model, summary_model = _fixture_models(settings)
    provider_activities: dict[str, list[Mapping[str, object]]] = {
        model: [] for model in (draft_model, commentary_model, summary_model)
    }

    def activity_sink(model: str) -> Callable[[Mapping[str, object]], None]:
        def record(activity: Mapping[str, object]) -> None:
            provider_activities[model].append(dict(activity))
            _trace_provider_activity(trace, activity)

        return record

    insight_clients = tuple(
        AIGateClient(
            base_url=settings.aigate_url,
            model=model,
            token=settings.aigate_token,
            web_research_enabled=False,
            activity_sink=activity_sink(model),
            streaming_enabled=True,
        )
        for model in (commentary_model, summary_model)
    )
    commentary_client, summary_client = insight_clients
    if draft_model not in CLAUDEBOX_REPLY_MODELS:
        raise PromptFixtureError("Reply fixture requires a Claudebox agent model")
    draft_client = ClaudeboxReplyClient(
        base_url=settings.aigate_url,
        model=draft_model,
        token=settings.aigate_token,
        reasoning_effort="high",
        activity_sink=activity_sink(draft_model),
    )
    trace.event(
        "fixture_started",
        draft_model=draft_model,
        commentary_model=commentary_model,
        summary_model=summary_model,
        base_url=settings.aigate_url,
    )
    await asyncio.gather(
        *(client.verify_configured_model() for client in insight_clients)
    )
    workspace_session_id = await draft_client.start_session()
    trace.event(
        "claudebox_workspace_started",
        workspace_session_id=workspace_session_id,
    )
    trace.event(
        "model_inventory_verified",
        draft_model=draft_model,
        commentary_model=commentary_model,
        summary_model=summary_model,
    )
    await _assert_model_driven_research(
        draft_client,
        settings,
        workspace_session_id,
        trace,
    )
    draft_provider = _TracingDraftProvider(draft_client, trace)
    commentary_provider = _TracingProvider(commentary_client, trace)
    summary_provider = _TracingProvider(summary_client, trace)
    transcript = _synthetic_transcript()
    deadline_seconds = DEFAULT_PROVIDER_GENERATION_DEADLINE.total_seconds()
    draft, commentary, summary = await asyncio.gather(
        draft_provider.draft(
            DraftRequest(
                generation_id=_DRAFT_GENERATION_ID,
                trigger_turn_id=_REMOTE_TURN_ID,
                context_revision=transcript.revision,
                transcript=transcript,
                deadline_seconds=deadline_seconds,
            )
        ),
        commentary_provider.insight(
            InsightRequest(
                generation_id=_COMMENTARY_GENERATION_ID,
                kind=InsightKind.COMMENTARY,
                trigger_turn_id=_USER_TURN_ID,
                context_revision=transcript.revision,
                transcript=transcript,
                deadline_seconds=deadline_seconds,
            )
        ),
        summary_provider.insight(
            InsightRequest(
                generation_id=_SUMMARY_GENERATION_ID,
                kind=InsightKind.SUMMARY,
                trigger_turn_id=_REMOTE_TURN_ID,
                context_revision=transcript.revision,
                transcript=transcript,
                deadline_seconds=deadline_seconds,
            )
        ),
    )
    _assert_completed_text(
        draft.generation_id,
        draft.status,
        draft.text,
        requires_one_line=True,
    )
    _assert_completed_text(
        commentary.generation_id,
        commentary.status,
        commentary.text,
        requires_one_line=False,
    )
    _assert_completed_text(
        summary.generation_id,
        summary.status,
        summary.text,
        requires_one_line=False,
    )
    trace.event("basic_prompt_contract_passed")
    await _assert_interview_story(
        draft_provider,
        commentary_provider,
        summary_provider,
        trace,
    )
    trace.event("fixture_passed")
    return trace.path


def _trace_provider_activity(
    trace: FixtureTrace,
    activity: Mapping[str, object],
) -> None:
    fields = dict(activity)
    fields.pop("kind", None)
    trace.event("provider_activity", **fields)


async def _assert_model_driven_research(
    client: ClaudeboxReplyClient,
    settings: Settings,
    workspace_session_id: str,
    trace: FixtureTrace,
) -> None:
    result = await client.draft(
        DraftRequest(
            generation_id=_RESEARCH_DRAFT_GENERATION_ID,
            trigger_turn_id=_RESEARCH_REMOTE_TURN_ID,
            context_revision=1,
            transcript=TranscriptSnapshot(
                revision=1,
                lines=(
                    TranscriptLine(
                        stream_id=_REMOTE_STREAM_ID,
                        speaker_role=SpeakerRole.REMOTE,
                        revision=1,
                        text=_RESEARCH_REMOTE_TEXT,
                        is_final=True,
                    ),
                ),
            ),
            deadline_seconds=DEFAULT_PROVIDER_GENERATION_DEADLINE.total_seconds(),
        )
    )
    _assert_completed_text(
        result.generation_id,
        result.status,
        result.text,
        requires_one_line=True,
    )
    if sum(marker in result.text.lower() for marker in _RESEARCH_REPLY_MARKERS) < 2:
        raise PromptFixtureError(
            "Reply agent did not return repository-specific AIGate information"
        )
    checkout = await asyncio.to_thread(
        _find_research_checkout,
        settings,
        workspace_session_id,
    )
    trace.event(
        "model_driven_research_verified",
        model=client.model,
        checkout=checkout,
        reply=result.text,
    )


def _read_workspace_entries(
    settings: Settings,
    *path_parts: str,
) -> tuple[dict[str, object], ...]:
    parsed = urlsplit(settings.aigate_url)
    api_path = parsed.path.rstrip("/")
    if not api_path.endswith("/v1"):
        raise PromptFixtureError("AIGate URL must end in /v1")
    gateway_path = api_path.removesuffix("/v1")
    workspace_path = "/".join(quote(part, safe="") for part in path_parts)
    endpoint = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"{gateway_path}{_CLAUDEBOX_FILES_PATH}/{workspace_path}",
            "",
            "",
        )
    )
    headers = {
        HEADER_AUTHORIZATION: f"{BEARER_PREFIX}{settings.aigate_token}",
    }
    request = Request(endpoint, headers=headers, method="GET")
    try:
        with urlopen(
            request,
            timeout=DEFAULT_HTTP_TIMEOUT.total_seconds(),
        ) as response:
            raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
            status_code = response.status
    except HTTPError as error:
        raise PromptFixtureError(
            f"Claudebox workspace listing returned HTTP {error.code}"
        ) from error
    except TimeoutError as error:
        raise PromptFixtureError("Claudebox workspace listing timed out") from error
    except URLError as error:
        raise PromptFixtureError("connect to Claudebox workspace listing") from error
    except OSError as error:
        raise PromptFixtureError("read Claudebox workspace listing") from error
    if status_code != HTTPStatus.OK:
        raise PromptFixtureError(
            f"Claudebox workspace listing returned HTTP {status_code}"
        )
    if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
        raise PromptFixtureError("Claudebox workspace listing exceeds the size limit")
    try:
        payload = require_json_object(decode_json(raw))
        entries = require_json_array(payload.get("entries"))
        return tuple(require_json_object(entry) for entry in entries)
    except (json.JSONDecodeError, ValueError) as error:
        raise PromptFixtureError(
            "Claudebox workspace listing returned invalid JSON"
        ) from error


def _find_research_checkout(
    settings: Settings,
    workspace_session_id: str,
) -> str:
    pending: list[tuple[tuple[str, ...], int]] = [((workspace_session_id,), 0)]
    scanned_directories = 0
    while pending and scanned_directories < _MAX_WORKSPACE_DIRECTORIES:
        directory_parts, depth = pending.pop(0)
        scanned_directories += 1
        entries = _read_workspace_entries(settings, *directory_parts)
        for entry in entries:
            name = entry.get("name")
            if (
                entry.get("type") != "dir"
                or not isinstance(name, str)
                or not _SAFE_WORKSPACE_ENTRY_PATTERN.fullmatch(name)
                or name in {".", ".."}
            ):
                continue
            checkout_parts = (*directory_parts, name)
            git_config = _read_workspace_file(
                settings,
                *checkout_parts,
                ".git",
                "config",
            )
            if git_config is not None and _RESEARCH_REMOTE_PATTERN.search(git_config):
                return "/".join(checkout_parts[1:])
            if depth < _MAX_WORKSPACE_SCAN_DEPTH:
                pending.append((checkout_parts, depth + 1))
    raise PromptFixtureError(
        "Reply agent did not create the required repository checkout"
    )


def _read_workspace_file(
    settings: Settings,
    *path_parts: str,
) -> str | None:
    parsed = urlsplit(settings.aigate_url)
    gateway_path = parsed.path.rstrip("/").removesuffix("/v1")
    encoded_path = "/".join(quote(part, safe="") for part in path_parts)
    endpoint = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"{gateway_path}{_CLAUDEBOX_FILES_PATH}/{encoded_path}",
            "",
            "",
        )
    )
    request = Request(
        endpoint,
        headers={
            HEADER_AUTHORIZATION: f"{BEARER_PREFIX}{settings.aigate_token}",
        },
        method="GET",
    )
    try:
        with urlopen(
            request,
            timeout=DEFAULT_HTTP_TIMEOUT.total_seconds(),
        ) as response:
            raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
    except HTTPError as error:
        if error.code == HTTPStatus.NOT_FOUND:
            return None
        raise PromptFixtureError(
            f"Claudebox workspace file returned HTTP {error.code}"
        ) from error
    except TimeoutError as error:
        raise PromptFixtureError("Claudebox workspace file timed out") from error
    except URLError as error:
        raise PromptFixtureError("connect to Claudebox workspace file") from error
    except OSError as error:
        raise PromptFixtureError("read Claudebox workspace file") from error
    if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
        raise PromptFixtureError("Claudebox workspace file exceeds the size limit")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PromptFixtureError("Claudebox workspace file is not UTF-8") from error


def _trace_directory() -> Path:
    value = os.environ.get(_TRACE_DIRECTORY_ENV, "").strip()
    if not value:
        raise PromptFixtureError("fixture trace directory is required")
    return Path(value)


def _fixture_models(settings: Settings) -> tuple[str, str, str]:
    models = (
        os.environ.get(_DRAFT_MODEL_ENV, settings.aigate_reply_model or "").strip(),
        os.environ.get(
            _COMMENTARY_MODEL_ENV,
            settings.aigate_coach_model or "",
        ).strip(),
        os.environ.get(
            _SUMMARY_MODEL_ENV,
            settings.aigate_summary_model or "",
        ).strip(),
    )
    if any(not model for model in models):
        raise PromptFixtureError("three fixture AIGate models are required")
    if len(set(models)) != len(models):
        raise PromptFixtureError("fixture AIGate models must be distinct")
    return models


class _TracingProvider:
    """Record exact synthetic request context and result lifecycle evidence."""

    def __init__(self, client: AIGateClient, trace: FixtureTrace) -> None:
        self._client = client
        self._trace = trace
        self.draft_requests: list[DraftRequest] = []
        self.insight_requests: list[InsightRequest] = []

    async def draft(self, request: DraftRequest) -> DraftResult:
        self.draft_requests.append(request)
        self._trace.event(
            "draft_request",
            generation_id=request.generation_id,
            trigger_turn_id=request.trigger_turn_id,
            context_revision=request.context_revision,
            transcript=_snapshot_record(request.transcript),
        )
        try:
            result = await self._client.draft(request)
        except (ConfigurationError, ProtocolError, RemoteServiceError) as error:
            self._trace.event(
                "draft_error",
                generation_id=request.generation_id,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        self._trace.event(
            "draft_result",
            generation_id=result.generation_id,
            status=result.status.value,
            context_revision=result.context_revision,
            text=result.text,
        )
        return result

    async def insight(self, request: InsightRequest) -> InsightResult:
        self.insight_requests.append(request)
        self._trace.event(
            "insight_request",
            generation_id=request.generation_id,
            insight_kind=request.kind.value,
            trigger_turn_id=request.trigger_turn_id,
            context_revision=request.context_revision,
            transcript=_snapshot_record(request.transcript),
        )
        try:
            result = await self._client.insight(request)
        except (ConfigurationError, ProtocolError, RemoteServiceError) as error:
            self._trace.event(
                "insight_error",
                generation_id=request.generation_id,
                insight_kind=request.kind.value,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        self._trace.event(
            "insight_result",
            generation_id=result.generation_id,
            insight_kind=result.kind.value,
            status=result.status.value,
            context_revision=result.context_revision,
            text=result.text,
        )
        return result


class _TracingDraftProvider:
    """Record direct-agent requests and results without hiding their context."""

    def __init__(self, client: ClaudeboxReplyClient, trace: FixtureTrace) -> None:
        self._client = client
        self._trace = trace
        self.draft_requests: list[DraftRequest] = []

    async def draft(self, request: DraftRequest) -> DraftResult:
        self.draft_requests.append(request)
        self._trace.event(
            "draft_request",
            generation_id=request.generation_id,
            trigger_turn_id=request.trigger_turn_id,
            context_revision=request.context_revision,
            transcript=_snapshot_record(request.transcript),
        )
        try:
            result = await self._client.draft(request)
        except (ConfigurationError, ProtocolError, RemoteServiceError) as error:
            self._trace.event(
                "draft_error",
                generation_id=request.generation_id,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        self._trace.event(
            "draft_result",
            generation_id=result.generation_id,
            status=result.status.value,
            context_revision=result.context_revision,
            text=result.text,
        )
        return result


async def _assert_interview_story(
    draft_provider: _TracingDraftProvider,
    commentary_provider: _TracingProvider,
    summary_provider: _TracingProvider,
    trace: FixtureTrace,
) -> None:
    coordinator = ConversationCoordinator(
        draft_provider,
        commentary_provider,
        summary_provider,
    )
    try:
        user_update = await coordinator.ingest(
            _interview_event(
                speaker_role=SpeakerRole.USER,
                stream_id=_USER_STREAM_ID,
                revision=_INITIAL_EVENT_REVISION,
                text=_INTERVIEW_USER_TEXT,
            )
        )
        trace.event(
            "coordinator_ingest",
            speaker_role=SpeakerRole.USER.value,
            transcript_revision=user_update.transcript.revision,
            turn_state=user_update.turn.state.value if user_update.turn else None,
            timeline_text=user_update.timeline.text if user_update.timeline else None,
        )
        await coordinator.wait_for_idle()
        initial_insights = coordinator.drain_completed_insights()
        commentary = _completed_insight(initial_insights, InsightKind.COMMENTARY)
        _assert_completed_text(
            commentary.generation_id,
            commentary.status,
            commentary.text,
            requires_one_line=False,
        )
        first_summary = _completed_summary(initial_insights)
        _assert_story_anchors(
            first_summary.text,
            _INITIAL_STORY_ANCHORS,
            "first running summary",
        )
        snapshot_after_user = coordinator.transcript_snapshot()
        if snapshot_after_user.running_summary != first_summary.text:
            raise PromptFixtureError("running summary was not retained by coordinator")
        trace.event(
            "running_summary_verified",
            stage="after_user_turn",
            text=first_summary.text,
        )

        remote_update = await coordinator.ingest(
            _interview_event(
                speaker_role=SpeakerRole.REMOTE,
                stream_id=_REMOTE_STREAM_ID,
                revision=_INITIAL_EVENT_REVISION,
                text=_INTERVIEW_REMOTE_TEXT,
            )
        )
        trace.event(
            "coordinator_ingest",
            speaker_role=SpeakerRole.REMOTE.value,
            transcript_revision=remote_update.transcript.revision,
            turn_state=remote_update.turn.state.value if remote_update.turn else None,
            timeline_text=remote_update.timeline.text
            if remote_update.timeline
            else None,
        )
        draft = await coordinator.wait_for_idle()
        if draft is None:
            raise PromptFixtureError("interview question did not produce a reply draft")
        _assert_completed_text(
            draft.generation_id,
            draft.status,
            draft.text,
            requires_one_line=True,
        )
        if not any(marker in draft.text.lower() for marker in _DRAFT_STORY_MARKERS):
            raise PromptFixtureError(
                "reply draft did not address the interview context"
            )
        _assert_draft_received_summary(
            draft_provider.draft_requests,
            first_summary.text,
        )
        remote_summary = _completed_summary(coordinator.drain_completed_insights())
        _assert_story_anchors(
            remote_summary.text,
            _INITIAL_STORY_ANCHORS,
            "summary after first interviewer question",
        )
        trace.event(
            "running_summary_verified",
            stage="after_first_remote_turn",
            text=remote_summary.text,
        )

        await coordinator.ingest(
            _interview_event(
                speaker_role=SpeakerRole.USER,
                stream_id=_USER_STREAM_ID,
                revision=_FOLLOWUP_PARTIAL_REVISION,
                text=_INTERVIEW_USER_MITIGATION_TEXT,
                source_event_type=TranscriptEventType.PARTIAL,
            )
        )
        mitigation_update = await coordinator.ingest(
            _interview_event(
                speaker_role=SpeakerRole.USER,
                stream_id=_USER_STREAM_ID,
                revision=_FOLLOWUP_FINAL_REVISION,
                text=_INTERVIEW_USER_MITIGATION_TEXT,
            )
        )
        trace.event(
            "coordinator_ingest",
            speaker_role=SpeakerRole.USER.value,
            transcript_revision=mitigation_update.transcript.revision,
            turn_state=(
                mitigation_update.turn.state.value if mitigation_update.turn else None
            ),
            timeline_text=(
                mitigation_update.timeline.text if mitigation_update.timeline else None
            ),
        )
        await coordinator.wait_for_idle()
        mitigation_insights = coordinator.drain_completed_insights()
        mitigation_commentary = _completed_insight(
            mitigation_insights,
            InsightKind.COMMENTARY,
        )
        _assert_completed_text(
            mitigation_commentary.generation_id,
            mitigation_commentary.status,
            mitigation_commentary.text,
            requires_one_line=False,
        )
        mitigation_summary = _completed_summary(mitigation_insights)
        _assert_story_anchors(
            mitigation_summary.text,
            _FINAL_STORY_ANCHORS,
            "summary after user mitigation",
        )
        trace.event(
            "running_summary_verified",
            stage="after_user_mitigation",
            text=mitigation_summary.text,
        )

        await coordinator.ingest(
            _interview_event(
                speaker_role=SpeakerRole.REMOTE,
                stream_id=_REMOTE_STREAM_ID,
                revision=_FOLLOWUP_PARTIAL_REVISION,
                text=_INTERVIEW_FINAL_REMOTE_TEXT,
                source_event_type=TranscriptEventType.PARTIAL,
            )
        )
        final_remote_update = await coordinator.ingest(
            _interview_event(
                speaker_role=SpeakerRole.REMOTE,
                stream_id=_REMOTE_STREAM_ID,
                revision=_FOLLOWUP_FINAL_REVISION,
                text=_INTERVIEW_FINAL_REMOTE_TEXT,
            )
        )
        trace.event(
            "coordinator_ingest",
            speaker_role=SpeakerRole.REMOTE.value,
            transcript_revision=final_remote_update.transcript.revision,
            turn_state=(
                final_remote_update.turn.state.value
                if final_remote_update.turn
                else None
            ),
            timeline_text=(
                final_remote_update.timeline.text
                if final_remote_update.timeline
                else None
            ),
        )
        final_draft = await coordinator.wait_for_idle()
        if final_draft is None:
            raise PromptFixtureError("final interview question did not produce a draft")
        _assert_completed_text(
            final_draft.generation_id,
            final_draft.status,
            final_draft.text,
            requires_one_line=True,
        )
        _assert_final_draft_story(final_draft.text)
        _assert_draft_received_summary(
            draft_provider.draft_requests,
            mitigation_summary.text,
        )
        final_summary = _completed_summary(coordinator.drain_completed_insights())
        _assert_story_anchors(
            final_summary.text,
            _FINAL_STORY_ANCHORS,
            "final running summary",
        )
        trace.event(
            "interview_story_verified",
            first_draft_text=draft.text,
            final_draft_text=final_draft.text,
            first_summary=first_summary.text,
            final_summary=final_summary.text,
        )
    finally:
        await coordinator.stop()


def _interview_event(
    *,
    speaker_role: SpeakerRole,
    stream_id: str,
    revision: int,
    text: str,
    source_event_type: TranscriptEventType = TranscriptEventType.FINAL,
) -> TranscriptEvent:
    return TranscriptEvent(
        session_id=_SYNTHETIC_SESSION_ID,
        stream_id=stream_id,
        utterance_id=f"{stream_id}:{revision}",
        revision=revision,
        speaker_role=speaker_role,
        source_event_type=source_event_type,
        asr_model="synthetic-interview-asr",
        text=text,
        is_final=source_event_type is TranscriptEventType.FINAL,
        audio_seconds=1.0,
        words=(),
    )


def _completed_summary(insights: tuple[InsightResult, ...]) -> InsightResult:
    return _completed_insight(insights, InsightKind.SUMMARY)


def _completed_insight(
    insights: tuple[InsightResult, ...],
    expected_kind: InsightKind,
) -> InsightResult:
    for insight in reversed(insights):
        if (
            insight.kind is expected_kind
            and insight.status is GenerationStatus.COMPLETED
        ):
            return insight
    raise PromptFixtureError(
        f"interview did not produce completed {expected_kind.value}"
    )


def _assert_story_anchors(
    summary: str,
    anchors: tuple[str, ...],
    stage: str,
) -> None:
    normalized_summary = summary.lower()
    missing = [anchor for anchor in anchors if anchor not in normalized_summary]
    if missing:
        raise PromptFixtureError(f"{stage} omitted required story anchors")


def _assert_draft_received_summary(
    draft_requests: list[DraftRequest],
    expected_summary: str,
) -> None:
    if not draft_requests:
        raise PromptFixtureError("interview did not send a draft request")
    request = draft_requests[-1]
    if request.transcript.running_summary != expected_summary:
        raise PromptFixtureError("reply draft did not receive the running summary")


def _assert_final_draft_story(draft: str) -> None:
    normalized_draft = draft.lower()
    if not any(marker in normalized_draft for marker in _FINAL_DRAFT_EVIDENCE_MARKERS):
        raise PromptFixtureError("final reply draft did not provide concrete evidence")
    if not any(marker in normalized_draft for marker in _FINAL_DRAFT_DELIVERY_MARKERS):
        raise PromptFixtureError("final reply draft did not address delivery safety")
    unexpected_weekdays = {
        match.group().lower()
        for match in _WEEKDAY_PATTERN.finditer(draft)
        if match.group().lower() != _ALLOWED_INTERVIEW_WEEKDAY
    }
    if unexpected_weekdays:
        raise PromptFixtureError("final reply draft introduced an unstated weekday")


def _snapshot_record(snapshot: TranscriptSnapshot) -> dict[str, object]:
    return {
        "revision": snapshot.revision,
        "running_summary": snapshot.running_summary,
        "lines": [
            {
                "stream_id": line.stream_id,
                "speaker_role": line.speaker_role.value,
                "revision": line.revision,
                "text": line.text,
                "is_final": line.is_final,
            }
            for line in snapshot.lines
        ],
    }


def _synthetic_transcript() -> TranscriptSnapshot:
    return TranscriptSnapshot(
        revision=_TRANSCRIPT_REVISION,
        running_summary="The speakers are clarifying a proposed technical approach.",
        lines=(
            TranscriptLine(
                stream_id="synthetic-user-stream",
                speaker_role=SpeakerRole.USER,
                revision=1,
                text="Could you explain the tradeoff in a practical way?",
                is_final=True,
            ),
            TranscriptLine(
                stream_id="synthetic-remote-stream",
                speaker_role=SpeakerRole.REMOTE,
                revision=_TRANSCRIPT_REVISION,
                text="The approach reduces setup effort but needs careful monitoring.",
                is_final=True,
            ),
        ),
    )


def _assert_completed_text(
    generation_id: str,
    status: GenerationStatus,
    text: str,
    *,
    requires_one_line: bool,
) -> None:
    if status is not GenerationStatus.COMPLETED or not text.strip():
        raise PromptFixtureError(f"{generation_id} did not complete with text")
    stripped_text = text.strip()
    if any(marker in stripped_text for marker in _PLAIN_PROSE_MARKERS):
        raise PromptFixtureError(f"{generation_id} returned Markdown formatting")
    if stripped_text.startswith(_PLAIN_PROSE_PREFIXES):
        raise PromptFixtureError(f"{generation_id} returned Markdown structure")
    if requires_one_line and "\n" in stripped_text:
        raise PromptFixtureError(f"{generation_id} returned multiple lines")


if __name__ == "__main__":
    raise SystemExit(main())
