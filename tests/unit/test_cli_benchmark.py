from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from two_x_brainz.aigate import AIGateClient
from two_x_brainz.benchmark import ASRBenchmarkReport, NativeStreamBenchmark
from two_x_brainz.cli import main
from two_x_brainz.config import Settings
from two_x_brainz.contracts import SpeakerRole
from two_x_brainz.errors import CaptureError, RemoteServiceError


class CLIBenchmarkTests(unittest.TestCase):
    def test_benchmark_emits_fixed_safe_json_report(self) -> None:
        report = ASRBenchmarkReport(
            model="fixture-model",
            source_audio_seconds=2.4,
            native_elapsed_seconds=2.5,
            native_streams=(
                NativeStreamBenchmark(
                    stream_id="fixture-user-stream",
                    speaker_role=SpeakerRole.USER,
                    event_types=("partial", "final"),
                    frames=120,
                    audio_seconds=2.4,
                    word_error_rate=0.25,
                ),
                NativeStreamBenchmark(
                    stream_id="fixture-remote-stream",
                    speaker_role=SpeakerRole.REMOTE,
                    event_types=("partial", "final"),
                    frames=120,
                    audio_seconds=2.4,
                    word_error_rate=0.25,
                ),
            ),
            draft_elapsed_seconds=None,
            batch_json_elapsed_seconds=0.1,
            batch_json_word_error_rate=0.0,
            batch_verbose_json_elapsed_seconds=0.2,
            batch_verbose_json_word_error_rate=0.0,
            verbose_segment_count=1,
            verbose_word_count=1,
        )
        output = io.StringIO()
        settings = _settings()
        with (
            patch.object(
                sys,
                "argv",
                ["2xbrainz", "benchmark", "--audio", "/fixture/audio.wav"],
            ),
            patch(
                "two_x_brainz.cli.Settings.from_environment",
                return_value=settings,
            ),
            patch("two_x_brainz.cli.configure_logging"),
            patch(
                "two_x_brainz.cli.run_asr_benchmark",
                new_callable=AsyncMock,
                return_value=report,
            ) as benchmark_mock,
            patch("sys.stdout", output),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        benchmark_mock.assert_awaited_once_with(
            settings,
            Path("/fixture/audio.wav"),
            None,
            None,
        )
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "schema_version": 1,
                "kind": "asr_benchmark",
                "model": "fixture-model",
                "source_audio_seconds": 2.4,
                "native_elapsed_seconds": 2.5,
                "draft_elapsed_seconds": None,
                "native_streams": [
                    {
                        "stream_id": "fixture-user-stream",
                        "speaker_role": "user",
                        "event_types": ["partial", "final"],
                        "frames": 120,
                        "audio_seconds": 2.4,
                        "word_error_rate": 0.25,
                    },
                    {
                        "stream_id": "fixture-remote-stream",
                        "speaker_role": "remote",
                        "event_types": ["partial", "final"],
                        "frames": 120,
                        "audio_seconds": 2.4,
                        "word_error_rate": 0.25,
                    },
                ],
                "batch_json_elapsed_seconds": 0.1,
                "batch_json_word_error_rate": 0.0,
                "batch_verbose_json_elapsed_seconds": 0.2,
                "batch_verbose_json_word_error_rate": 0.0,
                "verbose_segment_count": 1,
                "verbose_word_count": 1,
            },
        )

    def test_benchmark_with_draft_passes_a_configured_aigate_client(self) -> None:
        report = ASRBenchmarkReport(
            model="fixture-model",
            source_audio_seconds=2.4,
            native_elapsed_seconds=2.5,
            native_streams=(),
            draft_elapsed_seconds=0.3,
            batch_json_elapsed_seconds=0.1,
            batch_json_word_error_rate=None,
            batch_verbose_json_elapsed_seconds=0.2,
            batch_verbose_json_word_error_rate=None,
            verbose_segment_count=1,
            verbose_word_count=1,
        )
        settings = _settings(reply_model="draft-model")
        with (
            patch.object(
                sys,
                "argv",
                [
                    "2xbrainz",
                    "benchmark",
                    "--audio",
                    "/fixture/audio.wav",
                    "--with-draft",
                ],
            ),
            patch(
                "two_x_brainz.cli.Settings.from_environment",
                return_value=settings,
            ),
            patch("two_x_brainz.cli.configure_logging"),
            patch.object(AIGateClient, "require_model") as require_model,
            patch(
                "two_x_brainz.cli.run_asr_benchmark",
                new_callable=AsyncMock,
                return_value=report,
            ) as benchmark_mock,
            patch("sys.stdout", io.StringIO()),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        require_model.assert_called_once()
        benchmark_call = benchmark_mock.await_args
        self.assertIsNotNone(benchmark_call)
        assert benchmark_call is not None
        supplied_provider = benchmark_call.args[2]
        self.assertIsInstance(supplied_provider, AIGateClient)
        self.assertEqual(supplied_provider.model, "draft-model")

    def test_benchmark_accepts_a_finite_model_override(self) -> None:
        report = ASRBenchmarkReport(
            model="override-model",
            source_audio_seconds=2.4,
            native_elapsed_seconds=2.5,
            native_streams=(),
            draft_elapsed_seconds=None,
            batch_json_elapsed_seconds=0.1,
            batch_json_word_error_rate=None,
            batch_verbose_json_elapsed_seconds=0.2,
            batch_verbose_json_word_error_rate=None,
            verbose_segment_count=0,
            verbose_word_count=0,
        )
        with (
            patch.object(
                sys,
                "argv",
                [
                    "2xbrainz",
                    "benchmark",
                    "--audio",
                    "/fixture/audio.wav",
                    "--model",
                    "override-model",
                ],
            ),
            patch(
                "two_x_brainz.cli.Settings.from_environment",
                return_value=_settings(),
            ),
            patch("two_x_brainz.cli.configure_logging"),
            patch(
                "two_x_brainz.cli.run_asr_benchmark",
                new_callable=AsyncMock,
                return_value=report,
            ) as benchmark_mock,
            patch("sys.stdout", io.StringIO()),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        benchmark_call = benchmark_mock.await_args
        assert benchmark_call is not None
        self.assertEqual(benchmark_call.args[0].talkies_model, "override-model")

    def test_benchmark_reference_file_is_forwarded_without_entering_output(
        self,
    ) -> None:
        report = ASRBenchmarkReport(
            model="fixture-model",
            source_audio_seconds=2.4,
            native_elapsed_seconds=2.5,
            native_streams=(),
            draft_elapsed_seconds=None,
            batch_json_elapsed_seconds=0.1,
            batch_json_word_error_rate=0.0,
            batch_verbose_json_elapsed_seconds=0.2,
            batch_verbose_json_word_error_rate=0.0,
            verbose_segment_count=1,
            verbose_word_count=1,
        )
        output = io.StringIO()
        settings = _settings()
        with (
            patch.object(
                sys,
                "argv",
                [
                    "2xbrainz",
                    "benchmark",
                    "--audio",
                    "/fixture/audio.wav",
                    "--reference-file",
                    "/fixture/reference.txt",
                ],
            ),
            patch(
                "two_x_brainz.cli.Settings.from_environment",
                return_value=settings,
            ),
            patch("two_x_brainz.cli.configure_logging"),
            patch(
                "two_x_brainz.cli.run_asr_benchmark",
                new_callable=AsyncMock,
                return_value=report,
            ) as benchmark_mock,
            patch("sys.stdout", output),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        benchmark_mock.assert_awaited_once_with(
            settings,
            Path("/fixture/audio.wav"),
            None,
            Path("/fixture/reference.txt"),
        )
        self.assertNotIn("reference", output.getvalue())


class CLIErrorTests(unittest.TestCase):
    def test_expected_benchmark_error_group_exits_without_a_traceback(self) -> None:
        settings = _settings()
        stderr = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                ["2xbrainz", "benchmark", "--audio", "/fixture/audio.wav"],
            ),
            patch(
                "two_x_brainz.cli.Settings.from_environment",
                return_value=settings,
            ),
            patch("two_x_brainz.cli.configure_logging"),
            patch(
                "two_x_brainz.cli._run",
                new_callable=AsyncMock,
                side_effect=ExceptionGroup(
                    "benchmark failure",
                    [RemoteServiceError("private")],
                ),
            ),
            patch("two_x_brainz.cli.logger.error") as logger_error,
            patch("sys.stderr", stderr),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "error: concurrent operation failed\n")
        logger_error.assert_called_once_with(
            "command stopped after an expected concurrent operation failure",
            extra={"reason": "expected_concurrent_operation_failure"},
        )

    def test_expected_live_error_group_exits_without_a_traceback(self) -> None:
        settings = _settings()
        stderr = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                ["2xbrainz", "live"],
            ),
            patch(
                "two_x_brainz.cli.Settings.from_environment",
                return_value=settings,
            ),
            patch("two_x_brainz.cli.configure_logging"),
            patch(
                "two_x_brainz.cli._run",
                new_callable=AsyncMock,
                side_effect=ExceptionGroup(
                    "live stream failure",
                    [CaptureError("private")],
                ),
            ),
            patch("two_x_brainz.cli.logger.error") as logger_error,
            patch("sys.stderr", stderr),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            stderr.getvalue(),
            "error: live session stopped because capture or ASR is unavailable\n",
        )
        logger_error.assert_called_once_with(
            "live session stopped after an expected stream failure",
            extra={"reason": "expected_live_stream_failure"},
        )


def _settings(reply_model: str = "fixture-reply-model") -> Settings:
    return Settings(
        talkies_ws_url="ws://talkies:8000/v1/audio/transcriptions/stream",
        talkies_model="fixture-model",
        talkies_token=None,
        aigate_url="http://aigate:4000/v1",
        aigate_reply_model=reply_model,
        aigate_token=None,
        log_level="INFO",
        log_file=Path("/tmp/2xbrainz-test.log"),
    )
