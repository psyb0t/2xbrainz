from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import Iterator
from email.message import Message
from unittest.mock import patch

import two_x_brainz.aigate as aigate
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

_download_research_page = aigate._download_research_page  # pyright: ignore[reportPrivateUsage]
_ResearchPage = aigate._ResearchPage  # pyright: ignore[reportPrivateUsage]
_select_research_result = aigate._select_research_result  # pyright: ignore[reportPrivateUsage]


class AIGateClientTests(unittest.TestCase):
    def test_require_model_rejects_an_unconfigured_client(self) -> None:
        client = AIGateClient(
            base_url="http://aigate.example/v1",
            model=None,
            token=None,
        )

        with self.assertRaisesRegex(ConfigurationError, "AIGate model"):
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

    def test_cancelled_request_is_not_reported_as_failed(self) -> None:
        activities: list[dict[str, object]] = []
        started = asyncio.Event()

        async def request_completion(*arguments: object, **keywords: object) -> object:
            del arguments, keywords
            started.set()
            await asyncio.Event().wait()

        async def scenario() -> None:
            client = _client()
            client.activity_sink = lambda event: activities.append(dict(event))
            with patch.object(
                AIGateClient,
                "_request_completion",
                new=request_completion,
            ):
                task = asyncio.create_task(client.draft(_draft_request()))
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(scenario())

        phases = [event["phase"] for event in activities]
        self.assertIn("request_cancelled", phases)
        self.assertNotIn("request_failed", phases)

    def test_failed_request_exposes_a_bounded_reason(self) -> None:
        activities: list[dict[str, object]] = []
        client = _client()
        client.activity_sink = lambda event: activities.append(dict(event))
        with (
            patch.object(
                AIGateClient,
                "_post",
                side_effect=RemoteServiceError("AIGate returned HTTP 503"),
            ),
            self.assertRaisesRegex(RemoteServiceError, "HTTP 503"),
        ):
            asyncio.run(client.draft(_draft_request()))

        failed = next(
            event for event in activities if event["phase"] == "request_failed"
        )
        self.assertEqual(failed["error_type"], "RemoteServiceError")
        self.assertEqual(failed["error_message"], "AIGate returned HTTP 503")

    def test_streams_visible_reasoning_and_output_activity(self) -> None:
        activities: list[dict[str, object]] = []
        client = _client()
        client.streaming_enabled = True
        client.activity_sink = lambda event: activities.append(dict(event))
        response = _StreamingHTTPResponse(
            (
                b'data: {"choices":[{"delta":{"reasoning_content":"Check "}}]}\n',
                b'data: {"choices":[{"delta":{"reasoning_content":"facts."}}]}\n',
                b'data: {"choices":[{"delta":{"content":"Useful "}}]}\n',
                b'data: {"choices":[{"delta":{"content":"reply."}}]}\n',
                b"data: [DONE]\n",
            )
        )

        with (
            patch("two_x_brainz.aigate.urlopen", return_value=response),
            self.assertLogs("two_x_brainz.aigate", level="DEBUG") as captured,
        ):
            result = asyncio.run(client.draft(_draft_request()))

        self.assertEqual(result.text, "Useful reply.")
        reasoning_events = [
            event for event in activities if event["phase"] == "reasoning_streaming"
        ]
        output_events = [
            event for event in activities if event["phase"] == "output_streaming"
        ]
        self.assertEqual(reasoning_events[-1]["reasoning"], "Check facts.")
        self.assertEqual(output_events[-1]["output"], "Useful reply.")
        self.assertTrue(
            next(event for event in activities if event["phase"] == "stream_completed")[
                "reasoning_exposed"
            ]
        )
        self.assertEqual(
            len({event["flow_id"] for event in activities if "flow_id" in event}),
            1,
        )
        messages = [record.getMessage() for record in captured.records]
        self.assertIn("AIGate SSE request started", messages)
        self.assertEqual(messages.count("AIGate SSE event received"), 4)
        self.assertIn("AIGate SSE request completed", messages)
        self.assertIn("provider activity emitted", messages)
        self.assertNotIn("Useful reply", "\n".join(captured.output))

    def test_stream_normalizes_repeated_snapshots_and_special_tokens(self) -> None:
        activities: list[dict[str, object]] = []
        client = _client()
        client.streaming_enabled = True
        client.activity_sink = lambda event: activities.append(dict(event))
        response = _StreamingHTTPResponse(
            (
                _stream_event({"reasoning_content": "We"}),
                _stream_event({"reasoning_content": "We"}),
                _stream_event({"reasoning_content": "We need"}),
                _stream_event({"reasoning_content": "<unk>"}),
                _stream_event({"reasoning_content": "We need facts."}),
                _stream_event({"content": "Use"}),
                _stream_event({"content": "Use"}),
                _stream_event({"content": "Use the facts."}),
                b"data: [DONE]\n",
            )
        )

        with patch("two_x_brainz.aigate.urlopen", return_value=response):
            result = asyncio.run(client.draft(_draft_request()))

        self.assertEqual(result.text, "Use the facts.")
        completed = next(
            event for event in activities if event["phase"] == "stream_completed"
        )
        self.assertEqual(completed["reasoning"], "We need facts.")
        self.assertNotIn("<unk>", str(completed))

    def test_streams_fragmented_tool_call_and_exact_bounded_result(self) -> None:
        activities: list[dict[str, object]] = []
        client = _client()
        client.streaming_enabled = True
        client.web_research_enabled = True
        client.activity_sink = lambda event: activities.append(dict(event))
        tool_stream = _StreamingHTTPResponse(
            (
                _stream_event(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "search-",
                                "function": {
                                    "name": "research_",
                                    "arguments": '{"query":"example ',
                                },
                            }
                        ]
                    }
                ),
                _stream_event(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "1",
                                "function": {
                                    "name": "web",
                                    "arguments": 'technology"}',
                                },
                            }
                        ]
                    }
                ),
                b"data: [DONE]\n",
            )
        )
        output_stream = _StreamingHTTPResponse(
            (
                _stream_event({"content": "Grounded reply."}),
                b"data: [DONE]\n",
            )
        )

        async def run_tool_call(*arguments: object) -> str:
            del arguments
            return "bounded exact result"

        with (
            patch(
                "two_x_brainz.aigate.urlopen",
                side_effect=[tool_stream, output_stream],
            ),
            patch.object(AIGateClient, "_run_tool_call", new=run_tool_call),
        ):
            result = asyncio.run(client.draft(_draft_request()))

        self.assertEqual(result.text, "Grounded reply.")
        started = next(
            event for event in activities if event["phase"] == "tool_started"
        )
        completed = next(
            event for event in activities if event["phase"] == "tool_completed"
        )
        self.assertEqual(started["tool"], "research_web")
        self.assertEqual(
            started["tool_input"],
            {"query": "example technology", "num_results": 5},
        )
        self.assertEqual(completed["tool_result"], "bounded exact result")
        self.assertEqual(started["flow_id"], completed["flow_id"])

    def test_stream_rejects_an_oversized_response(self) -> None:
        client = _client()
        client.streaming_enabled = True
        response = _StreamingHTTPResponse((b"x" * (MAX_PROVIDER_RESPONSE_BYTES + 1),))

        with (
            patch("two_x_brainz.aigate.urlopen", return_value=response),
            self.assertRaisesRegex(RemoteServiceError, "stream exceeds"),
        ):
            asyncio.run(client.draft(_draft_request()))

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
            allowed_fetch_urls: frozenset[str],
        ) -> str:
            del client, call, allowed_fetch_urls
            nonlocal active_calls, maximum_active_calls
            active_calls += 1
            maximum_active_calls = max(maximum_active_calls, active_calls)
            await asyncio.sleep(0)
            await asyncio.sleep(0.01)
            active_calls -= 1
            return '{"results":[]}'

        tool_response = _tool_completion(
            _research_tool_call("research-1", "example technology"),
            _research_tool_call("research-2", "example project"),
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
            ["research_web", "execute_code"],
        )
        followup_payload = post.call_args_list[1].args[0]
        self.assertIn("tools", followup_payload)
        messages = require_json_array(followup_payload["messages"])
        self.assertEqual(require_json_object(messages[-4])["role"], "assistant")
        self.assertEqual(require_json_object(messages[-3])["role"], "tool")
        self.assertEqual(require_json_object(messages[-1])["role"], "tool")

    def test_draft_researches_and_reads_a_matching_public_result(self) -> None:
        page_url = "https://example.com/project"
        search_payload = {
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "results": [
                                    {
                                        "title": "Example project",
                                        "url": page_url,
                                        "snippet": "Public project information.",
                                    }
                                ]
                            }
                        ),
                    }
                ]
            }
        }
        activities: list[dict[str, object]] = []
        client = _client(web_research_enabled=True)
        client.activity_sink = lambda event: activities.append(dict(event))
        with (
            patch.object(
                AIGateClient,
                "_post",
                side_effect=[
                    _tool_completion(
                        _research_tool_call("research-1", "example project")
                    ),
                    _completion("Grounded reply."),
                ],
            ) as post,
            patch.object(
                AIGateClient,
                "_post_mcp",
                return_value=search_payload,
            ) as post_mcp,
            patch(
                "two_x_brainz.aigate._download_research_page",
                return_value=_ResearchPage(
                    markdown="Readable project details.",
                    links=(),
                ),
            ) as download,
        ):
            result = asyncio.run(client.draft(_draft_request()))

        self.assertEqual(result.text, "Grounded reply.")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post_mcp.call_args.args[0], "mcp_tools-search_web")
        download.assert_called_once_with(page_url)
        self.assertEqual(
            [
                event["phase"]
                for event in activities
                if isinstance(event.get("phase"), str)
                and str(event["phase"]).startswith("tool_")
            ],
            ["tool_started", "tool_completed"],
        )
        completed = next(
            event for event in activities if event["phase"] == "tool_completed"
        )
        tool_result = require_json_object(json.loads(str(completed["tool_result"])))
        self.assertEqual(tool_result["status"], "page_fetched")
        page = require_json_object(tool_result["page"])
        self.assertEqual(page["content_format"], "markdown")
        self.assertEqual(page["content"], "Readable project details.")

    def test_research_follows_a_discovered_documentation_url_in_the_next_round(
        self,
    ) -> None:
        index_url = "https://example.com/project"
        documentation_url = "https://example.com/docs/routing.md"
        search_payload = {
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "results": [
                                    {
                                        "title": "Example project",
                                        "url": index_url,
                                        "snippet": "Public project information.",
                                    }
                                ]
                            }
                        ),
                    }
                ]
            }
        }
        client = _client(web_research_enabled=True)
        with (
            patch.object(
                AIGateClient,
                "_post",
                side_effect=[
                    _tool_completion(
                        _research_tool_call("research-index", "example project")
                    ),
                    _tool_completion(
                        _research_url_tool_call("research-doc", documentation_url)
                    ),
                    _completion("Grounded in the routing documentation."),
                ],
            ) as post,
            patch.object(
                AIGateClient,
                "_post_mcp",
                return_value=search_payload,
            ),
            patch(
                "two_x_brainz.aigate._download_research_page",
                side_effect=[
                    _ResearchPage(
                        markdown=(
                            "| Document | Purpose |\n"
                            "| --- | --- |\n"
                            "| [Routing](docs/routing.md) | Request routing details |"
                        ),
                        links=({"label": "Routing", "url": documentation_url},),
                    ),
                    _ResearchPage(
                        markdown=(
                            "Routing validates a request before provider dispatch."
                        ),
                        links=(),
                    ),
                ],
            ) as download,
        ):
            result = asyncio.run(client.draft(_draft_request()))

        self.assertEqual(result.text, "Grounded in the routing documentation.")
        self.assertEqual(post.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in download.call_args_list],
            [index_url, documentation_url],
        )

    def test_fetch_rejects_a_url_not_returned_by_search(self) -> None:
        activities: list[dict[str, object]] = []
        client = _client(web_research_enabled=True)
        client.activity_sink = lambda event: activities.append(dict(event))
        with (
            patch.object(
                AIGateClient,
                "_post",
                side_effect=[
                    _tool_completion(
                        _fetch_tool_call("fetch-1", "http://169.254.169.254/latest")
                    ),
                    _completion("Safe fallback."),
                ],
            ),
            patch.object(AIGateClient, "_post_mcp") as post_mcp,
        ):
            result = asyncio.run(client.draft(_draft_request()))

        self.assertEqual(result.text, "Safe fallback.")
        post_mcp.assert_not_called()
        failed = next(event for event in activities if event["phase"] == "tool_failed")
        self.assertEqual(failed["tool"], "fetch_url")

    def test_fetch_rejects_a_non_public_url_returned_by_search(self) -> None:
        metadata_url = "http://169.254.169.254/latest/meta-data/"
        search_payload = {
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "results": [
                                    {
                                        "title": "Host metadata",
                                        "url": metadata_url,
                                        "snippet": "Must never be fetched.",
                                    }
                                ]
                            }
                        ),
                    }
                ]
            }
        }
        activities: list[dict[str, object]] = []
        client = _client(web_research_enabled=True)
        client.activity_sink = lambda event: activities.append(dict(event))
        with (
            patch.object(
                AIGateClient,
                "_post",
                side_effect=[
                    _tool_completion(_search_tool_call("search-1", "host metadata")),
                    _tool_completion(_fetch_tool_call("fetch-1", metadata_url)),
                    _completion("Safe fallback."),
                ],
            ),
            patch.object(
                AIGateClient,
                "_post_mcp",
                return_value=search_payload,
            ) as post_mcp,
            patch(
                "two_x_brainz.aigate.socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("169.254.169.254", 80))],
            ),
        ):
            result = asyncio.run(client.draft(_draft_request()))

        self.assertEqual(result.text, "Safe fallback.")
        self.assertEqual(post_mcp.call_count, 1)
        failed = next(event for event in activities if event["phase"] == "tool_failed")
        self.assertEqual(failed["tool"], "fetch_url")
        tool_result = require_json_object(json.loads(str(failed["tool_result"])))
        self.assertEqual(tool_result["error"], "tool unavailable")
        self.assertEqual(tool_result["error_type"], "ProtocolError")
        self.assertIn("non-public", str(tool_result["reason"]))

    def test_fetch_rejects_malformed_or_credential_bearing_urls(self) -> None:
        invalid_urls = (
            "file:///etc/passwd",
            "https://user:password@example.test/private",
            "https://example.test/" + "x" * 2_000,
        )
        for url in invalid_urls:
            with (
                self.subTest(url=url[:80]),
                patch.object(
                    AIGateClient,
                    "_post",
                    return_value=_tool_completion(_fetch_tool_call("fetch-1", url)),
                ),
                self.assertRaises(ProtocolError),
            ):
                asyncio.run(_client(web_research_enabled=True).draft(_draft_request()))

    def test_research_result_selector_requires_a_clear_subject_match(self) -> None:
        exact_result = json.dumps(
            {
                "query": "psyb0t aigate github",
                "results": [
                    {
                        "title": "psyb0t/aigate",
                        "url": "https://github.com/psyb0t/aigate",
                        "snippet": "A unified AI gateway.",
                    },
                    {
                        "title": "An unrelated AI gateway",
                        "url": "https://example.com/gateway",
                        "snippet": "A different project.",
                    },
                ],
            }
        )
        selected = _select_research_result(
            exact_result,
            "psyb0t aigate github",
        )

        self.assertEqual(
            selected,
            {
                "title": "psyb0t/aigate",
                "url": "https://github.com/psyb0t/aigate",
            },
        )

        ambiguous_result = json.dumps(
            {
                "query": "airgate workflows single endpoint",
                "results": [
                    {
                        "title": "Single workflow examples",
                        "url": "https://github.blog/example",
                        "snippet": "Workflow guidance.",
                    },
                    {
                        "title": "air-gate",
                        "url": "https://github.com/example/air-gate",
                        "snippet": "An unrelated project.",
                    },
                ],
            }
        )
        self.assertIsNone(
            _select_research_result(
                ambiguous_result,
                "airgate workflows single endpoint",
            )
        )

    def test_research_page_extracts_main_text_and_drops_boilerplate(self) -> None:
        body = b"""\
<html><body>
  <nav>Home Pricing Login</nav>
  <main><article>
    <h1>Example project</h1>
    <p>This project provides one gateway for several useful AI services.</p>
    <p>Applications send requests through a documented public interface.</p>
  </article></main>
  <footer>Copyright and cookie settings</footer>
</body></html>
"""
        response = _HTTPResponse(
            body,
            url="https://example.com/project",
            content_type="text/html; charset=utf-8",
        )
        with (
            patch(
                "two_x_brainz.aigate.socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
            ),
            patch(
                "two_x_brainz.aigate.build_opener",
                return_value=_Opener(response),
            ),
        ):
            text = _download_research_page("https://example.com/project")

        self.assertIn("Example project", text.markdown)
        self.assertIn("documented public interface", text.markdown)
        self.assertNotIn("Pricing Login", text.markdown)
        self.assertNotIn("cookie settings", text.markdown)

    def test_research_page_preserves_markdown_tables_and_resolves_links(self) -> None:
        body = b"""\
<html><body><article>
  <p>Documentation index for the project and its public request flow.</p>
  <table>
    <tr><th>Document</th><th>Purpose</th></tr>
    <tr><td><a href="/docs/routing.md">Routing</a></td><td>Request routing</td></tr>
  </table>
</article></body></html>
"""
        response = _HTTPResponse(
            body,
            url="https://example.com/project/",
            content_type="text/html; charset=utf-8",
        )
        with (
            patch(
                "two_x_brainz.aigate.socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
            ),
            patch(
                "two_x_brainz.aigate.build_opener",
                return_value=_Opener(response),
            ),
        ):
            page = _download_research_page("https://example.com/project/")

        self.assertIn("| Document | Purpose |", page.markdown)
        self.assertIn("[Routing](https://example.com/docs/routing.md)", page.markdown)
        self.assertEqual(
            page.links,
            (
                {
                    "label": "Routing",
                    "url": "https://example.com/docs/routing.md",
                },
            ),
        )

    def test_research_page_reads_raw_markdown_and_discovers_relative_docs(self) -> None:
        body = b"""\
# Project documentation

| Guide | Purpose |
| --- | --- |
| [Operations](operations.md) | Runtime behavior |
"""
        response = _HTTPResponse(
            body,
            url="https://example.com/docs/index.md",
            content_type="text/markdown; charset=utf-8",
        )
        with (
            patch(
                "two_x_brainz.aigate.socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
            ),
            patch(
                "two_x_brainz.aigate.build_opener",
                return_value=_Opener(response),
            ),
        ):
            page = _download_research_page("https://example.com/docs/index.md")

        self.assertIn("| [Operations](operations.md) |", page.markdown)
        self.assertEqual(
            page.links[0]["url"],
            "https://example.com/docs/operations.md",
        )

    def test_research_page_rejects_private_oversized_and_non_html_inputs(
        self,
    ) -> None:
        with self.assertRaisesRegex(ProtocolError, "non-public"):
            _download_research_page("http://169.254.169.254/latest")

        cases = (
            (
                _HTTPResponse(
                    b"x" * (MAX_PROVIDER_RESPONSE_BYTES + 1),
                    url="https://example.com/large",
                    content_type="text/html",
                ),
                "size limit",
            ),
            (
                _HTTPResponse(
                    b'{"data":true}',
                    url="https://example.com/data",
                    content_type="application/json",
                ),
                "not HTML or Markdown",
            ),
        )
        for response, expected_error in cases:
            with (
                self.subTest(expected_error=expected_error),
                patch(
                    "two_x_brainz.aigate.socket.getaddrinfo",
                    return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
                ),
                patch(
                    "two_x_brainz.aigate.build_opener",
                    return_value=_Opener(response),
                ),
                self.assertRaisesRegex(ProtocolError, expected_error),
            ):
                _download_research_page(response.geturl())

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
    def __init__(
        self,
        body: bytes,
        status: int = 200,
        *,
        url: str = "https://example.com/",
        content_type: str = "application/json",
    ) -> None:
        self.status = status
        self._body = body
        self._url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self) -> _HTTPResponse:
        return self

    def __exit__(self, *arguments: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self._body[:size]

    def geturl(self) -> str:
        return self._url


class _Opener:
    def __init__(self, response: _HTTPResponse) -> None:
        self._response = response

    def open(self, request: object, timeout: float) -> _HTTPResponse:
        del request, timeout
        return self._response


class _StreamingHTTPResponse:
    def __init__(self, lines: tuple[bytes, ...]) -> None:
        self._lines = lines

    def __enter__(self) -> _StreamingHTTPResponse:
        return self

    def __exit__(self, *arguments: object) -> None:
        return None

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._lines)


def _stream_event(delta: dict[str, object]) -> bytes:
    payload = {"choices": [{"delta": delta}]}
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n".encode()


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


def _research_tool_call(identifier: str, query: str) -> dict[str, object]:
    return {
        "id": identifier,
        "type": "function",
        "function": {
            "name": "research_web",
            "arguments": json.dumps({"query": query}),
        },
    }


def _research_url_tool_call(identifier: str, url: str) -> dict[str, object]:
    return {
        "id": identifier,
        "type": "function",
        "function": {
            "name": "research_web",
            "arguments": json.dumps({"url": url}),
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


def _fetch_tool_call(identifier: str, url: str) -> dict[str, object]:
    return {
        "id": identifier,
        "type": "function",
        "function": {
            "name": "fetch_url",
            "arguments": json.dumps({"url": url}),
        },
    }
