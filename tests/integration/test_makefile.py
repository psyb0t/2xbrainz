from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class MakefileIntegrationTests(unittest.TestCase):
    def test_run_mounts_project_logs_and_sets_container_log_directory(self) -> None:
        result = _dry_run("run")

        self.assertIn('-v "' + str(_PROJECT_ROOT) + '/logs:/logs:rw"', result.stdout)
        self.assertIn('TWOXBRAINZ_LOG_DIRECTORY="/logs"', result.stdout)
        self.assertIn("live --web-port 7860", result.stdout)
        self.assertNotIn("--output", result.stdout)

    def test_default_host_network_does_not_require_host_python(self) -> None:
        for target in ("run", "benchmark", "test-real"):
            with self.subTest(target=target):
                result = _dry_run(target)
                self.assertNotIn("python3 -m two_x_brainz.docker_hosts", result.stdout)

    def test_named_network_uses_the_validated_host_mapping_helper(self) -> None:
        for target in ("run", "benchmark", "test-real"):
            with self.subTest(target=target):
                result = _dry_run(target, "LIVE_NETWORK=app-network")
                self.assertIn("python3 -m two_x_brainz.docker_hosts", result.stdout)

    def test_named_network_web_mode_is_rejected_before_docker_run(self) -> None:
        result = _dry_run("run", "LIVE_NETWORK=app-network")
        guard_position = result.stdout.index("run requires LIVE_NETWORK=host")
        run_position = result.stdout.index("docker run", guard_position)
        self.assertLess(guard_position, run_position)

    def test_run_has_no_launch_time_audio_setup_override(self) -> None:
        result = _dry_run("run", "SELECT_AUDIO=true")

        self.assertNotIn("--select-audio", result.stdout)

    def test_logs_mounts_the_same_project_directory_read_only(self) -> None:
        result = _dry_run("logs")

        self.assertIn('-v "' + str(_PROJECT_ROOT) + '/logs:/logs:ro"', result.stdout)
        self.assertIn('TWOXBRAINZ_LOG_NAME=""', result.stdout)
        self.assertIn("--entrypoint /bin/sh", result.stdout)
        self.assertIn('find "/logs"', result.stdout)

    def test_doctor_loads_the_gitignored_runtime_environment(self) -> None:
        result = _dry_run("doctor")

        self.assertIn('--env-file ".env"', result.stdout)

    def test_local_test_targets_remove_the_exact_development_image(self) -> None:
        for target in (
            "lint",
            "test",
            "test-unit",
            "test-integration",
            "test-coverage",
        ):
            with self.subTest(target=target):
                result = _dry_run(target)

                self.assertIn(
                    'docker image inspect "$image"',
                    result.stdout,
                )
                self.assertIn(
                    'docker image rm "$image"',
                    result.stdout,
                )
                self.assertIn("for image in 2xbrainz-dev:local", result.stdout)
                self.assertNotIn("docker image rm --force", result.stdout)
                self.assertNotIn("docker image prune", result.stdout)

    def test_real_test_removes_only_its_dedicated_image(self) -> None:
        result = _dry_run("test-real")

        self.assertIn("for image in 2xbrainz-test-real:local", result.stdout)
        self.assertIn("real_aigate_prompts.py", result.stdout)
        self.assertIn("real_talkies_concurrency.py", result.stdout)
        self.assertIn("real_interrupted_audio_research.py", result.stdout)
        self.assertIn("TWOXBRAINZ_CONCURRENCY_AUDIO=/fixture/audio.wav", result.stdout)
        self.assertIn("commons-audio-cc0.wav:/fixture/audio.wav:ro", result.stdout)
        self.assertNotIn("for image in 2xbrainz:local", result.stdout)
        self.assertNotIn("docker image rm --force", result.stdout)
        self.assertNotIn("docker image prune", result.stdout)

    def test_focused_real_talkies_test_uses_the_concurrency_fixture(self) -> None:
        result = _dry_run(
            "test-real-talkies",
            "TALKIES_MODEL=nemotron-3.5-asr-0.6b",
        )

        self.assertIn("real_talkies_concurrency.py", result.stdout)
        self.assertIn(
            'TWOXBRAINZ_FIXTURE_TALKIES_MODEL="nemotron-3.5-asr-0.6b"',
            result.stdout,
        )
        self.assertIn("for image in 2xbrainz-test-real:local", result.stdout)

    def test_real_audio_research_uses_full_fixture_and_cleans_image(self) -> None:
        result = _dry_run(
            "test-real-audio-research",
            "TALKIES_MODEL=local-talkies-cuda-nemotron-3.5-asr-0.6b",
            "FIXTURE_AIGATE_RESEARCH_MODEL=claudebox-sonnet",
        )

        self.assertIn("real_interrupted_audio_research.py", result.stdout)
        self.assertIn("live_talkies_tts_fixture.py", result.stdout)
        self.assertIn("real_aigate_prompts.py", result.stdout)
        self.assertIn(
            'TWOXBRAINZ_FIXTURE_TALKIES_MODEL="local-talkies-cuda-nemotron-3.5-asr-0.6b"',
            result.stdout,
        )
        self.assertIn(
            'TWOXBRAINZ_FIXTURE_RESEARCH_MODEL="claudebox-sonnet"',
            result.stdout,
        )
        self.assertIn("--tmpfs /fixture-work:rw,exec,nosuid,size=512m", result.stdout)
        self.assertIn("for image in 2xbrainz-test-real:local", result.stdout)
        self.assertNotIn("docker image rm --force", result.stdout)
        self.assertNotIn("docker image prune", result.stdout)

    def test_real_evaluation_mounts_scenario_and_cleans_its_image(self) -> None:
        result = _dry_run(
            "test-real-evaluation",
            "TALKIES_MODEL=custom-asr",
            "FIXTURE_AIGATE_DRAFT_MODEL=custom-reply",
            "FIXTURE_AIGATE_COMMENTARY_MODEL=custom-coach",
            "FIXTURE_AIGATE_SUMMARY_MODEL=custom-story",
            "FIXTURE_AIGATE_RESEARCH_MODEL=claudebox-sonnet",
        )

        self.assertIn("real_conversation_evaluation.py", result.stdout)
        self.assertIn("live_talkies_tts_fixture.py", result.stdout)
        self.assertIn(
            "slang-interrupted-project-chat.json:/fixture/scenario.json:ro",
            result.stdout,
        )
        self.assertIn(
            "TWOXBRAINZ_EVALUATION_SCENARIO=/fixture/scenario.json",
            result.stdout,
        )
        self.assertIn('TWOXBRAINZ_EVALUATION_REPEATS="3"', result.stdout)
        self.assertIn(
            'TWOXBRAINZ_FIXTURE_TALKIES_MODEL="custom-asr"',
            result.stdout,
        )
        self.assertIn(
            'TWOXBRAINZ_FIXTURE_DRAFT_MODEL="custom-reply"',
            result.stdout,
        )
        self.assertIn(
            'TWOXBRAINZ_FIXTURE_COMMENTARY_MODEL="custom-coach"',
            result.stdout,
        )
        self.assertIn(
            'TWOXBRAINZ_FIXTURE_SUMMARY_MODEL="custom-story"',
            result.stdout,
        )
        self.assertIn(
            'TWOXBRAINZ_FIXTURE_RESEARCH_MODEL="claudebox-sonnet"',
            result.stdout,
        )
        self.assertIn("--tmpfs /fixture-work:rw,exec,nosuid,size=512m", result.stdout)
        self.assertIn("for image in 2xbrainz-test-real:local", result.stdout)
        self.assertNotIn("docker image rm --force", result.stdout)
        self.assertNotIn("docker image prune", result.stdout)

    def test_evaluation_report_uses_only_local_artifacts_and_cleans_image(self) -> None:
        result = _dry_run(
            "evaluation-report",
            "EVALUATION_RUN=scenario-suite-123",
        )

        self.assertIn("two_x_brainz.evaluation_report", result.stdout)
        self.assertIn(
            'TWOXBRAINZ_EVALUATION_RUN="scenario-suite-123"',
            result.stdout,
        )
        self.assertIn(
            ".testing/fixture-traces:/workspace/.testing/fixture-traces:rw",
            result.stdout,
        )
        self.assertNotIn("--env-file", result.stdout)
        self.assertNotIn("--network", result.stdout)
        self.assertIn("for image in 2xbrainz-dev:local", result.stdout)

    def test_browser_test_uses_the_cleanup_owned_runner(self) -> None:
        result = _dry_run("test-browser")

        self.assertIn("./scripts/browser_smoke.sh", result.stdout)
        self.assertIn(
            'BROWSER_TEST_APP_IMAGE="2xbrainz-browser-test:local"',
            result.stdout,
        )
        self.assertIn("psyb0t/stealthy-auto-browse@sha256:", result.stdout)


def _dry_run(target: str, *variables: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "--no-print-directory", "-n", target, *variables],
        cwd=_PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
