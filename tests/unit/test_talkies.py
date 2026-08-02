from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from two_x_brainz.audio import WavFixture
from two_x_brainz.constants import MAX_PROVIDER_RESPONSE_BYTES
from two_x_brainz.contracts import (
    ASRStreamStats,
    SpeakerRole,
    TranscriptEvent,
    TranscriptEventType,
    WordTiming,
)
from two_x_brainz.errors import ConfigurationError, ProtocolError, RemoteServiceError
from two_x_brainz.talkies import (
    BatchResponseFormat,
    TalkiesClient,
    TalkiesStreamConfig,
    UtteranceReconciler,
    batch_url,
    models_url,
    parse_batch_transcription,
    parse_model_inventory,
    parse_talkies_event,
)


class TalkiesEventTests(unittest.TestCase):
    def test_normalizes_partial_with_word_timing(self) -> None:
        event = parse_talkies_event(
            message=(
                '{"type":"partial","revision":3,"text":"hello",'
                '"confidence":0.75,"language":"en",'
                '"words":[{"word":"hello","start":0.1,"end":0.4,'
                '"confidence":0.98}],"audio_seconds":1.28,"is_final":false}'
            ),
            session_id="session",
            stream_id="remote-stream",
            speaker_role=SpeakerRole.REMOTE,
            model="nemotron-3.5-asr-0.6b",
        )

        self.assertIsNotNone(event)
        assert event is not None
        assert not isinstance(event, ASRStreamStats)
        self.assertEqual(event.source_event_type, TranscriptEventType.PARTIAL)
        self.assertEqual(event.words[0].start_ms, 100)
        self.assertEqual(event.words[0].confidence, 0.98)
        self.assertEqual(event.started_at_ms, 100)
        self.assertEqual(event.ended_at_ms, 400)
        self.assertEqual(event.confidence, 0.75)
        self.assertEqual(event.language, "en")
        self.assertFalse(event.is_final)

    def test_rejects_invalid_word_metadata_before_normalizing_event(self) -> None:
        invalid_messages = (
            '{"type":"partial","revision":3,"text":"hello",'
            '"words":[{"word":"hello","start":0.4,"end":0.1}],'
            '"audio_seconds":1.28}',
            '{"type":"partial","revision":3,"text":"hello",'
            '"words":[{"word":"hello","confidence":1.01}],'
            '"audio_seconds":1.28}',
            '{"type":"partial","revision":3,"text":"hello",'
            '"confidence":NaN,"audio_seconds":1.28}',
            '{"type":"partial","revision":3,"text":"hello",'
            '"language":"   ","audio_seconds":1.28}',
        )

        for message in invalid_messages:
            with self.subTest(message=message), self.assertRaises(ProtocolError):
                parse_talkies_event(
                    message=message,
                    session_id="session",
                    stream_id="remote-stream",
                    speaker_role=SpeakerRole.REMOTE,
                    model="nemotron-3.5-asr-0.6b",
                )

    def test_rejects_binary_server_messages(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_talkies_event(
                message=b"not-json",
                session_id="session",
                stream_id="remote-stream",
                speaker_role=SpeakerRole.REMOTE,
                model="nemotron-3.5-asr-0.6b",
            )

    def test_normalizes_terminal_stream_statistics(self) -> None:
        event = parse_talkies_event(
            message='{"type":"stats","audio_seconds":1.28,"frames":64,"canceled":false}',
            session_id="session",
            stream_id="remote-stream",
            speaker_role=SpeakerRole.REMOTE,
            model="nemotron-3.5-asr-0.6b",
        )

        self.assertIsInstance(event, ASRStreamStats)
        assert isinstance(event, ASRStreamStats)
        self.assertEqual(event.audio_seconds, 1.28)
        self.assertEqual(event.frames, 64)
        self.assertFalse(event.canceled)

    def test_rejects_invalid_terminal_stream_statistics(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "frames"):
            parse_talkies_event(
                message=(
                    '{"type":"stats","audio_seconds":1.28,'
                    '"frames":true,"canceled":false}'
                ),
                session_id="session",
                stream_id="remote-stream",
                speaker_role=SpeakerRole.REMOTE,
                model="nemotron-3.5-asr-0.6b",
            )

    def test_reconciles_timestamped_fragments_into_a_complete_final(self) -> None:
        reconciler = UtteranceReconciler()

        first = reconciler.apply(
            _event(
                revision=1,
                text="first",
                words=(_word("first", 0, 100),),
            )
        )
        final = reconciler.apply(
            _event(
                revision=2,
                text="second",
                words=(_word("second", 100, 200),),
                event_type=TranscriptEventType.FINAL,
            )
        )

        self.assertEqual(first.text, "first")
        self.assertEqual(final.text, "first second")
        self.assertEqual(tuple(word.word for word in final.words), ("first", "second"))

    def test_reconciliation_deduplicates_full_hypotheses_and_resets_after_final(
        self,
    ) -> None:
        reconciler = UtteranceReconciler()
        words = (_word("repeat", 0, 100), _word("word", 100, 200))

        repeated = reconciler.apply(_event(revision=1, text="repeat word", words=words))
        final = reconciler.apply(
            _event(
                revision=2,
                text="repeat word",
                words=words,
                event_type=TranscriptEventType.FINAL,
            )
        )
        next_turn = reconciler.apply(
            _event(
                revision=3,
                text="next",
                words=(_word("next", 0, 100),),
            )
        )

        self.assertEqual(repeated.text, "repeat word")
        self.assertEqual(final.text, "repeat word")
        self.assertEqual(next_turn.text, "next")

    def test_reconciliation_promotes_last_complete_partial_on_empty_final(self) -> None:
        reconciler = UtteranceReconciler()
        partial = reconciler.apply(
            _event(
                revision=1,
                text="recognized interview question",
                words=(
                    _word("recognized", 0, 100),
                    _word("interview", 100, 200),
                    _word("question", 200, 300),
                ),
            )
        )
        final = reconciler.apply(
            _event(
                revision=2,
                text="",
                words=(),
                event_type=TranscriptEventType.FINAL,
            )
        )

        self.assertEqual(final.text, partial.text)
        self.assertEqual(final.words, partial.words)
        self.assertEqual(final.started_at_ms, partial.started_at_ms)
        self.assertEqual(final.ended_at_ms, partial.ended_at_ms)

    def test_reconciliation_keeps_untimestamped_text_without_ambiguous_deduplication(
        self,
    ) -> None:
        reconciler = UtteranceReconciler()
        event = _event(
            revision=1,
            text="repeat repeat",
            words=(WordTiming("repeat", None, None),),
        )

        self.assertEqual(reconciler.apply(event), event)


class TalkiesBatchContractTests(unittest.TestCase):
    def test_derives_file_endpoint_from_secure_stream_url(self) -> None:
        self.assertEqual(
            batch_url("wss://talkies.example/v1/audio/transcriptions/stream"),
            "https://talkies.example/v1/audio/transcriptions",
        )

    def test_preserves_proxy_prefix_for_file_endpoint(self) -> None:
        self.assertEqual(
            batch_url("wss://aigate.example/talkies/v1/audio/transcriptions/stream"),
            "https://aigate.example/talkies/v1/audio/transcriptions",
        )

    def test_accepts_standard_json_transcription_shape(self) -> None:
        result = parse_batch_transcription(
            {"text": "fixture transcript"},
            BatchResponseFormat.JSON,
        )

        self.assertEqual(result.text, "fixture transcript")
        self.assertIsNone(result.duration_seconds)
        self.assertIsNone(result.segment_count)

    def test_accepts_verbose_json_transcription_shape(self) -> None:
        result = parse_batch_transcription(
            {
                "task": "transcribe",
                "language": "en",
                "duration": 1.25,
                "text": "fixture transcript",
                "segments": [{"start": 0.0, "end": 1.25, "text": "fixture transcript"}],
                "words": [{"start": 0.0, "end": 1.25, "word": "fixture"}],
            },
            BatchResponseFormat.VERBOSE_JSON,
        )

        self.assertEqual(result.duration_seconds, 1.25)
        self.assertEqual(result.segment_count, 1)
        self.assertEqual(result.word_count, 1)

    def test_rejects_verbose_json_with_invalid_segment_order(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "end precedes"):
            parse_batch_transcription(
                {
                    "task": "transcribe",
                    "language": "en",
                    "duration": 1.25,
                    "text": "fixture transcript",
                    "segments": [
                        {"start": 1.25, "end": 0.0, "text": "fixture transcript"}
                    ],
                    "words": [],
                },
                BatchResponseFormat.VERBOSE_JSON,
            )

    def test_rejects_malformed_file_response_before_rendering(self) -> None:
        with (
            patch(
                "two_x_brainz.talkies.urlopen",
                return_value=_HTTPResponse(b"not-json"),
            ),
            self.assertRaisesRegex(ProtocolError, "invalid JSON"),
        ):
            asyncio.run(
                _file_client().transcribe_file(_fixture(), BatchResponseFormat.JSON)
            )

    def test_rejects_oversized_file_response_before_parsing(self) -> None:
        oversized_response = b"x" * (MAX_PROVIDER_RESPONSE_BYTES + 1)
        with (
            patch(
                "two_x_brainz.talkies.urlopen",
                return_value=_HTTPResponse(oversized_response),
            ),
            self.assertRaisesRegex(ProtocolError, "exceeds size limit"),
        ):
            asyncio.run(
                _file_client().transcribe_file(_fixture(), BatchResponseFormat.JSON)
            )


class TalkiesModelInventoryTests(unittest.TestCase):
    def test_derives_model_inventory_from_secure_stream_url(self) -> None:
        self.assertEqual(
            models_url("wss://talkies.example/v1/audio/transcriptions/stream"),
            "https://talkies.example/v1/models",
        )

    def test_preserves_proxy_prefix_for_model_inventory(self) -> None:
        self.assertEqual(
            models_url("wss://aigate.example/talkies/v1/audio/transcriptions/stream"),
            "https://aigate.example/talkies/v1/models",
        )

    def test_rejects_url_without_native_stream_suffix(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "native streaming endpoint"):
            models_url("ws://talkies.example/other")

    def test_accepts_model_inventory_in_any_order(self) -> None:
        model_ids = parse_model_inventory(
            {"data": [{"id": "other-model"}, {"id": "fixture-model"}]}
        )

        self.assertEqual(model_ids, frozenset({"fixture-model", "other-model"}))

    def test_rejects_empty_duplicate_and_malformed_inventory_entries(self) -> None:
        invalid_payloads: tuple[dict[str, object], ...] = (
            {"data": []},
            {"data": [{"id": "fixture-model"}, {"id": "fixture-model"}]},
            {"data": [{"id": "  "}]},
            {"data": [{"id": 1}]},
            {"data": ["fixture-model"]},
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ProtocolError):
                parse_model_inventory(payload)

    def test_preflight_uses_bearer_auth_and_accepts_configured_model(self) -> None:
        response = _HTTPResponse(b'{"data":[{"id":"fixture-model"}]}')
        with patch(
            "two_x_brainz.talkies.urlopen", return_value=response
        ) as urlopen_mock:
            asyncio.run(_file_client(token="test-token").verify_configured_model())

        request = urlopen_mock.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "http://talkies:8000/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")

    def test_preflight_rejects_unavailable_model_before_transport(self) -> None:
        response = _HTTPResponse(b'{"data":[{"id":"other-model"}]}')
        with (
            patch("two_x_brainz.talkies.urlopen", return_value=response),
            self.assertRaisesRegex(RemoteServiceError, "not available"),
        ):
            asyncio.run(_file_client().verify_configured_model())


class _HTTPResponse:
    def __init__(self, body: bytes) -> None:
        self.status = 200
        self._body = body

    def __enter__(self) -> _HTTPResponse:
        return self

    def __exit__(self, *arguments: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self._body[:size]


def _event(
    *,
    revision: int,
    text: str,
    words: tuple[WordTiming, ...],
    event_type: TranscriptEventType = TranscriptEventType.PARTIAL,
) -> TranscriptEvent:
    return TranscriptEvent(
        session_id="session",
        stream_id="remote-stream",
        utterance_id=f"remote-stream:{revision}",
        revision=revision,
        speaker_role=SpeakerRole.REMOTE,
        source_event_type=event_type,
        asr_model="fixture-model",
        text=text,
        is_final=event_type is TranscriptEventType.FINAL,
        audio_seconds=0.02,
        words=words,
    )


def _word(word: str, start_ms: int, end_ms: int) -> WordTiming:
    return WordTiming(word, start_ms, end_ms)


def _file_client(token: str | None = None) -> TalkiesClient:
    return TalkiesClient(
        TalkiesStreamConfig(
            url="ws://talkies:8000/v1/audio/transcriptions/stream",
            model="fixture-model",
            token=token,
        )
    )


def _fixture() -> WavFixture:
    return WavFixture(wav_bytes=b"fixture", pcm16le=b"", duration_seconds=0.0)
