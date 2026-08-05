from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from two_x_brainz.aigate import AIGateClient
from two_x_brainz.constants import (
    DEFAULT_PROVIDER_GENERATION_DEADLINE,
    MAX_COMMENTARY_TOKENS,
    MAX_DRAFT_TEXT_CHARACTERS,
    MAX_PROVIDER_RESPONSE_BYTES,
    MAX_REPLY_DRAFT_TOKENS,
    MAX_SUMMARY_TOKENS,
)
from two_x_brainz.contracts import (
    DraftRequest,
    InsightKind,
    InsightRequest,
    SpeakerRole,
    TranscriptLine,
    TranscriptSnapshot,
)
from two_x_brainz.errors import ConfigurationError, ProtocolError, RemoteServiceError
from two_x_brainz.json_support import require_json_array, require_json_object


class AIGateClientTests(unittest.TestCase):
    def test_require_model_rejects_an_unconfigured_client(self) -> None:
        client = AIGateClient(
            base_url="http://aigate.example/v1",
            model=None,
            token=None,
        )

        with self.assertRaisesRegex(ConfigurationError, "AIGATE_MODEL"):
            client.require_model()

    def test_preflight_accepts_configured_model_and_sends_bearer_auth(self) -> None:
        response = _HTTPResponse(b'{"object":"list","data":[{"id":"test-model"}]}')
        with patch(
            "two_x_brainz.aigate.urlopen",
            return_value=response,
        ) as urlopen_mock:
            asyncio.run(_client(token="test-token").verify_configured_model())

        request = urlopen_mock.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "http://aigate.example/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")

    def test_both_endpoints_resolve_under_the_configured_base_prefix(self) -> None:
        # The two paths must agree about whether the base carries the API
        # prefix. A mismatch is invisible against AIGate, whose nginx forwards
        # verbatim to LiteLLM and answers on the prefixed and unprefixed form
        # alike, but no single base URL then works against OpenAI or Groq.
        posted = _HTTPResponse(
            b'{"choices":[{"message":{"content":"draft"}}]}',
        )
        listed = _HTTPResponse(b'{"object":"list","data":[{"id":"test-model"}]}')
        for base in ("https://api.openai.com/v1", "https://api.openai.com/v1/"):
            with self.subTest(base=base):
                client = AIGateClient(
                    base_url=base,
                    model="test-model",
                    token="test-token",
                )
                with patch(
                    "two_x_brainz.aigate.urlopen",
                    return_value=listed,
                ) as models_mock:
                    asyncio.run(client.verify_configured_model())
                with patch(
                    "two_x_brainz.aigate.urlopen",
                    return_value=posted,
                ) as chat_mock:
                    asyncio.run(client.draft(_draft_request()))

                self.assertEqual(
                    models_mock.call_args.args[0].full_url,
                    "https://api.openai.com/v1/models",
                )
                self.assertEqual(
                    chat_mock.call_args.args[0].full_url,
                    "https://api.openai.com/v1/chat/completions",
                )

    def test_preflight_rejects_unavailable_and_malformed_inventories(self) -> None:
        invalid_inventories = (
            b'{"object":"list","data":[{"id":"other-model"}]}',
            b'{"object":"list","data":[]}',
            b'{"object":"list","data":[{"id":"test-model"},{"id":"test-model"}]}',
            b'{"object":"list","data":[{"id":"  "}]}',
            b'{"object":"object","data":[{"id":"test-model"}]}',
            b"not-json",
        )

        for body in invalid_inventories:
            with (
                self.subTest(body=body),
                patch(
                    "two_x_brainz.aigate.urlopen",
                    return_value=_HTTPResponse(body),
                ),
                self.assertRaises((ProtocolError, RemoteServiceError)),
            ):
                asyncio.run(_client().verify_configured_model())

    def test_preflight_wraps_transport_failures_and_rejects_oversized_inventory(
        self,
    ) -> None:
        with (
            patch("two_x_brainz.aigate.urlopen", side_effect=TimeoutError),
            self.assertRaisesRegex(RemoteServiceError, "inventory timed out"),
        ):
            asyncio.run(_client().verify_configured_model())

        with (
            patch(
                "two_x_brainz.aigate.urlopen",
                return_value=_HTTPResponse(b"{}", status=503),
            ),
            self.assertRaisesRegex(RemoteServiceError, "HTTP 503"),
        ):
            asyncio.run(_client().verify_configured_model())

        oversized_inventory = b"x" * (MAX_PROVIDER_RESPONSE_BYTES + 1)
        with (
            patch(
                "two_x_brainz.aigate.urlopen",
                return_value=_HTTPResponse(oversized_inventory),
            ),
            self.assertRaisesRegex(ProtocolError, "exceeds size limit"),
        ):
            asyncio.run(_client().verify_configured_model())

    def test_draft_sets_the_reply_token_budget(self) -> None:
        payloads: list[dict[str, object]] = []

        def post(client: AIGateClient, payload: dict[str, object]) -> object:
            payloads.append(payload)
            return _completion("short reply")

        with patch.object(AIGateClient, "_post", new=post):
            result = asyncio.run(_client().draft(_draft_request()))

        self.assertEqual(result.text, "short reply")
        self.assertEqual(payloads[0]["max_tokens"], MAX_REPLY_DRAFT_TOKENS)
        self.assertGreaterEqual(MAX_REPLY_DRAFT_TOKENS, MAX_SUMMARY_TOKENS)
        messages = require_json_array(payloads[0]["messages"])
        draft_prompt = require_json_object(messages[0])["content"]
        assert isinstance(draft_prompt, str)
        self.assertIn("Never introduce an unstated date", draft_prompt)
        self.assertIn("clearly phrased as a proposal", draft_prompt)
        self.assertIn("Never present a proposed mechanism", draft_prompt)

    def test_runtime_reasoning_effort_is_snapshotted_into_request(self) -> None:
        payloads: list[dict[str, object]] = []
        activities: list[dict[str, object]] = []

        def post(client: AIGateClient, payload: dict[str, object]) -> object:
            del client
            payloads.append(payload)
            return _completion("short reply")

        client = _client()
        client.activity_sink = lambda event: activities.append(dict(event))
        client.configure("test-model", "high")
        with patch.object(AIGateClient, "_post", new=post):
            asyncio.run(client.draft(_draft_request()))

        self.assertEqual(payloads[0]["reasoning_effort"], "high")
        self.assertEqual(activities[0]["phase"], "request_started")
        self.assertEqual(activities[-1]["phase"], "request_completed")
        self.assertNotIn("messages", activities[0])

    def test_runtime_provider_settings_reject_invalid_reasoning(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "reasoning effort"):
            _client().configure("test-model", "unbounded")

    def test_model_inventory_is_sorted_for_selector(self) -> None:
        response = _HTTPResponse(
            b'{"object":"list","data":[{"id":"z-model"},{"id":"a-model"}]}'
        )
        with patch("two_x_brainz.aigate.urlopen", return_value=response):
            models = asyncio.run(_client().list_models())

        self.assertEqual(models, ("a-model", "z-model"))

    def test_session_brief_frames_all_generation_prompts(self) -> None:
        payloads: list[dict[str, object]] = []

        def post(client: AIGateClient, payload: dict[str, object]) -> object:
            del client
            payloads.append(payload)
            return _completion("short result")

        client = AIGateClient(
            base_url="http://aigate.example/v1",
            model="test-model",
            token=None,
            session_brief="Interview for a product role.",
        )
        with patch.object(AIGateClient, "_post", new=post):
            asyncio.run(client.draft(_draft_request()))
            asyncio.run(client.insight(_insight_request(InsightKind.COMMENTARY)))
            asyncio.run(client.insight(_insight_request(InsightKind.SUMMARY)))

        for payload in payloads:
            messages = require_json_array(payload["messages"])
            prompt = require_json_object(messages[0])["content"]
            assert isinstance(prompt, str)
            self.assertIn("Operator-provided session brief", prompt)
            self.assertIn("Interview for a product role.", prompt)

    def test_draft_rejects_empty_visible_content(self) -> None:
        with (
            patch.object(AIGateClient, "_post", return_value=_completion("")),
            self.assertRaisesRegex(ProtocolError, "non-empty text"),
        ):
            asyncio.run(_client().draft(_draft_request()))

    def test_draft_retries_one_empty_visible_completion(self) -> None:
        with (
            patch.object(
                AIGateClient,
                "_post",
                side_effect=[_completion(""), _completion("usable reply")],
            ) as post_mock,
            self.assertLogs("two_x_brainz.aigate", level="WARNING") as logs,
        ):
            result = asyncio.run(_client().draft(_draft_request()))

        self.assertEqual(result.text, "usable reply")
        self.assertEqual(post_mock.call_count, 2)
        self.assertEqual(logs.records[0].__dict__["reason"], "empty_completion")

    def test_draft_wraps_a_transport_timeout(self) -> None:
        with (
            patch("two_x_brainz.aigate.urlopen", side_effect=TimeoutError),
            self.assertRaisesRegex(RemoteServiceError, "timed out"),
        ):
            asyncio.run(_client().draft(_draft_request()))

    def test_generation_transport_uses_the_provider_deadline(self) -> None:
        with patch(
            "two_x_brainz.aigate.urlopen",
            return_value=_HTTPResponse(
                b'{"choices":[{"message":{"content":"draft"}}]}'
            ),
        ) as urlopen_mock:
            asyncio.run(_client().draft(_draft_request()))

        self.assertEqual(
            urlopen_mock.call_args.kwargs["timeout"],
            DEFAULT_PROVIDER_GENERATION_DEADLINE.total_seconds(),
        )

    def test_insights_set_kind_specific_token_budgets(self) -> None:
        payloads: list[dict[str, object]] = []

        def post(client: AIGateClient, payload: dict[str, object]) -> object:
            payloads.append(payload)
            return _completion("private note")

        with patch.object(AIGateClient, "_post", new=post):
            asyncio.run(_client().insight(_insight_request(InsightKind.COMMENTARY)))
            asyncio.run(_client().insight(_insight_request(InsightKind.SUMMARY)))

        self.assertEqual(payloads[0]["max_tokens"], MAX_COMMENTARY_TOKENS)
        self.assertEqual(payloads[1]["max_tokens"], MAX_SUMMARY_TOKENS)
        commentary_messages = require_json_array(payloads[0]["messages"])
        commentary_prompt = require_json_object(commentary_messages[0])["content"]
        assert isinstance(commentary_prompt, str)
        self.assertIn("no more than 80 words", commentary_prompt)

        summary_messages = require_json_array(payloads[1]["messages"])
        summary_prompt = require_json_object(summary_messages[0])["content"]
        assert isinstance(summary_prompt, str)
        self.assertIn("no more than 120 words", summary_prompt)
        self.assertIn("unless a speaker explicitly corrects it", summary_prompt)
        self.assertIn("Do not infer qualifiers", summary_prompt)
        self.assertIn("not a heading or label", summary_prompt)

    def test_insight_rejects_markdown_provider_content(self) -> None:
        with (
            patch.object(
                AIGateClient,
                "_post",
                return_value=_completion("# Summary"),
            ),
            self.assertRaisesRegex(ProtocolError, "Markdown structure"),
        ):
            asyncio.run(_client().insight(_insight_request(InsightKind.SUMMARY)))

    def test_provider_content_converts_safe_inline_markdown_to_plain_text(self) -> None:
        with (
            patch.object(
                AIGateClient,
                "_post",
                return_value=_completion(
                    "Use **staging** [logs with _details_](https://example.invalid) "
                    "before `Tuesday`."
                ),
            ),
            self.assertLogs("two_x_brainz.aigate", level="INFO") as captured_logs,
        ):
            result = asyncio.run(_client().draft(_draft_request()))

        self.assertEqual(
            result.text,
            "Use staging logs with details before Tuesday.",
        )
        self.assertEqual(len(captured_logs.records), 1)
        self.assertEqual(
            captured_logs.records[0].getMessage(),
            "converted provider Markdown to plain text",
        )
        self.assertEqual(captured_logs.records[0].__dict__["output_kind"], "draft")
        self.assertEqual(
            captured_logs.records[0].__dict__["reason"],
            "markdown_to_plain_text",
        )

    def test_draft_accepts_text_at_its_character_limit(self) -> None:
        text = "x" * MAX_DRAFT_TEXT_CHARACTERS

        with patch.object(AIGateClient, "_post", return_value=_completion(text)):
            result = asyncio.run(_client().draft(_draft_request()))

        self.assertEqual(result.text, text)

    def test_draft_rejects_text_above_its_character_limit(self) -> None:
        text = "x" * (MAX_DRAFT_TEXT_CHARACTERS + 1)

        with (
            patch.object(AIGateClient, "_post", return_value=_completion(text)),
            self.assertRaisesRegex(ProtocolError, "configured limit"),
        ):
            asyncio.run(_client().draft(_draft_request()))

    def test_draft_rejects_multiline_or_markdown_provider_content(self) -> None:
        invalid_content = {
            "multiline": "First thought.\nSecond thought.",
            "bullet": "- First thought.",
            "heading": "# First thought.",
            "blockquote": "> First thought.",
            "fenced code": "```\ntext\n```",
            "inline HTML": "<strong>First thought.</strong>",
        }

        for name, content in invalid_content.items():
            with (
                self.subTest(name=name),
                patch.object(
                    AIGateClient,
                    "_post",
                    return_value=_completion(content),
                ),
                self.assertRaises(ProtocolError),
            ):
                asyncio.run(_client().draft(_draft_request()))

    def test_draft_context_places_summary_before_recent_transcript(self) -> None:
        payloads: list[dict[str, object]] = []

        def post(client: AIGateClient, payload: dict[str, object]) -> object:
            payloads.append(payload)
            return _completion("short reply")

        with patch.object(AIGateClient, "_post", new=post):
            asyncio.run(_client().draft(_draft_request_with_summary()))

        messages = require_json_array(payloads[0]["messages"])
        user_message = require_json_object(messages[1])
        content = user_message["content"]
        assert isinstance(content, str)
        self.assertLess(content.index("running summary"), content.index("remote:"))

    def test_draft_runs_allowed_tool_calls_in_parallel_then_returns_a_reply(
        self,
    ) -> None:
        maximum_active_calls = 0
        active_calls = 0

        async def run_tool_call(
            client: AIGateClient,
            call: object,
        ) -> str:
            del client, call
            nonlocal active_calls, maximum_active_calls
            active_calls += 1
            maximum_active_calls = max(maximum_active_calls, active_calls)
            await asyncio.sleep(0)
            await asyncio.sleep(0.01)
            active_calls -= 1
            return '{"results":[]}'

        tool_response = _tool_completion(
            _search_tool_call("search-1", "example technology"),
            _search_tool_call("search-2", "example project"),
            _code_tool_call("code-1", "2 + 2"),
        )
        client = _client(web_research_enabled=True)
        with (
            patch.object(
                AIGateClient,
                "_post",
                side_effect=[tool_response, _completion("short reply")],
            ) as post,
            patch.object(AIGateClient, "_run_tool_call", new=run_tool_call),
        ):
            result = asyncio.run(client.draft(_draft_request()))

        self.assertEqual(result.text, "short reply")
        self.assertEqual(maximum_active_calls, 3)
        initial_payload = post.call_args_list[0].args[0]
        tool_schemas = require_json_array(initial_payload["tools"])
        self.assertEqual(
            [
                require_json_object(require_json_object(tool)["function"])["name"]
                for tool in tool_schemas
            ],
            ["search_web", "execute_code"],
        )
        final_payload = post.call_args_list[1].args[0]
        self.assertNotIn("tools", final_payload)
        messages = require_json_array(final_payload["messages"])
        self.assertEqual(require_json_object(messages[-4])["role"], "assistant")
        self.assertEqual(require_json_object(messages[-3])["role"], "tool")
        self.assertEqual(require_json_object(messages[-1])["role"], "tool")

    def test_research_tools_are_disabled_for_insights(self) -> None:
        payloads: list[dict[str, object]] = []

        def post(client: AIGateClient, payload: dict[str, object]) -> object:
            del client
            payloads.append(payload)
            return _completion("private note")

        with patch.object(AIGateClient, "_post", new=post):
            asyncio.run(
                _client(web_research_enabled=True).insight(
                    _insight_request(InsightKind.COMMENTARY)
                )
            )

        self.assertNotIn("tools", payloads[0])

    def test_web_research_uses_the_aigate_mcp_endpoint_and_token(self) -> None:
        mcp_payload = {
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": '{"query":"example technology","results":[]}',
                    }
                ]
            }
        }
        response = _HTTPResponse(f"data: {json.dumps(mcp_payload)}\n\n".encode())
        client = _client(token="test-token", web_research_enabled=True)
        with (
            patch(
                "two_x_brainz.aigate.urlopen",
                return_value=response,
            ) as urlopen_mock,
            patch.object(
                AIGateClient,
                "_post",
                side_effect=[
                    _tool_completion(
                        _search_tool_call("search-1", "example technology")
                    ),
                    _completion("short reply"),
                ],
            ),
        ):
            result = asyncio.run(client.draft(_draft_request()))

        request = urlopen_mock.call_args.args[0]
        self.assertEqual(request.full_url, "http://aigate.example/mcp/")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(result.text, "short reply")

    def test_web_search_rejects_structured_private_identifiers(self) -> None:
        private_queries = (
            "person@example.test product",
            "https://private.example.test/project",
            "@private_handle technology",
            "product support 555 123 4567",
        )
        for query in private_queries:
            with (
                self.subTest(query=query),
                patch.object(
                    AIGateClient,
                    "_post",
                    return_value=_tool_completion(_search_tool_call("search-1", query)),
                ),
                self.assertRaisesRegex(ProtocolError, "private identifier"),
            ):
                asyncio.run(_client(web_research_enabled=True).draft(_draft_request()))

    def test_code_calculation_uses_the_aigate_mcp_allowlist(self) -> None:
        mcp_payload = {"result": {"content": [{"type": "text", "text": "4"}]}}
        response = _HTTPResponse(f"data: {json.dumps(mcp_payload)}\n\n".encode())
        client = _client(token="test-token", web_research_enabled=True)
        with (
            patch(
                "two_x_brainz.aigate.urlopen",
                return_value=response,
            ) as urlopen_mock,
            patch.object(
                AIGateClient,
                "_post",
                side_effect=[
                    _tool_completion(_code_tool_call("code-1", "2 + 2")),
                    _completion("short reply"),
                ],
            ),
        ):
            result = asyncio.run(client.draft(_draft_request()))

        request = urlopen_mock.call_args.args[0]
        assert request.data is not None
        payload = require_json_object(json.loads(request.data))
        parameters = require_json_object(payload["params"])
        arguments = require_json_object(parameters["arguments"])
        self.assertEqual(parameters["name"], "mcp_tools-execute_code")
        self.assertEqual(arguments, {"language": "python", "source": "print(2 + 2)"})
        self.assertEqual(result.text, "short reply")

    def test_code_calculation_rejects_non_arithmetic_and_resource_abuse(self) -> None:
        invalid_expressions = (
            "__import__('os').system('id')",
            "open('/etc/passwd').read()",
            "'not a number'",
            "True + 1",
            "2 ** 1000000",
            "(2 + 3) ** 2",
            "1" * 257,
            "-" * 20 + "1",
        )
        for expression in invalid_expressions:
            with (
                self.subTest(expression=expression),
                patch.object(
                    AIGateClient,
                    "_post",
                    return_value=_tool_completion(
                        _code_tool_call("code-1", expression)
                    ),
                ),
                self.assertRaisesRegex(ProtocolError, "execute_code"),
            ):
                asyncio.run(_client(web_research_enabled=True).draft(_draft_request()))


class _HTTPResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> _HTTPResponse:
        return self

    def __exit__(self, *arguments: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self._body[:size]


def _client(
    token: str | None = None,
    *,
    web_research_enabled: bool = False,
) -> AIGateClient:
    return AIGateClient(
        base_url="http://aigate.example/v1",
        model="test-model",
        token=token,
        web_research_enabled=web_research_enabled,
    )


def _draft_request() -> DraftRequest:
    return DraftRequest(
        generation_id="draft-id",
        trigger_turn_id="turn-id",
        context_revision=1,
        transcript=_transcript(),
        deadline_seconds=DEFAULT_PROVIDER_GENERATION_DEADLINE.total_seconds(),
    )


def _insight_request(kind: InsightKind) -> InsightRequest:
    return InsightRequest(
        generation_id="insight-id",
        kind=kind,
        trigger_turn_id="turn-id",
        context_revision=1,
        transcript=_transcript(),
        deadline_seconds=DEFAULT_PROVIDER_GENERATION_DEADLINE.total_seconds(),
    )


def _draft_request_with_summary() -> DraftRequest:
    return DraftRequest(
        generation_id="draft-id",
        trigger_turn_id="turn-id",
        context_revision=1,
        transcript=TranscriptSnapshot(
            revision=1,
            lines=_transcript().lines,
            running_summary="running summary",
        ),
        deadline_seconds=DEFAULT_PROVIDER_GENERATION_DEADLINE.total_seconds(),
    )


def _transcript() -> TranscriptSnapshot:
    return TranscriptSnapshot(
        revision=1,
        lines=(
            TranscriptLine(
                stream_id="remote-stream",
                speaker_role=SpeakerRole.REMOTE,
                revision=1,
                text="Could we review this?",
                is_final=True,
            ),
        ),
    )


def _completion(content: str) -> dict[str, object]:
    return {"choices": [{"message": {"content": content}}]}


def _tool_completion(*calls: dict[str, object]) -> dict[str, object]:
    return {"choices": [{"message": {"tool_calls": list(calls)}}]}


def _search_tool_call(identifier: str, query: str) -> dict[str, object]:
    return {
        "id": identifier,
        "type": "function",
        "function": {
            "name": "search_web",
            "arguments": json.dumps({"query": query}),
        },
    }


def _code_tool_call(identifier: str, expression: str) -> dict[str, object]:
    return {
        "id": identifier,
        "type": "function",
        "function": {
            "name": "execute_code",
            "arguments": json.dumps({"expression": expression}),
        },
    }
