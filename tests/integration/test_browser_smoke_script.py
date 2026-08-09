from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _PROJECT_ROOT / "scripts" / "browser_smoke.sh"
_APP_IMAGE = "2xbrainz-browser-test:local"
_BROWSER_IMAGE = "browser-test-image@sha256:fixture"


class BrowserSmokeScriptTests(unittest.TestCase):
    def test_success_cleans_every_owned_resource(self) -> None:
        result, docker_log = _run_script(_successful_response())

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("stop --time 5 2xbrainz-browser-check-test-run", docker_log)
        self.assertIn("stop --time 5 2xbrainz-browser-fixture-test-run", docker_log)
        self.assertIn(f"image rm {_APP_IMAGE}", docker_log)
        self.assertIn("--rm", docker_log)
        self.assertIn(
            "--network container:2xbrainz-browser-fixture-test-run",
            docker_log,
        )
        self.assertIn(
            "--network container:2xbrainz-browser-check-test-run",
            docker_log,
        )
        self.assertNotIn("--network host", docker_log)

    def test_failed_browser_assertion_still_cleans_every_owned_resource(self) -> None:
        response = json.dumps(
            {
                "success": True,
                "data": {
                    "success": False,
                    "step_results": [
                        {"action": "click", "error": "selector was not found"}
                    ],
                },
            },
        )
        result, docker_log = _run_script(response)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("selector was not found", result.stdout)
        self.assertIn("stop --time 5 2xbrainz-browser-check-test-run", docker_log)
        self.assertIn("stop --time 5 2xbrainz-browser-fixture-test-run", docker_log)
        self.assertIn(f"image rm {_APP_IMAGE}", docker_log)

    def test_browser_console_error_fails_and_cleans_every_owned_resource(self) -> None:
        console_response = json.dumps(
            {
                "success": True,
                "data": {
                    "log": [
                        {
                            "type": "error",
                            "text": "Uncaught Error: each_key_duplicate",
                        }
                    ]
                },
            }
        )
        result, docker_log = _run_script(
            _successful_response(),
            console_response=console_response,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("browser console errors", result.stdout)
        self.assertIn("stop --time 5 2xbrainz-browser-check-test-run", docker_log)
        self.assertIn("stop --time 5 2xbrainz-browser-fixture-test-run", docker_log)
        self.assertIn(f"image rm {_APP_IMAGE}", docker_log)

    def test_cleanup_refuses_same_name_with_an_unexpected_image(self) -> None:
        result, docker_log = _run_script(
            _successful_response(),
            wrong_browser_image=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("stop --time 5 2xbrainz-browser-check-test-run", docker_log)
        self.assertIn("stop --time 5 2xbrainz-browser-fixture-test-run", docker_log)


def _run_script(
    response: str,
    *,
    console_response: str | None = None,
    wrong_browser_image: bool = False,
) -> tuple[subprocess.CompletedProcess[str], str]:
    environment_path = os.environ.get("UV_PROJECT_ENVIRONMENT")
    temporary_parent = (
        str(Path(environment_path).parent) if environment_path is not None else None
    )
    with tempfile.TemporaryDirectory(dir=temporary_parent) as temporary:
        directory = Path(temporary)
        binary_directory = directory / "bin"
        binary_directory.mkdir()
        docker_log = directory / "docker.log"
        fixture_log_directory = directory / "fixture-logs"
        fixture_log_directory.mkdir()
        _write_fixture_log(fixture_log_directory / "stream-observability.jsonl")
        _write_executable(binary_directory / "docker", _fake_docker())
        environment = {
            **os.environ,
            "PATH": f"{binary_directory}:{os.environ['PATH']}",
            "BROWSER_TEST_PROJECT_ROOT": str(_PROJECT_ROOT),
            "BROWSER_TEST_RUN_ID": "test-run",
            "BROWSER_TEST_APP_IMAGE": _APP_IMAGE,
            "BROWSER_TEST_IMAGE": _BROWSER_IMAGE,
            "BROWSER_TEST_LOG_FILE": str(directory / "browser.log"),
            "BROWSER_TEST_FIXTURE_LOG_DIRECTORY": str(fixture_log_directory),
            "FAKE_DOCKER_LOG": str(docker_log),
            "FAKE_BROWSER_RESPONSE": response,
            "FAKE_BROWSER_CONSOLE_RESPONSE": console_response
            or '{"success":true,"data":{"log":[]}}',
            "FAKE_WRONG_BROWSER_IMAGE": "1" if wrong_browser_image else "0",
        }
        result = subprocess.run(
            [str(_SCRIPT)],
            cwd=_PROJECT_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        if docker_log.exists():
            return result, docker_log.read_text(encoding="utf-8")
        diagnostic = (
            f"docker log missing\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        return result, diagnostic


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def _fake_docker() -> str:
    return textwrap.dedent(
        """\
        #!/bin/bash
        set -euo pipefail
        printf '%s\n' "$*" >>"$FAKE_DOCKER_LOG"
        if [[ "$1 $2" == "container inspect" ]]; then
            name="${@: -1}"
            if [[ "$name" == *browser-check* ]]; then
                if [[ "$FAKE_WRONG_BROWSER_IMAGE" == "1" ]]; then
                    printf '%s\n' 'unowned-image:latest'
                else
                    printf '%s\n' "$BROWSER_TEST_IMAGE"
                fi
            else
                printf '%s\n' "$BROWSER_TEST_APP_IMAGE"
            fi
        fi
        if [[ "$1" == "run" ]]; then
            method=""
            url=""
            previous=""
            for argument in "$@"; do
                if [[ "$previous" == "POST" || "$previous" == "GET" ]]; then
                    method="$previous"
                    url="$argument"
                    break
                fi
                previous="$argument"
            done
            if [[ "$method" == "POST" ]]; then
                payload="$(cat)"
                if [[ "$payload" == *'enable_console_log'* ]]; then
                    printf '%s\n' '{"success":true,"data":{"enabled":true}}'
                elif [[ "$payload" == *'get_console_log'* ]]; then
                    printf '%s\n' "$FAKE_BROWSER_CONSOLE_RESPONSE"
                else
                    printf '%s\n' "$FAKE_BROWSER_RESPONSE"
                fi
            elif [[ "$url" == *"/screenshot/"* ]]; then
                printf '\\x89PNG\\r\\n\\x1a\\n'
            elif [[ "$method" == "GET" ]]; then
                printf '%s\n' '{"status":"ok"}'
            fi
        fi
        """
    )


def _successful_response() -> str:
    result = {
        "appShell": True,
        "connected": True,
        "providerFeeds": 3,
        "providerAssignments": 3,
        "modelFilter": True,
        "settingsModalBounded": True,
        "settingsTabs": 3,
        "modelListScrollable": True,
        "modelOptionReadable": True,
        "modelSelectedInView": True,
        "modelResultCount": "120 of 120",
        "persistedDraftModel": "claudebox-provider-example-model-001",
        "selectedDraftModel": "claudebox-provider-example-model-001",
        "generationCards": 0,
        "replyItems": 6,
        "collapsedTraceRows": True,
        "replyText": "Start at the gateway, then follow validation and routing.",
        "allFeedsIdle": True,
        "coachCancelled": True,
        "storyFailed": True,
        "failureReasonVisible": True,
        "storyResponses": 2,
        "streamOrder": [
            "stream-status",
            "stream-event",
            "stream-event",
            "stream-status",
            "stream-event",
            "stream-response",
        ],
        "cleanText": True,
    }
    return json.dumps(
        {
            "success": True,
            "data": {
                "success": True,
                "outputs": {
                    "ui": {"result": result},
                    "reset": {"result": {"settingsCleared": True}},
                },
            },
        }
    )


def _write_fixture_log(path: Path) -> None:
    records = [
        {"msg": "fake AIGate SSE response started"},
        {"msg": "AIGate SSE event received"},
        {"msg": "provider activity retained"},
        {"msg": "web console snapshot streamed"},
        {"msg": "fake AIGate browser flow completed"},
        {
            "msg": "frontend stream diagnostic received",
            "frontend_event": "snapshot_received",
        },
        {
            "msg": "frontend stream diagnostic received",
            "frontend_event": "provider_feed_rendered",
        },
    ]
    for phase in (
        "reasoning_streaming",
        "tool_started",
        "tool_completed",
        "output_streaming",
        "request_completed",
    ):
        records.append({"msg": "provider activity emitted", "phase": phase})
    path.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )
