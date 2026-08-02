from __future__ import annotations

import asyncio
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

    def test_draft_rejects_empty_visible_content(self) -> None:
        with (
            patch.object(AIGateClient, "_post", return_value=_completion("")),
            self.assertRaisesRegex(ProtocolError, "non-empty text"),
        ):
            asyncio.run(_client().draft(_draft_request()))

    def test_draft_wraps_a_transport_timeout(self) -> None:
        with (
            patch("two_x_brainz.aigate.urlopen", side_effect=TimeoutError),
            self.assertRaisesRegex(RemoteServiceError, "timed out"),
        ):
            asyncio.run(_client().draft(_draft_request()))

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


def _client(token: str | None = None) -> AIGateClient:
    return AIGateClient(
        base_url="http://aigate.example/v1",
        model="test-model",
        token=token,
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
