"""Docker-first CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from two_x_brainz import VERSION
from two_x_brainz.aigate import AIGateClient, EchoDraftProvider
from two_x_brainz.benchmark import run_asr_benchmark
from two_x_brainz.capture import list_pipewire_nodes
from two_x_brainz.config import Settings
from two_x_brainz.constants import JSON_RECORD_SCHEMA_VERSION
from two_x_brainz.coordinator import ConversationCoordinator
from two_x_brainz.errors import (
    CaptureError,
    ProtocolError,
    RemoteServiceError,
    TwoXBrainzError,
)
from two_x_brainz.logging_config import configure_logging
from two_x_brainz.replay import load_replay_events
from two_x_brainz.runtime import run_live, transcript_record

logger = logging.getLogger(__name__)

_EXIT_FAILURE = 1
_EXIT_SUCCESS = 0
_EXPECTED_LIVE_ERRORS = (CaptureError, ProtocolError, RemoteServiceError)
_LIVE_SESSION_FAILURE_MESSAGE = (
    "live session stopped because capture or ASR is unavailable"
)
_CONCURRENT_OPERATION_FAILURE_MESSAGE = "concurrent operation failed"


def main() -> int:
    """Run the requested CLI command with structured diagnostics enabled."""
    parser = _build_parser()
    arguments = parser.parse_args()
    try:
        settings = Settings.from_environment()
        configure_logging(settings.log_level, settings.log_file)
        return asyncio.run(_run(arguments, settings))
    except TwoXBrainzError as error:
        logger.error("command failed", extra={"error": str(error)})
        print(f"error: {error}", file=sys.stderr)
        return _EXIT_FAILURE
    except BaseExceptionGroup as error:
        expected_errors, unexpected_errors = error.split(_EXPECTED_LIVE_ERRORS)
        if unexpected_errors is not None:
            raise
        if expected_errors is None:
            raise
        if arguments.command != "live":
            logger.error(
                "command stopped after an expected concurrent operation failure",
                extra={"reason": "expected_concurrent_operation_failure"},
            )
            print(f"error: {_CONCURRENT_OPERATION_FAILURE_MESSAGE}", file=sys.stderr)
            return _EXIT_FAILURE
        logger.error(
            "live session stopped after an expected stream failure",
            extra={"reason": "expected_live_stream_failure"},
        )
        print(f"error: {_LIVE_SESSION_FAILURE_MESSAGE}", file=sys.stderr)
        return _EXIT_FAILURE
    except KeyboardInterrupt:
        logger.info("command interrupted")
        return _EXIT_SUCCESS


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="2xbrainz")
    parser.add_argument("--version", action="version", version=VERSION)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("doctor", help="show sanitized runtime configuration")
    subcommands.add_parser("status", help="show configured runtime status")
    subcommands.add_parser(
        "devices",
        help="list PipeWire nodes visible in the container",
    )
    replay = subcommands.add_parser("replay", help="replay a JSONL transcript fixture")
    replay.add_argument("--events", required=True, type=Path)
    live = subcommands.add_parser(
        "live",
        help="capture two PipeWire nodes and call Talkies",
    )
    live.add_argument("--mic-node", required=True)
    live.add_argument("--system-node", required=True)
    benchmark = subcommands.add_parser(
        "benchmark",
        help="verify concurrent Talkies native and file contracts with one WAV",
    )
    benchmark.add_argument("--audio", required=True, type=Path)
    benchmark.add_argument(
        "--reference-file",
        type=Path,
        help="optional local UTF-8 reference transcript used only to calculate WER",
    )
    benchmark.add_argument(
        "--with-draft",
        action="store_true",
        help="run one synthetic text-only AIGate draft beside native ASR streams",
    )
    return parser


async def _run(arguments: argparse.Namespace, settings: Settings) -> int:
    command = arguments.command
    if command in {"doctor", "status"}:
        _write_status(settings)
        return _EXIT_SUCCESS
    if command == "devices":
        print(json.dumps(await list_pipewire_nodes(), separators=(",", ":")))
        return _EXIT_SUCCESS
    if command == "replay":
        await _run_replay(arguments.events, settings)
        return _EXIT_SUCCESS
    if command == "live":
        await run_live(settings, arguments.mic_node, arguments.system_node)
        return _EXIT_SUCCESS
    if command == "benchmark":
        draft_provider = None
        if arguments.with_draft:
            draft_provider = AIGateClient(
                base_url=settings.aigate_url,
                model=settings.aigate_model,
                token=settings.aigate_token,
            )
            draft_provider.require_model()
        report = await run_asr_benchmark(
            settings,
            arguments.audio,
            draft_provider,
            arguments.reference_file,
        )
        print(
            json.dumps(
                {
                    "schema_version": JSON_RECORD_SCHEMA_VERSION,
                    "kind": "asr_benchmark",
                    "model": report.model,
                    "source_audio_seconds": report.source_audio_seconds,
                    "native_elapsed_seconds": report.native_elapsed_seconds,
                    "draft_elapsed_seconds": report.draft_elapsed_seconds,
                    "native_streams": [
                        {
                            "stream_id": stream.stream_id,
                            "speaker_role": stream.speaker_role.value,
                            "event_types": stream.event_types,
                            "frames": stream.frames,
                            "audio_seconds": stream.audio_seconds,
                            "word_error_rate": stream.word_error_rate,
                        }
                        for stream in report.native_streams
                    ],
                    "batch_json_elapsed_seconds": report.batch_json_elapsed_seconds,
                    "batch_json_word_error_rate": report.batch_json_word_error_rate,
                    "batch_verbose_json_elapsed_seconds": (
                        report.batch_verbose_json_elapsed_seconds
                    ),
                    "batch_verbose_json_word_error_rate": (
                        report.batch_verbose_json_word_error_rate
                    ),
                    "verbose_segment_count": report.verbose_segment_count,
                    "verbose_word_count": report.verbose_word_count,
                },
                separators=(",", ":"),
            )
        )
        return _EXIT_SUCCESS
    raise TwoXBrainzError(f"unsupported command: {command}")


async def _run_replay(events_path: Path, settings: Settings) -> None:
    provider = EchoDraftProvider()
    coordinator = ConversationCoordinator(provider, provider)
    for event in load_replay_events(
        events_path,
        session_id="replay",
        model=settings.talkies_model,
    ):
        update = await coordinator.ingest(event)
        print(
            json.dumps(
                transcript_record(event),
                separators=(",", ":"),
            )
        )
        if update.turn is not None:
            print(
                json.dumps(
                    {
                        "schema_version": JSON_RECORD_SCHEMA_VERSION,
                        "kind": "turn",
                        "turn_id": update.turn.turn_id,
                        "speaker_role": update.turn.speaker_role.value,
                        "state": update.turn.state.value,
                        "transcript_revision": update.turn.transcript_revision,
                    },
                    separators=(",", ":"),
                )
            )
        if update.timeline is not None:
            print(
                json.dumps(
                    {
                        "schema_version": JSON_RECORD_SCHEMA_VERSION,
                        "kind": "timeline",
                        "turn_id": update.timeline.turn_id,
                        "speaker_role": update.timeline.speaker_role.value,
                        "transcript_revision": update.timeline.transcript_revision,
                        "text": update.timeline.text,
                    },
                    separators=(",", ":"),
                )
            )
    draft = await coordinator.wait_for_idle()
    if draft is not None:
        print(
            json.dumps(
                {
                    "schema_version": JSON_RECORD_SCHEMA_VERSION,
                    "kind": "draft",
                    "generation_id": draft.generation_id,
                    "trigger_turn_id": draft.trigger_turn_id,
                    "status": draft.status.value,
                    "text": draft.text,
                    "context_revision": draft.context_revision,
                },
                separators=(",", ":"),
            )
        )
    for insight in coordinator.drain_completed_insights():
        print(
            json.dumps(
                {
                    "schema_version": JSON_RECORD_SCHEMA_VERSION,
                    "kind": insight.kind.value,
                    "generation_id": insight.generation_id,
                    "trigger_turn_id": insight.trigger_turn_id,
                    "status": insight.status.value,
                    "text": insight.text,
                    "context_revision": insight.context_revision,
                },
                separators=(",", ":"),
            )
        )


def _write_status(settings: Settings) -> None:
    print(
        json.dumps(
            {
                "schema_version": JSON_RECORD_SCHEMA_VERSION,
                "version": VERSION,
                "talkies_ws_url": settings.talkies_ws_url,
                "talkies_model": settings.talkies_model,
                "talkies_token_configured": settings.talkies_token is not None,
                "aigate_url": settings.aigate_url,
                "aigate_mode": settings.aigate_mode.value,
                "aigate_model": settings.aigate_model,
                "aigate_token_configured": settings.aigate_token is not None,
                "log_level": settings.log_level,
            },
            separators=(",", ":"),
        )
    )
