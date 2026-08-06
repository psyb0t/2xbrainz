"""Opt-in real Talkies TTS/ASR live fixture with no host audio device."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import struct
import sys
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from two_x_brainz.audio import load_wav_fixture
from two_x_brainz.config import Settings
from two_x_brainz.constants import (
    AIGATE_CHAT_COMPLETIONS_PATH,
    AIGATE_MODELS_PATH,
    BEARER_PREFIX,
    HEADER_AUTHORIZATION,
    HEADER_CONTENT_TYPE,
    JSON_CONTENT_TYPE,
    MAX_AUDIO_FIXTURE_BYTES,
    TALKIES_STREAM_PATH,
)
from two_x_brainz.contracts import SpeakerRole
from two_x_brainz.errors import AudioFixtureError
from two_x_brainz.fixture_trace import FixtureTrace, FixtureTraceError

_MIC_NODE = "fixture-microphone"
_SYSTEM_NODE = "fixture-system"
_AIGATE_MODEL = "fixture-aigate-text"
_FIXTURE_REPLY_TEXT = "A concise synthetic fixture response."
_FIXTURE_INITIAL_DRAFT_TEXT = (
    "I will verify duplicate delivery prevention before the Tuesday rehearsal."
)
_FIXTURE_FINAL_DRAFT_TEXT = (
    "I will verify the idempotency guard in staging before the Tuesday rehearsal."
)
_REAL_AIGATE_ENV = "TWOXBRAINZ_FIXTURE_REAL_AIGATE"
_AUDIO_SCENARIO_ENV = "TWOXBRAINZ_FIXTURE_AUDIO_SCENARIO"
_REMOTE_START_SIGNAL_ENV = "TWOXBRAINZ_FIXTURE_REMOTE_START_SIGNAL"
_REMOTE_START_DELAY_ENV = "TWOXBRAINZ_FIXTURE_REMOTE_START_DELAY_SECONDS"
_TTS_MODEL_ENV = "TWOXBRAINZ_FIXTURE_TTS_MODEL"
_TTS_VOICE_ENV = "TWOXBRAINZ_FIXTURE_TTS_VOICE"
_WORK_DIRECTORY_ENV = "TWOXBRAINZ_FIXTURE_WORK_DIR"
_TRACE_DIRECTORY_ENV = "TWOXBRAINZ_FIXTURE_TRACE_DIR"
_USER_WAV_ENV = "TWOXBRAINZ_FIXTURE_USER_WAV"
_REMOTE_WAV_ENV = "TWOXBRAINZ_FIXTURE_REMOTE_WAV"
_USER_FOLLOWUP_WAV_ENV = "TWOXBRAINZ_FIXTURE_USER_FOLLOWUP_WAV"
_REMOTE_FOLLOWUP_WAV_ENV = "TWOXBRAINZ_FIXTURE_REMOTE_FOLLOWUP_WAV"
_USER_FOLLOWUP_SIGNAL_ENV = "TWOXBRAINZ_FIXTURE_USER_FOLLOWUP_SIGNAL"
_REMOTE_FOLLOWUP_SIGNAL_ENV = "TWOXBRAINZ_FIXTURE_REMOTE_FOLLOWUP_SIGNAL"
_FOLLOWUP_SILENCE_FRAME_COUNT_ENV = "TWOXBRAINZ_FIXTURE_FOLLOWUP_SILENCE_FRAMES"
_DEFAULT_TTS_MODEL = "kokoro-82m-nvidia"
_DEFAULT_TTS_VOICE = "af_heart"
_OVERLAP_SCENARIO = "overlap"
_SEQUENTIAL_SCENARIO = "sequential"
_INTERVIEW_SCENARIO = "interview"
_SUPPORTED_AUDIO_SCENARIOS = frozenset(
    {_OVERLAP_SCENARIO, _SEQUENTIAL_SCENARIO, _INTERVIEW_SCENARIO}
)
_TRUE_VALUE = "true"
_USER_TEXT = (
    "I will lead the Orchid migration from the notification worker to the queue. "
    "I will complete a Tuesday rehearsal. The unresolved risk is duplicate deliveries."
)
_REMOTE_TEXT = "How will you prevent duplicate deliveries before the Tuesday rehearsal?"
_INTERVIEW_USER_TEXTS = (
    _USER_TEXT,
    "I will add an idempotency key at the consumer boundary and test duplicate "
    "delivery recovery in staging before the Tuesday rehearsal.",
)
_INTERVIEW_REMOTE_TEXTS = (
    _REMOTE_TEXT,
    "What evidence will show the idempotency guard works before the rehearsal?",
)
_USER_TIMELINE_MARKERS = ("orchid", "tuesday", "duplicate")
_REMOTE_TIMELINE_MARKERS = ("duplicate", "tuesday")
_INTERVIEW_FIRST_REMOTE_MARKER_GROUPS = (
    ("duplicate",),
    ("tuesday", "rehearsal"),
)
_DRAFT_CONTEXT_MARKERS = ("duplicate", "idempot", "rehearsal", "tuesday")
_INTERVIEW_IDEMPOTENCY_MARKERS = ("idempot", "potency")
_INTERVIEW_STAGING_MARKERS = ("staging", "stajing")
_INTERVIEW_SUMMARY_MARKER_GROUPS = (
    ("orchid",),
    ("tuesday",),
    ("duplicate",),
    _INTERVIEW_IDEMPOTENCY_MARKERS,
    _INTERVIEW_STAGING_MARKERS,
)
_INTERVIEW_FINAL_DRAFT_MARKERS = ("idempot", "staging", "evidence", "verify")
_INTERVIEW_USER_FOLLOWUP_MARKER_GROUPS = (
    _INTERVIEW_IDEMPOTENCY_MARKERS,
    _INTERVIEW_STAGING_MARKERS,
    ("tuesday",),
)
_INTERVIEW_REMOTE_FOLLOWUP_MARKER_GROUPS = (
    _INTERVIEW_IDEMPOTENCY_MARKERS,
    ("rehearsal",),
)
_CAPTURE_TIMEOUT_SECONDS = 45
_PRODUCT_CAPTURE_TIMEOUT_SECONDS = 120
_EXIT_TIMEOUT_SECONDS = 15
_FOLLOWUP_SILENCE_FRAME_COUNT = 150
_FIXTURE_TTS_TIMEOUT_SECONDS = 60
_FIXTURE_TTS_MAX_ATTEMPTS = 3
_FIXTURE_TTS_RETRY_DELAY_SECONDS = 2
_TTS_PATH = "/v1/audio/speech"
_CLI_ERROR_PREFIX = "error: "
_RIFF_HEADER_SIZE = 12
_RIFF_SIZE_OFFSET = 4
_RIFF_CHUNK_HEADER_SIZE = 8
_UNKNOWN_RIFF_CHUNK_SIZE = 0xFFFFFFFF

_FAKE_PW_RECORD = """#!/app/.venv/bin/python
from __future__ import annotations
import os
import sys
import time
from pathlib import Path
from two_x_brainz.audio import load_wav_fixture

node_paths = {
    "fixture-microphone": os.environ.get("TWOXBRAINZ_FIXTURE_USER_WAV"),
    "fixture-system": os.environ.get("TWOXBRAINZ_FIXTURE_REMOTE_WAV"),
}
followup_paths = {
    "fixture-microphone": os.environ.get("TWOXBRAINZ_FIXTURE_USER_FOLLOWUP_WAV"),
    "fixture-system": os.environ.get("TWOXBRAINZ_FIXTURE_REMOTE_FOLLOWUP_WAV"),
}
followup_signals = {
    "fixture-microphone": os.environ.get("TWOXBRAINZ_FIXTURE_USER_FOLLOWUP_SIGNAL"),
    "fixture-system": os.environ.get("TWOXBRAINZ_FIXTURE_REMOTE_FOLLOWUP_SIGNAL"),
}
try:
    target = sys.argv[sys.argv.index("--target") + 1]
    fixture_path = node_paths[target]
except (IndexError, KeyError, ValueError, TypeError):
    raise SystemExit("invalid fixture pw-record target")
if target == "fixture-system":
    remote_start_signal = os.environ.get("TWOXBRAINZ_FIXTURE_REMOTE_START_SIGNAL")
    if remote_start_signal:
        while not Path(remote_start_signal).is_file():
            sys.stdout.buffer.write(b"\\x00" * 640)
            sys.stdout.buffer.flush()
            time.sleep(0.02)
    remote_start_delay = float(
        os.environ.get("TWOXBRAINZ_FIXTURE_REMOTE_START_DELAY_SECONDS", "0")
    )
    if remote_start_delay > 0:
        time.sleep(remote_start_delay)
def play(path):
    pcm = load_wav_fixture(Path(path)).pcm16le
    complete_pcm_size = len(pcm) - len(pcm) % 640
    for offset in range(0, complete_pcm_size, 640):
        sys.stdout.buffer.write(pcm[offset : offset + 640])
        sys.stdout.buffer.flush()
        time.sleep(0.02)

def play_silence():
    frame_count = int(
        os.environ.get("TWOXBRAINZ_FIXTURE_FOLLOWUP_SILENCE_FRAMES", "0")
    )
    for _ in range(frame_count):
        sys.stdout.buffer.write(b"\\x00" * 640)
        sys.stdout.buffer.flush()
        time.sleep(0.02)

def wait_for_followup(signal):
    while not Path(signal).is_file():
        sys.stdout.buffer.write(b"\\x00" * 640)
        sys.stdout.buffer.flush()
        time.sleep(0.02)

play(fixture_path)
followup_path = followup_paths[target]
followup_signal = followup_signals[target]
if followup_path and followup_signal:
    play_silence()
    wait_for_followup(followup_signal)
    play(followup_path)
"""


class FixtureError(RuntimeError):
    """An opt-in live fixture requirement was not met."""


def main() -> int:
    try:
        trace_path = asyncio.run(_run())
    except (FixtureError, FixtureTraceError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(
        '{"kind":"live_talkies_tts_fixture","result":"passed","trace_file":'
        f'"{trace_path}"}}'
    )
    return 0


async def _run() -> Path:
    settings = Settings.from_environment()
    if settings.talkies_token is None:
        raise FixtureError("shared gateway token is required for Talkies")
    work_directory = Path(os.environ.get(_WORK_DIRECTORY_ENV, "/tmp"))
    if not work_directory.is_dir():
        raise FixtureError("fixture working directory is unavailable")
    scenario = _audio_scenario()
    trace = FixtureTrace(
        _trace_directory(),
        f"live-talkies-{scenario}",
        secret_values=(settings.aigate_token or "", settings.talkies_token or ""),
    )
    try:
        return await _run_with_trace(
            settings,
            work_directory,
            scenario,
            trace,
        )
    except Exception as error:
        trace.failure(error)
        raise


async def _run_with_trace(
    settings: Settings,
    work_directory: Path,
    scenario: str,
    trace: FixtureTrace,
) -> Path:
    talkies_token = settings.talkies_token
    if talkies_token is None:
        raise FixtureError("shared gateway token is required for Talkies")
    use_real_aigate = _real_aigate_enabled()
    if use_real_aigate and settings.aigate_token is None:
        raise FixtureError("real AIGate fixture requires TWOXBRAINZ_AIGATE_TOKEN")
    if use_real_aigate and settings.aigate_reply_model is None:
        raise FixtureError("real AIGate fixture requires TWOXBRAINZ_AIGATE_REPLY_MODEL")
    with tempfile.TemporaryDirectory(
        prefix="2xbrainz-live-fixture-", dir=work_directory
    ) as name:
        directory = Path(name)
        user_wav = directory / "user.wav"
        remote_wav = directory / "remote.wav"
        user_followup_wav = directory / "user-followup.wav"
        remote_followup_wav = directory / "remote-followup.wav"
        trace.event(
            "fixture_started",
            scenario=scenario,
            real_aigate=use_real_aigate,
            talkies_model=settings.talkies_model,
            reply_model=(
                settings.aigate_reply_model if use_real_aigate else _AIGATE_MODEL
            ),
        )
        user_texts, remote_texts = _scenario_texts(scenario)
        _synthesize_wav(settings, user_texts[0], user_wav, SpeakerRole.USER, trace)
        _synthesize_wav(
            settings,
            remote_texts[0],
            remote_wav,
            SpeakerRole.REMOTE,
            trace,
        )
        if scenario == _INTERVIEW_SCENARIO:
            _synthesize_wav(
                settings,
                user_texts[1],
                user_followup_wav,
                SpeakerRole.USER,
                trace,
            )
            _synthesize_wav(
                settings,
                remote_texts[1],
                remote_followup_wav,
                SpeakerRole.REMOTE,
                trace,
            )
        _validate_wav(user_wav)
        _validate_wav(remote_wav)
        if scenario == _INTERVIEW_SCENARIO:
            _validate_wav(user_followup_wav)
            _validate_wav(remote_followup_wav)
        trace.event("fixture_wavs_validated")
        command = _write_fixture_pw_record(directory)
        remote_start_signal = (
            directory / "start-remote"
            if scenario in {_SEQUENTIAL_SCENARIO, _INTERVIEW_SCENARIO}
            else None
        )
        user_followup_signal = (
            directory / "start-user-followup"
            if scenario == _INTERVIEW_SCENARIO
            else None
        )
        remote_followup_signal = (
            directory / "start-remote-followup"
            if scenario == _INTERVIEW_SCENARIO
            else None
        )
        if use_real_aigate:
            assert settings.aigate_token is not None
            assert settings.aigate_reply_model is not None
            records = await _run_live(
                aigate_url=settings.aigate_url,
                reply_model=settings.aigate_reply_model,
                aigate_token=settings.aigate_token,
                command=command,
                user_wav=user_wav,
                remote_wav=remote_wav,
                user_followup_wav=(user_followup_wav if user_followup_signal else None),
                remote_followup_wav=(
                    remote_followup_wav if remote_followup_signal else None
                ),
                talkies_token=talkies_token,
                scenario=scenario,
                remote_start_signal=remote_start_signal,
                user_followup_signal=user_followup_signal,
                remote_followup_signal=remote_followup_signal,
                trace=trace,
            )
        else:
            with _fixture_aigate() as aigate_url:
                records = await _run_live(
                    aigate_url=aigate_url,
                    reply_model=_AIGATE_MODEL,
                    aigate_token=talkies_token,
                    command=command,
                    user_wav=user_wav,
                    remote_wav=remote_wav,
                    user_followup_wav=(
                        user_followup_wav if user_followup_signal else None
                    ),
                    remote_followup_wav=(
                        remote_followup_wav if remote_followup_signal else None
                    ),
                    talkies_token=talkies_token,
                    scenario=scenario,
                    remote_start_signal=remote_start_signal,
                    user_followup_signal=user_followup_signal,
                    remote_followup_signal=remote_followup_signal,
                    trace=trace,
                )
    _assert_records(records, scenario)
    trace.event("fixture_assertion_passed", assertion="base_live_records")
    if scenario == _OVERLAP_SCENARIO:
        _assert_overlap_records(records)
        trace.event("fixture_assertion_passed", assertion="overlap_suppression")
        trace.event("fixture_passed")
        return trace.path
    if scenario == _INTERVIEW_SCENARIO:
        _assert_interview_records(records)
        trace.event("fixture_assertion_passed", assertion="interview_story")
    else:
        _assert_product_records(records)
        trace.event("fixture_assertion_passed", assertion="sequential_product_flow")
    trace.event("fixture_passed")
    return trace.path


def _trace_directory() -> Path:
    value = os.environ.get(_TRACE_DIRECTORY_ENV, "").strip()
    if not value:
        raise FixtureError("fixture trace directory is required")
    return Path(value)


def _audio_scenario() -> str:
    scenario = os.environ.get(_AUDIO_SCENARIO_ENV, _OVERLAP_SCENARIO).strip()
    if scenario not in _SUPPORTED_AUDIO_SCENARIOS:
        raise FixtureError(
            "fixture audio scenario must be overlap, sequential, or interview"
        )
    return scenario


def _scenario_texts(scenario: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if scenario == _INTERVIEW_SCENARIO:
        return _INTERVIEW_USER_TEXTS, _INTERVIEW_REMOTE_TEXTS
    return (_USER_TEXT,), (_REMOTE_TEXT,)


def _real_aigate_enabled() -> bool:
    return os.environ.get(_REAL_AIGATE_ENV, "").strip().lower() == _TRUE_VALUE


def _synthesize_wav(
    settings: Settings,
    text: str,
    destination: Path,
    speaker_role: SpeakerRole,
    trace: FixtureTrace,
) -> None:
    model = os.environ.get(_TTS_MODEL_ENV, _DEFAULT_TTS_MODEL).strip()
    voice = os.environ.get(_TTS_VOICE_ENV, _DEFAULT_TTS_VOICE).strip()
    if not model or not voice:
        raise FixtureError("fixture Talkies TTS model and voice must not be empty")
    trace.event(
        "tts_request",
        speaker_role=speaker_role.value,
        model=model,
        voice=voice,
        text=text,
    )
    request = Request(
        _tts_url(settings.talkies_ws_url),
        data=json.dumps(
            {
                "model": model,
                "input": text,
                "voice": voice,
                "response_format": "wav",
            },
            separators=(",", ":"),
        ).encode("utf-8"),
        headers={
            HEADER_AUTHORIZATION: f"{BEARER_PREFIX}{settings.talkies_token}",
            HEADER_CONTENT_TYPE: JSON_CONTENT_TYPE,
        },
        method="POST",
    )
    body = _request_tts_body(request, speaker_role, trace)
    if not body or len(body) > MAX_AUDIO_FIXTURE_BYTES:
        raise FixtureError("Talkies TTS returned an invalid fixture size")
    destination.write_bytes(_canonicalize_wav_lengths(body))
    trace.event(
        "tts_response",
        speaker_role=speaker_role.value,
        wav_bytes=len(body),
    )


def _request_tts_body(
    request: Request,
    speaker_role: SpeakerRole,
    trace: FixtureTrace,
) -> bytes:
    for attempt in range(1, _FIXTURE_TTS_MAX_ATTEMPTS + 1):
        try:
            with urlopen(request, timeout=_FIXTURE_TTS_TIMEOUT_SECONDS) as response:
                body = response.read(MAX_AUDIO_FIXTURE_BYTES + 1)
                trace.event(
                    "tts_attempt_completed",
                    speaker_role=speaker_role.value,
                    attempt=attempt,
                    status_code=response.status,
                )
                return body
        except HTTPError as error:
            trace.event(
                "tts_attempt_failed",
                speaker_role=speaker_role.value,
                attempt=attempt,
                status_code=error.code,
            )
            if not _should_retry_tts_conflict(error.code, attempt):
                raise FixtureError(f"Talkies TTS returned HTTP {error.code}") from error
            time.sleep(_FIXTURE_TTS_RETRY_DELAY_SECONDS)
        except TimeoutError as error:
            raise FixtureError("Talkies TTS timed out") from error
        except URLError as error:
            raise FixtureError("connect to Talkies TTS") from error
        except OSError as error:
            raise FixtureError("read Talkies TTS response") from error
    raise FixtureError("Talkies TTS exhausted its retry budget")


def _should_retry_tts_conflict(status_code: int, attempt: int) -> bool:
    return status_code == HTTPStatus.CONFLICT and attempt < _FIXTURE_TTS_MAX_ATTEMPTS


def _canonicalize_wav_lengths(body: bytes) -> bytes:
    if len(body) < _RIFF_HEADER_SIZE or body[:4] != b"RIFF" or body[8:12] != b"WAVE":
        raise FixtureError("Talkies TTS did not return a RIFF WAV")
    declared_riff_size = struct.unpack("<I", body[_RIFF_SIZE_OFFSET:8])[0]
    actual_riff_size = len(body) - _RIFF_CHUNK_HEADER_SIZE
    if declared_riff_size not in {actual_riff_size, _UNKNOWN_RIFF_CHUNK_SIZE}:
        raise FixtureError("Talkies TTS WAV has an invalid RIFF length")

    data_offset = _find_data_chunk(body)
    normalized = bytearray(body)
    normalized[_RIFF_SIZE_OFFSET:8] = struct.pack("<I", actual_riff_size)
    normalized[data_offset + 4 : data_offset + 8] = struct.pack(
        "<I", len(normalized) - data_offset - _RIFF_CHUNK_HEADER_SIZE
    )
    return bytes(normalized)


def _find_data_chunk(body: bytes) -> int:
    offset = _RIFF_HEADER_SIZE
    while offset + _RIFF_CHUNK_HEADER_SIZE <= len(body):
        chunk_size = struct.unpack("<I", body[offset + 4 : offset + 8])[0]
        remaining = len(body) - offset - _RIFF_CHUNK_HEADER_SIZE
        if body[offset : offset + 4] == b"data":
            if chunk_size not in {remaining, _UNKNOWN_RIFF_CHUNK_SIZE}:
                raise FixtureError("Talkies TTS WAV has an invalid data length")
            return offset
        if chunk_size > remaining:
            raise FixtureError("Talkies TTS WAV has an incomplete chunk")
        offset += _RIFF_CHUNK_HEADER_SIZE + chunk_size + chunk_size % 2
    raise FixtureError("Talkies TTS WAV does not contain a data chunk")


def _tts_url(stream_url: str) -> str:
    parsed = urlsplit(stream_url)
    if not parsed.path.endswith(TALKIES_STREAM_PATH):
        raise FixtureError("Talkies stream URL must have the native stream suffix")
    scheme = "https" if parsed.scheme == "wss" else "http"
    prefix = parsed.path.removesuffix(TALKIES_STREAM_PATH)
    return urlunsplit((scheme, parsed.netloc, f"{prefix}{_TTS_PATH}", "", ""))


def _validate_wav(path: Path) -> None:
    try:
        load_wav_fixture(path)
    except AudioFixtureError as error:
        raise FixtureError("Talkies TTS did not return a bounded PCM WAV") from error


def _write_fixture_pw_record(directory: Path) -> Path:
    command = directory / "pw-record"
    command.write_text(_FAKE_PW_RECORD, encoding="utf-8")
    command.chmod(command.stat().st_mode | stat.S_IXUSR)
    return command


async def _run_live(
    *,
    aigate_url: str,
    reply_model: str,
    aigate_token: str,
    command: Path,
    user_wav: Path,
    remote_wav: Path,
    user_followup_wav: Path | None,
    remote_followup_wav: Path | None,
    talkies_token: str,
    scenario: str,
    remote_start_signal: Path | None,
    user_followup_signal: Path | None,
    remote_followup_signal: Path | None,
    trace: FixtureTrace,
) -> list[dict[str, object]]:
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{command.parent}:{environment['PATH']}",
            "TWOXBRAINZ_AIGATE_URL": aigate_url,
            "TWOXBRAINZ_AIGATE_REPLY_MODEL": reply_model,
            "TWOXBRAINZ_AIGATE_COACH_MODEL": reply_model,
            "TWOXBRAINZ_AIGATE_SUMMARY_MODEL": reply_model,
            "TWOXBRAINZ_AIGATE_TOKEN": aigate_token,
            _USER_WAV_ENV: str(user_wav),
            _REMOTE_WAV_ENV: str(remote_wav),
            _FOLLOWUP_SILENCE_FRAME_COUNT_ENV: str(_FOLLOWUP_SILENCE_FRAME_COUNT),
        }
    )
    environment.pop(_REMOTE_START_SIGNAL_ENV, None)
    environment.pop(_REMOTE_START_DELAY_ENV, None)
    environment.pop(_USER_FOLLOWUP_WAV_ENV, None)
    environment.pop(_REMOTE_FOLLOWUP_WAV_ENV, None)
    environment.pop(_USER_FOLLOWUP_SIGNAL_ENV, None)
    environment.pop(_REMOTE_FOLLOWUP_SIGNAL_ENV, None)
    if remote_start_signal is not None:
        environment[_REMOTE_START_SIGNAL_ENV] = str(remote_start_signal)
    if user_followup_wav is not None and user_followup_signal is not None:
        environment[_USER_FOLLOWUP_WAV_ENV] = str(user_followup_wav)
        environment[_USER_FOLLOWUP_SIGNAL_ENV] = str(user_followup_signal)
    if remote_followup_wav is not None and remote_followup_signal is not None:
        environment[_REMOTE_FOLLOWUP_WAV_ENV] = str(remote_followup_wav)
        environment[_REMOTE_FOLLOWUP_SIGNAL_ENV] = str(remote_followup_signal)
    if scenario == _OVERLAP_SCENARIO:
        environment[_REMOTE_START_DELAY_ENV] = "1"
    trace.event("live_process_starting", scenario=scenario)
    process = await asyncio.create_subprocess_exec(
        "2xbrainz",
        "live",
        "--mic-node",
        _MIC_NODE,
        "--system-node",
        _SYSTEM_NODE,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise FixtureError("start live fixture")
    trace.event("live_process_started", process_id=process.pid)
    standard_error_task = asyncio.create_task(
        _trace_standard_error(process.stderr, trace)
    )
    records: list[dict[str, object]] = []
    capture_timeout = (
        _PRODUCT_CAPTURE_TIMEOUT_SECONDS
        if scenario in {_SEQUENTIAL_SCENARIO, _INTERVIEW_SCENARIO}
        else _CAPTURE_TIMEOUT_SECONDS
    )
    try:
        async with asyncio.timeout(capture_timeout):
            while not _scenario_records_ready(records, scenario):
                line = await process.stdout.readline()
                if not line:
                    await process.wait()
                    raise _early_exit_error(records)
                record = _parse_record(line)
                records.append(record)
                trace.event("live_json_record", record=record)
                _release_scheduled_playback(
                    records,
                    scenario=scenario,
                    remote_start_signal=remote_start_signal,
                    user_followup_signal=user_followup_signal,
                    remote_followup_signal=remote_followup_signal,
                    trace=trace,
                )
                _raise_for_required_provider_failure(records, scenario)
        process.stdin.write(b"stop\n")
        await process.stdin.drain()
        process.stdin.close()
        trace.event("live_stop_sent")
        await asyncio.wait_for(process.wait(), _EXIT_TIMEOUT_SECONDS)
    except TimeoutError as error:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise FixtureError("live fixture exceeded its deadline") from error
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await standard_error_task
    if process.returncode != 0:
        trace.event("live_process_exited", returncode=process.returncode)
        raise FixtureError("live fixture command failed")
    for line in (await process.stdout.read()).splitlines():
        record = _parse_record(line)
        records.append(record)
        trace.event("live_json_record", record=record)
    trace.event("live_process_exited", returncode=process.returncode)
    return records


async def _trace_standard_error(
    stream: asyncio.StreamReader,
    trace: FixtureTrace,
) -> None:
    while line := await stream.readline():
        _trace_standard_error_line(line, trace)


def _trace_standard_error_line(line: bytes, trace: FixtureTrace) -> None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError as error:
        message = line.decode("utf-8", errors="replace").strip()
        if _trace_terminal_error_line(message, trace):
            return
        trace.event(
            "live_stderr_unstructured",
            byte_count=len(line),
            message=message,
        )
        raise FixtureError("live fixture wrote an unstructured diagnostic") from error
    if not isinstance(record, dict):
        trace.event("live_stderr_unstructured", byte_count=len(line))
        raise FixtureError("live fixture wrote a non-object diagnostic")
    trace.event("live_stderr_record", record=cast(dict[str, object], record))


def _trace_terminal_error_line(message: str, trace: FixtureTrace) -> bool:
    if message.startswith(_CLI_ERROR_PREFIX):
        trace.event("live_stderr_terminal_error", message=message)
        return True
    return False


def _parse_record(line: bytes) -> dict[str, object]:
    try:
        record = json.loads(line)
    except json.JSONDecodeError as error:
        raise FixtureError("live fixture returned invalid JSON") from error
    if not isinstance(record, dict):
        raise FixtureError("live fixture returned a non-object JSON record")
    return cast(dict[str, object], record)


def _asr_roles(records: list[dict[str, object]]) -> set[str]:
    return {
        role
        for record in records
        if record.get("kind") == "asr_stats"
        and isinstance(role := record.get("speaker_role"), str)
    }


def _early_exit_error(records: list[dict[str, object]]) -> FixtureError:
    for record in records:
        if record.get("kind") != "session_error":
            continue
        reason = record.get("reason")
        if isinstance(reason, str):
            return FixtureError(f"live fixture ended before both ASR streams: {reason}")
    return FixtureError("live fixture ended before both ASR streams")


def _assert_records(records: list[dict[str, object]], scenario: str) -> None:
    if not any(
        record.get("kind") == "session" and record.get("action") == "started"
        for record in records
    ):
        raise FixtureError("live fixture did not start a session")
    _assert_positive_role_counts(records, "asr_stats", "frames")
    _assert_positive_role_counts(records, "capture_stats", "frame_count")
    final_roles = {
        role
        for record in records
        if record.get("kind") == "transcript"
        and record.get("type") == "final"
        and isinstance(role := record.get("speaker_role"), str)
    }
    if final_roles != {"user", "remote"}:
        raise FixtureError("live fixture did not finalize both streams")
    timeline_roles = {
        role
        for record in records
        if record.get("kind") == "timeline"
        and isinstance(role := record.get("speaker_role"), str)
    }
    if timeline_roles != {"user", "remote"}:
        raise FixtureError("live fixture did not emit both timeline entries")
    drift_records = [
        record for record in records if record.get("kind") == "capture_drift"
    ]
    if len(drift_records) != 1:
        raise FixtureError("live fixture did not report capture drift")
    comparison_count = drift_records[0].get("comparison_count")
    if not isinstance(comparison_count, int) or comparison_count < 0:
        raise FixtureError("live fixture reported invalid capture timing")
    if scenario == _OVERLAP_SCENARIO and comparison_count <= 0:
        raise FixtureError("overlap fixture did not compare capture timing")


def _assert_overlap_records(records: list[dict[str, object]]) -> None:
    if any(record.get("kind") == "draft" for record in records):
        raise FixtureError("overlapping remote audio incorrectly started a draft")
    _assert_completed_record(records, "commentary")
    _assert_completed_record(records, "summary")


def _assert_product_records(records: list[dict[str, object]]) -> None:
    _assert_completed_record(records, "draft")
    _assert_completed_record(records, "commentary")
    _assert_completed_record(records, "summary")
    _assert_timeline_markers(records, SpeakerRole.USER, _USER_TIMELINE_MARKERS)
    _assert_timeline_markers(records, SpeakerRole.REMOTE, _REMOTE_TIMELINE_MARKERS)
    _assert_completed_record_any_marker(records, "draft", _DRAFT_CONTEXT_MARKERS)
    _assert_completed_record_markers(records, "summary", _USER_TIMELINE_MARKERS)


def _assert_interview_records(records: list[dict[str, object]]) -> None:
    timeline_records = _timeline_records(records)
    expected_roles = [
        SpeakerRole.USER.value,
        SpeakerRole.REMOTE.value,
        SpeakerRole.USER.value,
        SpeakerRole.REMOTE.value,
    ]
    actual_roles = [record.get("speaker_role") for record in timeline_records]
    if actual_roles != expected_roles:
        raise FixtureError("interview timeline did not preserve four-turn role order")

    _assert_text_markers(
        _record_text(timeline_records[0], "initial user timeline"),
        _USER_TIMELINE_MARKERS,
        "initial user timeline",
    )
    _assert_text_marker_groups(
        _record_text(timeline_records[1], "first remote timeline"),
        _INTERVIEW_FIRST_REMOTE_MARKER_GROUPS,
        "first remote timeline",
    )
    _assert_text_marker_groups(
        _record_text(timeline_records[2], "user mitigation timeline"),
        _INTERVIEW_USER_FOLLOWUP_MARKER_GROUPS,
        "user mitigation timeline",
    )
    _assert_text_marker_groups(
        _record_text(timeline_records[3], "final remote timeline"),
        _INTERVIEW_REMOTE_FOLLOWUP_MARKER_GROUPS,
        "final remote timeline",
    )
    _assert_text_marker_groups(
        _latest_record_text(records, "summary"),
        _INTERVIEW_SUMMARY_MARKER_GROUPS,
        "summary",
    )

    first_remote_turn_id = _record_turn_id(timeline_records[1], "first remote timeline")
    final_remote_turn_id = _record_turn_id(timeline_records[3], "final remote timeline")
    completed_drafts = _completed_records(records, "draft")
    if len(completed_drafts) < 2:
        raise FixtureError(
            "interview did not retain drafts before and after mitigation"
        )
    if completed_drafts[0].get("trigger_turn_id") != first_remote_turn_id:
        raise FixtureError("interview did not create the initial reply draft")
    final_draft = completed_drafts[-1]
    if final_draft.get("trigger_turn_id") != final_remote_turn_id:
        raise FixtureError("interview retained a stale reply draft")
    _assert_text_has_any_marker(
        _record_text(final_draft, "final reply draft"),
        _INTERVIEW_FINAL_DRAFT_MARKERS,
        "final reply draft",
    )
    _assert_completed_record_for_turn(records, "summary", final_remote_turn_id)


def _record_text(record: dict[str, object], description: str) -> str:
    text = record.get("text")
    if isinstance(text, str):
        return text
    raise FixtureError(f"{description} did not contain text")


def _record_turn_id(record: dict[str, object], description: str) -> str:
    turn_id = record.get("turn_id")
    if isinstance(turn_id, str) and turn_id:
        return turn_id
    raise FixtureError(f"{description} did not contain a turn ID")


def _assert_text_has_any_marker(
    text: str,
    markers: tuple[str, ...],
    description: str,
) -> None:
    if any(marker in text.lower() for marker in markers):
        return
    raise FixtureError(f"{description} did not address the interview context")


def _assert_timeline_markers(
    records: list[dict[str, object]],
    speaker_role: SpeakerRole,
    markers: tuple[str, ...],
) -> None:
    text = _latest_record_text(records, "timeline", speaker_role.value)
    _assert_text_markers(text, markers, f"{speaker_role.value} timeline")


def _assert_completed_record_markers(
    records: list[dict[str, object]],
    kind: str,
    markers: tuple[str, ...],
) -> None:
    text = _latest_record_text(records, kind)
    _assert_text_markers(text, markers, kind)


def _assert_completed_record_any_marker(
    records: list[dict[str, object]],
    kind: str,
    markers: tuple[str, ...],
) -> None:
    text = _latest_record_text(records, kind)
    normalized_text = text.lower()
    if any(marker in normalized_text for marker in markers):
        return
    raise FixtureError(f"{kind} did not address the interview context")


def _latest_record_text(
    records: list[dict[str, object]],
    kind: str,
    speaker_role: str | None = None,
) -> str:
    for record in reversed(records):
        if record.get("kind") != kind:
            continue
        if speaker_role is not None and record.get("speaker_role") != speaker_role:
            continue
        text = record.get("text")
        if isinstance(text, str):
            return text
    raise FixtureError(f"live fixture did not retain {kind} text")


def _assert_text_markers(
    text: str,
    markers: tuple[str, ...],
    description: str,
) -> None:
    normalized_text = text.lower()
    missing = [marker for marker in markers if marker not in normalized_text]
    if missing:
        raise FixtureError(f"{description} omitted required interview context")


def _assert_text_marker_groups(
    text: str,
    marker_groups: tuple[tuple[str, ...], ...],
    description: str,
) -> None:
    normalized_text = text.lower()
    if all(
        any(marker in normalized_text for marker in markers)
        for markers in marker_groups
    ):
        return
    raise FixtureError(f"{description} omitted required interview context")


def _scenario_records_ready(records: list[dict[str, object]], scenario: str) -> bool:
    if _asr_roles(records) != {"user", "remote"}:
        return False
    final_roles = {
        role
        for record in records
        if record.get("kind") == "transcript"
        and record.get("type") == "final"
        and isinstance(role := record.get("speaker_role"), str)
    }
    if final_roles != {"user", "remote"}:
        return False
    timeline_roles = {
        role
        for record in records
        if record.get("kind") == "timeline"
        and isinstance(role := record.get("speaker_role"), str)
    }
    if timeline_roles != {"user", "remote"}:
        return False
    if not _has_completed_record(records, "commentary"):
        return False
    if not _has_completed_record(records, "summary"):
        return False
    if scenario == _OVERLAP_SCENARIO:
        return not any(record.get("kind") == "draft" for record in records)
    if scenario == _INTERVIEW_SCENARIO:
        return _interview_records_ready(records)
    return _has_completed_record(records, "draft")


def _interview_records_ready(records: list[dict[str, object]]) -> bool:
    timeline_records = _timeline_records(records)
    expected_roles = [
        SpeakerRole.USER.value,
        SpeakerRole.REMOTE.value,
        SpeakerRole.USER.value,
        SpeakerRole.REMOTE.value,
    ]
    if [record.get("speaker_role") for record in timeline_records] != expected_roles:
        return False
    if not _has_completed_record_for_turn(
        records,
        "commentary",
        _record_turn_id(timeline_records[2], "user mitigation timeline"),
    ):
        return False
    final_remote_turn_id = _record_turn_id(
        timeline_records[3],
        "final remote timeline",
    )
    return _has_completed_record_for_turn(records, "draft", final_remote_turn_id) and (
        _has_completed_record_for_turn(records, "summary", final_remote_turn_id)
    )


def _release_scheduled_playback(
    records: list[dict[str, object]],
    *,
    scenario: str,
    remote_start_signal: Path | None,
    user_followup_signal: Path | None,
    remote_followup_signal: Path | None,
    trace: FixtureTrace,
) -> None:
    if (
        remote_start_signal is not None
        and _has_completed_record(records, "commentary")
        and not remote_start_signal.exists()
    ):
        remote_start_signal.touch()
        trace.event("playback_released", speaker_role=SpeakerRole.REMOTE.value, turn=1)
    if scenario != _INTERVIEW_SCENARIO:
        return
    timeline_records = _timeline_records(records)
    if (
        user_followup_signal is not None
        and len(timeline_records) >= 2
        and _has_completed_record_for_turn(
            records,
            "draft",
            _record_turn_id(timeline_records[1], "first remote timeline"),
        )
        and not user_followup_signal.exists()
    ):
        user_followup_signal.touch()
        trace.event("playback_released", speaker_role=SpeakerRole.USER.value, turn=2)
    if len(timeline_records) < 3 or remote_followup_signal is None:
        return
    user_followup_turn_id = _record_turn_id(
        timeline_records[2],
        "user mitigation timeline",
    )
    if (
        _has_completed_record_for_turn(records, "commentary", user_followup_turn_id)
        and not remote_followup_signal.exists()
    ):
        remote_followup_signal.touch()
        trace.event("playback_released", speaker_role=SpeakerRole.REMOTE.value, turn=2)


def _raise_for_required_provider_failure(
    records: list[dict[str, object]],
    scenario: str,
) -> None:
    required_kinds = {"commentary", "summary"}
    if scenario != _OVERLAP_SCENARIO:
        required_kinds.add("draft")
    if any(
        record.get("kind") in required_kinds and record.get("status") == "failed"
        for record in records
    ):
        raise FixtureError("live fixture observed a terminal provider failure")


def _assert_completed_record(records: list[dict[str, object]], kind: str) -> None:
    if _has_completed_record(records, kind):
        return
    raise FixtureError(f"live fixture did not complete {kind}")


def _has_completed_record(records: list[dict[str, object]], kind: str) -> bool:
    return bool(_completed_records(records, kind))


def _completed_records(
    records: list[dict[str, object]],
    kind: str,
) -> list[dict[str, object]]:
    return [
        record
        for record in records
        if record.get("kind") == kind
        and record.get("status") == "completed"
        and isinstance(text := record.get("text"), str)
        and bool(text.strip())
    ]


def _has_completed_record_for_turn(
    records: list[dict[str, object]],
    kind: str,
    turn_id: str,
) -> bool:
    return any(
        record.get("trigger_turn_id") == turn_id
        for record in _completed_records(records, kind)
    )


def _assert_completed_record_for_turn(
    records: list[dict[str, object]],
    kind: str,
    turn_id: str,
) -> None:
    if _has_completed_record_for_turn(records, kind, turn_id):
        return
    raise FixtureError(f"live fixture did not complete {kind} for the final turn")


def _timeline_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    return [record for record in records if record.get("kind") == "timeline"]


def _assert_positive_role_counts(
    records: list[dict[str, object]], kind: str, field: str
) -> None:
    counts = {
        role: count
        for record in records
        if record.get("kind") == kind
        and isinstance(role := record.get("speaker_role"), str)
        and isinstance(count := record.get(field), int)
    }
    if counts.keys() != {"user", "remote"} or any(
        count <= 0 for count in counts.values()
    ):
        raise FixtureError(f"live fixture did not report populated {kind}")


# The stub answers only under the version prefix, the way a real provider does.
# Serving the unprefixed form too would hide a base-URL mistake that OpenAI and
# Groq would reject.
_API_PREFIX = "/v1"


class _FixtureAIGateHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != f"{_API_PREFIX}{AIGATE_MODELS_PATH}":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._write_json({"object": "list", "data": [{"id": _AIGATE_MODEL}]})

    def do_POST(self) -> None:
        if self.path != f"{_API_PREFIX}{AIGATE_CHAT_COMPLETIONS_PATH}":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            request = _read_fixture_aigate_request(self)
            content = _fixture_aigate_response_content(request)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        self._write_json({"choices": [{"message": {"content": content}}]})

    def log_message(self, format: str, *_arguments: object) -> None:
        return

    def _write_json(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header(HEADER_CONTENT_TYPE, JSON_CONTENT_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _FixtureAIGateServer:
    def __enter__(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureAIGateHandler)
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.start()
        host, port = cast(tuple[str, int], self._server.server_address)
        return f"http://{host}:{port}{_API_PREFIX}"

    def __exit__(self, *_arguments: object) -> None:
        self._server.shutdown()
        self._thread.join()
        self._server.server_close()


def _fixture_aigate() -> _FixtureAIGateServer:
    return _FixtureAIGateServer()


def _read_fixture_aigate_request(
    handler: _FixtureAIGateHandler,
) -> dict[str, object]:
    content_length = int(handler.headers.get("Content-Length", "0"))
    if content_length <= 0:
        raise ValueError("fixture AIGate request body is required")
    payload = json.loads(handler.rfile.read(content_length).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("fixture AIGate request must be an object")
    return cast(dict[str, object], payload)


def _fixture_aigate_response_content(request: dict[str, object]) -> str:
    messages = request.get("messages")
    if not isinstance(messages, list):
        raise ValueError("fixture AIGate request messages are required")
    message_list = cast(list[object], messages)
    if len(message_list) < 2:
        raise ValueError("fixture AIGate request messages are required")
    system_message = message_list[0]
    transcript_message = message_list[-1]
    if not isinstance(system_message, dict) or not isinstance(transcript_message, dict):
        raise ValueError("fixture AIGate request messages must be objects")
    system_payload = cast(dict[str, object], system_message)
    transcript_payload = cast(dict[str, object], transcript_message)
    system_prompt = system_payload.get("content")
    transcript = transcript_payload.get("content")
    if not isinstance(system_prompt, str) or not isinstance(transcript, str):
        raise ValueError("fixture AIGate request content must be text")
    if "running conversation summary" in system_prompt.lower():
        return f"Fixture running summary: {' '.join(transcript.split())}"
    if "private coaching" in system_prompt.lower():
        return _FIXTURE_REPLY_TEXT
    if any(marker in transcript.lower() for marker in _INTERVIEW_IDEMPOTENCY_MARKERS):
        return _FIXTURE_FINAL_DRAFT_TEXT
    return _FIXTURE_INITIAL_DRAFT_TEXT


if __name__ == "__main__":
    raise SystemExit(main())
