from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import AsyncIterator, Mapping
from http import HTTPStatus
from unittest.mock import AsyncMock, patch

import httpx

from two_x_brainz.claudebox import (
    ClaudeboxReplyClient,
    _iter_sse_events,  # pyright: ignore[reportPrivateUsage]
    _parse_completion_chunk,  # pyright: ignore[reportPrivateUsage]
    _parse_completion_response,  # pyright: ignore[reportPrivateUsage]
    _requires_repository_research,  # pyright: ignore[reportPrivateUsage]
)
from two_x_brainz.constants import MAX_DRAFT_TEXT_CHARACTERS
from two_x_brainz.contracts import (
    DraftRequest,
    DraftResult,
    SpeakerRole,
    TranscriptLine,
    TranscriptSnapshot,
)
from two_x_brainz.errors import (
    ConfigurationError,
    EmptyProviderContentError,
    IncompleteProviderStreamError,
    OversizedProviderOutputError,
    ProtocolError,
    RemoteServiceError,
)
from two_x_brainz.json_support import require_json_array


class ClaudeboxReplyClientTests(unittest.TestCase):
    def test_payload_omits_client_tools_and_headers_enable_native_tools(self) -> None:
        client = _client(session_brief="Product interview")
        workspace = asyncio.run(client.start_session())

        payload = client._request_payload(_snapshot())  # pyright: ignore[reportPrivateUsage]
        first_headers = client._request_headers(  # pyright: ignore[reportPrivateUsage]
            workspace,
            continue_session=False,
            instructions="Synthetic appended\ninstructions",
        )
        continued_headers = client._request_headers(  # pyright: ignore[reportPrivateUsage]
            workspace,
            continue_session=True,
            instructions="Synthetic appended\ninstructions",
        )

        self.assertEqual(payload["model"], "sonnet")
        self.assertIs(payload["stream"], True)
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)
        self.assertNotIn("response_format", payload)
        self.assertEqual(first_headers["X-Aicodebox-Workspace"], workspace)
        self.assertEqual(first_headers["X-Aicodebox-No-Tools"], "0")
        self.assertEqual(
            first_headers["X-Aicodebox-Append-System-Prompt"],
            "Synthetic appended instructions",
        )
        self.assertNotIn("X-Aicodebox-Continue", first_headers)
        self.assertEqual(continued_headers["X-Aicodebox-Continue"], "true")
        messages = require_json_array(payload["messages"])
        self.assertEqual(len(messages), 1)
        self.assertIn("Product interview", str(messages[0]))

    def test_streamed_deltas_update_activity_and_complete_reply(self) -> None:
        activities: list[Mapping[str, object]] = []
        client = _client(activities=activities)
        response = _sse_response(
            [
                _chunk("The repository "),
                _chunk("provides an AI gateway."),
                _chunk("", finish_reason="stop"),
                "[DONE]",
            ]
        )

        result = asyncio.run(
            client._consume_stream(  # pyright: ignore[reportPrivateUsage]
                response,
                flow_id="flow-1",
                generation_id="generation-1",
            )
        )

        self.assertEqual(result, "The repository provides an AI gateway.")
        self.assertEqual(
            [event["output"] for event in activities],
            ["The repository ", "The repository provides an AI gateway."],
        )

    def test_second_successful_request_continues_same_workspace(self) -> None:
        client = _client()
        calls: list[tuple[str, bool]] = []

        async def complete(
            _client_instance: ClaudeboxReplyClient,
            transcript: TranscriptSnapshot,
            workspace_session_id: str,
            *,
            continue_session: bool,
            flow_id: str,
            generation_id: str,
        ) -> str:
            del transcript, flow_id, generation_id
            calls.append((workspace_session_id, continue_session))
            return "Grounded reply."

        async def exercise() -> tuple[str, str]:
            first_workspace = await client.start_session()
            await client.draft(_draft_request("generation-1"))
            await client.draft(_draft_request("generation-2"))
            second_workspace = await client.start_session()
            await client.draft(_draft_request("generation-3"))
            return first_workspace, second_workspace

        with patch.object(ClaudeboxReplyClient, "_stream_completion", new=complete):
            first_workspace, second_workspace = asyncio.run(exercise())

        self.assertNotEqual(first_workspace, second_workspace)
        self.assertEqual(
            calls,
            [
                (first_workspace, False),
                (first_workspace, True),
                (second_workspace, False),
            ],
        )

    def test_cancelled_stream_does_not_initialize_workspace(self) -> None:
        activities: list[Mapping[str, object]] = []
        client = _client(activities=activities)
        started = asyncio.Event()

        async def blocked(*args: object, **kwargs: object) -> str:
            del args, kwargs
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def exercise() -> None:
            await client.start_session()
            task = asyncio.create_task(client.draft(_draft_request()))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with patch.object(ClaudeboxReplyClient, "_stream_completion", new=blocked):
            asyncio.run(exercise())

        self.assertFalse(client._workspace_initialized)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(activities[-1]["phase"], "request_cancelled")

    def test_replacement_waits_for_superseded_operation_then_continues(self) -> None:
        activities: list[Mapping[str, object]] = []
        client = _client(activities=activities)
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[bool] = []

        async def complete(
            _client_instance: ClaudeboxReplyClient,
            transcript: TranscriptSnapshot,
            workspace_session_id: str,
            *,
            continue_session: bool,
            flow_id: str,
            generation_id: str,
        ) -> str:
            del transcript, workspace_session_id
            calls.append(continue_session)
            if len(calls) == 1:
                started.set()
                await release.wait()
                client._activity(  # pyright: ignore[reportPrivateUsage]
                    phase="output_streaming",
                    flow_id=flow_id,
                    generation_id=generation_id,
                    output_kind="draft",
                    output="Stale reply.",
                )
            return "Grounded reply."

        async def exercise() -> DraftResult:
            await client.start_session()
            first = asyncio.create_task(client.draft(_draft_request("generation-1")))
            await started.wait()
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first

            replacement = asyncio.create_task(
                client.draft(_draft_request("generation-2"))
            )
            await asyncio.sleep(0)
            self.assertEqual(calls, [False])
            release.set()
            return await replacement

        with patch.object(ClaudeboxReplyClient, "_stream_completion", new=complete):
            result = asyncio.run(exercise())

        self.assertEqual(result.text, "Grounded reply.")
        self.assertEqual(calls, [False, True])
        self.assertFalse(
            any(
                activity.get("generation_id") == "generation-1"
                and activity.get("phase") == "output_streaming"
                for activity in activities
            )
        )

    def test_new_session_does_not_wait_for_superseded_previous_workspace(self) -> None:
        client = _client()
        first_started = asyncio.Event()
        first_release = asyncio.Event()
        calls: list[tuple[str, bool]] = []

        async def complete(
            _client_instance: ClaudeboxReplyClient,
            transcript: TranscriptSnapshot,
            workspace_session_id: str,
            *,
            continue_session: bool,
            flow_id: str,
            generation_id: str,
        ) -> str:
            del transcript, flow_id
            calls.append((workspace_session_id, continue_session))
            if generation_id == "generation-1":
                first_started.set()
                await first_release.wait()
            return "Grounded reply."

        async def exercise() -> tuple[str, str, DraftResult]:
            first_workspace = await client.start_session()
            first = asyncio.create_task(client.draft(_draft_request("generation-1")))
            await first_started.wait()
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first

            second_workspace = await client.start_session()
            replacement = await asyncio.wait_for(
                client.draft(_draft_request("generation-2")),
                timeout=0.1,
            )
            first_release.set()
            await asyncio.sleep(0)
            return first_workspace, second_workspace, replacement

        with patch.object(ClaudeboxReplyClient, "_stream_completion", new=complete):
            first_workspace, second_workspace, replacement = asyncio.run(exercise())

        self.assertNotEqual(first_workspace, second_workspace)
        self.assertEqual(replacement.text, "Grounded reply.")
        self.assertEqual(
            calls,
            [(first_workspace, False), (second_workspace, False)],
        )

    def test_unterminated_tool_stream_recovers_in_same_workspace(self) -> None:
        activities: list[Mapping[str, object]] = []
        client = _client(activities=activities)
        calls: list[tuple[str, str, bool]] = []

        async def fail_stream(
            _client_instance: ClaudeboxReplyClient,
            transcript: TranscriptSnapshot,
            workspace_session_id: str,
            *,
            continue_session: bool,
            flow_id: str,
            generation_id: str,
        ) -> str:
            del transcript, flow_id, generation_id
            calls.append(("stream", workspace_session_id, continue_session))
            raise IncompleteProviderStreamError("missing terminal event")

        async def recover(
            _client_instance: ClaudeboxReplyClient,
            workspace_session_id: str,
        ) -> str:
            calls.append(("recovery", workspace_session_id, True))
            return "AIGate provides a unified AI gateway."

        async def exercise() -> tuple[str, str]:
            workspace = await client.start_session()
            result = await client.draft(_draft_request())
            return workspace, result.text

        with (
            patch.object(ClaudeboxReplyClient, "_stream_request", new=fail_stream),
            patch.object(
                ClaudeboxReplyClient,
                "_complete_nonstreaming",
                new=recover,
            ),
        ):
            workspace, result = asyncio.run(exercise())

        self.assertEqual(result, "AIGate provides a unified AI gateway.")
        self.assertEqual(
            calls,
            [("stream", workspace, False), ("recovery", workspace, True)],
        )
        self.assertIn(
            "stream_recovery_started",
            [activity["phase"] for activity in activities],
        )
        self.assertTrue(client._workspace_initialized)  # pyright: ignore[reportPrivateUsage]

        recovery_payload = client._recovery_payload()  # pyright: ignore[reportPrivateUsage]
        self.assertIs(recovery_payload["stream"], False)
        self.assertNotIn("tools", recovery_payload)
        self.assertIn("completed research", str(recovery_payload["messages"]))

    def test_oversized_stream_uses_the_same_bounded_recovery(self) -> None:
        client = _client()

        async def fail_stream(*args: object, **kwargs: object) -> str:
            del args, kwargs
            raise OversizedProviderOutputError("too long")

        async def recover(*args: object, **kwargs: object) -> str:
            del args, kwargs
            return "Concise recovered reply."

        async def exercise() -> str:
            await client.start_session()
            return (await client.draft(_draft_request())).text

        with (
            patch.object(ClaudeboxReplyClient, "_stream_request", new=fail_stream),
            patch.object(
                ClaudeboxReplyClient,
                "_complete_nonstreaming",
                new=recover,
            ),
        ):
            self.assertEqual(asyncio.run(exercise()), "Concise recovered reply.")

    def test_nonstreaming_retries_transient_busy_workspace(self) -> None:
        client = _client()
        completed = httpx.Response(
            HTTPStatus.OK,
            content=json.dumps(
                {"choices": [{"message": {"content": "Grounded reply."}}]}
            ).encode(),
            request=httpx.Request("POST", "http://fixture/v1/chat/completions"),
        )
        responses = AsyncMock(
            side_effect=(
                httpx.Response(
                    HTTPStatus.CONFLICT,
                    request=httpx.Request(
                        "POST",
                        "http://fixture/v1/chat/completions",
                    ),
                ),
                httpx.Response(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    request=httpx.Request(
                        "POST",
                        "http://fixture/v1/chat/completions",
                    ),
                ),
                completed,
            )
        )

        with (
            patch.object(
                ClaudeboxReplyClient,
                "_post_nonstreaming_response",
                new=responses,
            ),
            patch("two_x_brainz.claudebox.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            result = asyncio.run(client._post_nonstreaming({}, {}))  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(result, "Grounded reply.")
        self.assertEqual(responses.await_count, 3)
        self.assertEqual(sleep.await_count, 2)

    def test_nonstreaming_busy_workspace_exhaustion_is_bounded(self) -> None:
        client = _client()
        responses = AsyncMock(
            return_value=httpx.Response(
                HTTPStatus.CONFLICT,
                request=httpx.Request(
                    "POST",
                    "http://fixture/v1/chat/completions",
                ),
            )
        )

        with (
            patch.object(
                ClaudeboxReplyClient,
                "_post_nonstreaming_response",
                new=responses,
            ),
            patch("two_x_brainz.claudebox.asyncio.sleep", new=AsyncMock()),
            self.assertRaisesRegex(RemoteServiceError, "HTTP 409"),
        ):
            asyncio.run(client._post_nonstreaming({}, {}))  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(responses.await_count, 40)

    def test_repository_url_uses_buffered_native_research_path(self) -> None:
        client = _client()
        repository_snapshot = TranscriptSnapshot(
            revision=1,
            lines=(
                TranscriptLine(
                    stream_id="remote",
                    speaker_role=SpeakerRole.REMOTE,
                    revision=1,
                    text="Inspect https://github.com/example/project before replying.",
                    is_final=True,
                ),
            ),
        )
        request = DraftRequest(
            generation_id="repository-research",
            trigger_turn_id="turn-1",
            context_revision=1,
            transcript=repository_snapshot,
            deadline_seconds=60,
        )
        calls: list[str] = []

        async def research(*args: object, **kwargs: object) -> str:
            del args, kwargs
            calls.append("research")
            return "Research completed with progress chatter."

        async def final_reply(*args: object, **kwargs: object) -> str:
            del args, kwargs
            calls.append("final")
            return "Grounded repository reply."

        async def stream(*args: object, **kwargs: object) -> str:
            del args, kwargs
            calls.append("stream")
            return "Unexpected stream reply."

        async def exercise() -> str:
            await client.start_session()
            return (await client.draft(request)).text

        with (
            patch.object(
                ClaudeboxReplyClient,
                "_complete_research_nonstreaming",
                new=research,
            ),
            patch.object(
                ClaudeboxReplyClient,
                "_complete_nonstreaming",
                new=final_reply,
            ),
            patch.object(ClaudeboxReplyClient, "_stream_request", new=stream),
        ):
            self.assertEqual(
                asyncio.run(exercise()),
                "Grounded repository reply.",
            )
        self.assertEqual(calls, ["research", "final"])
        self.assertTrue(_requires_repository_research(repository_snapshot))
        self.assertFalse(_requires_repository_research(_snapshot()))

    def test_spoken_repository_reference_requires_native_research(self) -> None:
        for text in (
            "Research the GitHub repository called AI Gate.",
            "Check the repository named AI Gate on git hub.",
            "Inspect the GitLab project before answering.",
        ):
            with self.subTest(text=text):
                self.assertTrue(
                    _requires_repository_research(_snapshot_with_remote_text(text))
                )

    def test_unrelated_host_or_repository_words_do_not_trigger_research(self) -> None:
        for text in (
            "GitHub is a website.",
            "That repository needs a clearer pitch.",
            "Run repository; rm -rf slash.",
        ):
            with self.subTest(text=text):
                self.assertFalse(
                    _requires_repository_research(_snapshot_with_remote_text(text))
                )

    def test_stream_rejects_error_malformed_empty_and_unterminated_data(self) -> None:
        client = _client()
        cases: tuple[tuple[list[object], type[Exception], str], ...] = (
            (
                [{"error": {"message": "upstream unavailable"}}, "[DONE]"],
                RemoteServiceError,
                "upstream unavailable",
            ),
            ([{"unexpected": True}, "[DONE]"], ProtocolError, "choices"),
            (["[DONE]"], EmptyProviderContentError, "non-empty"),
            ([_chunk("unfinished")], ProtocolError, r"before \[DONE\]"),
            (
                [_chunk("x" * (MAX_DRAFT_TEXT_CHARACTERS + 1)), "[DONE]"],
                ProtocolError,
                "size limit",
            ),
        )
        for events, error_type, message in cases:
            with self.subTest(error_type=error_type.__name__):
                response = _sse_response(events)
                with self.assertRaisesRegex(error_type, message):
                    asyncio.run(
                        client._consume_stream(  # pyright: ignore[reportPrivateUsage]
                            response,
                            flow_id="flow-1",
                            generation_id="generation-1",
                        )
                    )

    def test_invalid_configuration_and_missing_session_fail_before_io(self) -> None:
        for base_url, model, effort in (
            ("http://aigate.example", "claudebox-sonnet", "high"),
            ("file:///tmp/v1", "claudebox-sonnet", "high"),
            ("http://aigate.example/v1", "pibox-zai", "high"),
            ("http://aigate.example/v1", "claudebox-sonnet", "minimal"),
        ):
            with (
                self.subTest(base_url=base_url, model=model, effort=effort),
                self.assertRaises(ConfigurationError),
            ):
                _client(base_url=base_url, model=model, reasoning_effort=effort)
        with self.assertRaisesRegex(ConfigurationError, "Start listening"):
            asyncio.run(_client().draft(_draft_request()))

    def test_sse_parser_ignores_non_data_lines_and_rejects_invalid_json(self) -> None:
        response = _raw_sse_response(
            [
                b": heartbeat\n",
                b"event: message\n",
                b'data: {"choices":[]}\n\n',
                b"data: [DONE]\n\n",
            ]
        )
        parsed = asyncio.run(_collect_sse(response))
        self.assertEqual(parsed, [{"choices": []}, "[DONE]"])

        malformed = _raw_sse_response([b"data: {broken}\n\n"])
        with self.assertRaises(ProtocolError):
            asyncio.run(_collect_sse(malformed))

    def test_completion_chunk_rejects_non_text_content(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "must be text"):
            _parse_completion_chunk(_chunk_payload(42))

    def test_nonstreaming_response_parser_validates_completion_contract(self) -> None:
        valid = json.dumps(
            {"choices": [{"message": {"content": "  Grounded\nreply.  "}}]}
        ).encode()
        self.assertEqual(
            _parse_completion_response(valid, max_characters=1_000),
            "Grounded reply.",
        )

        invalid_cases = (
            b"not-json",
            b'{"choices": []}',
            b'{"choices": [{"message": {"content": 4}}]}',
        )
        for raw in invalid_cases:
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                _parse_completion_response(raw, max_characters=1_000)


class _AsyncSSEStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


def _sse_response(events: list[object]) -> httpx.Response:
    chunks: list[bytes] = []
    for event in events:
        encoded = event if isinstance(event, str) else json.dumps(event)
        chunks.append(f"data: {encoded}\n\n".encode())
    return _raw_sse_response(chunks)


def _raw_sse_response(chunks: list[bytes]) -> httpx.Response:
    return httpx.Response(
        HTTPStatus.OK,
        headers={"content-type": "text/event-stream"},
        stream=_AsyncSSEStream(chunks),
        request=httpx.Request("POST", "http://fixture/v1/chat/completions"),
    )


def _chunk(text: str, finish_reason: str | None = None) -> dict[str, object]:
    return _chunk_payload(text, finish_reason=finish_reason)


def _chunk_payload(
    content: object,
    *,
    finish_reason: str | None = None,
) -> dict[str, object]:
    return {
        "choices": [
            {
                "delta": {"content": content},
                "finish_reason": finish_reason,
            }
        ]
    }


async def _collect_sse(response: httpx.Response) -> list[object]:
    return [event async for event in _iter_sse_events(response)]


def _client(
    *,
    base_url: str = "http://aigate.example/v1",
    model: str = "claudebox-sonnet",
    token: str | None = "fixture-token",
    reasoning_effort: str = "high",
    session_brief: str | None = None,
    activities: list[Mapping[str, object]] | None = None,
) -> ClaudeboxReplyClient:
    sink = None
    if activities is not None:

        def collect_activity(event: Mapping[str, object]) -> None:
            activities.append(dict(event))

        sink = collect_activity
    return ClaudeboxReplyClient(
        base_url=base_url,
        model=model,
        token=token,
        reasoning_effort=reasoning_effort,
        session_brief=session_brief,
        activity_sink=sink,
    )


def _draft_request(generation_id: str = "generation-1") -> DraftRequest:
    return DraftRequest(
        generation_id=generation_id,
        trigger_turn_id="turn-1",
        context_revision=2,
        transcript=_snapshot(),
        deadline_seconds=60,
    )


def _snapshot() -> TranscriptSnapshot:
    return TranscriptSnapshot(
        revision=2,
        running_summary="The speakers are discussing a named software project.",
        lines=(
            TranscriptLine(
                stream_id="remote",
                speaker_role=SpeakerRole.REMOTE,
                revision=1,
                text="What does the named project do?",
                is_final=True,
            ),
        ),
    )


def _snapshot_with_remote_text(text: str) -> TranscriptSnapshot:
    return TranscriptSnapshot(
        revision=1,
        lines=(
            TranscriptLine(
                stream_id="remote",
                speaker_role=SpeakerRole.REMOTE,
                revision=1,
                text=text,
                is_final=True,
            ),
        ),
    )
