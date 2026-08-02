from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from two_x_brainz.constants import ENV_AIGATE_URL, ENV_TALKIES_WS_URL
from two_x_brainz.docker_hosts import docker_host_arguments


class DockerHostArgumentsTests(unittest.TestCase):
    def test_maps_host_resolved_provider_names_without_reading_tokens(self) -> None:
        contents = """\
TWOXBRAINZ_AIGATE_URL=http://aigate.ts.example
TWOXBRAINZ_TALKIES_WS_URL=wss://talkies.ts.example/v1/audio/transcriptions/stream
TWOXBRAINZ_AIGATE_TOKEN=not-in-output
"""
        with tempfile.TemporaryDirectory() as directory:
            environment = _write_environment(Path(directory), contents)
            arguments = docker_host_arguments(
                environment,
                lambda hostname: {
                    "aigate.ts.example": "192.0.2.12",
                    "talkies.ts.example": "192.0.2.13",
                }.get(hostname),
            )

        self.assertEqual(
            arguments,
            (
                "--add-host=aigate.ts.example:192.0.2.12",
                "--add-host=talkies.ts.example:192.0.2.13",
            ),
        )
        self.assertNotIn("not-in-output", " ".join(arguments))

    def test_skips_service_names_invalid_urls_and_unresolved_hosts(self) -> None:
        contents = """\
TWOXBRAINZ_AIGATE_URL=http://aigate:4000
TWOXBRAINZ_TALKIES_WS_URL=not-a-url
TWOXBRAINZ_AIGATE_URL=https://unresolved.ts.example
OTHER_URL=https://ignored.ts.example
"""
        with tempfile.TemporaryDirectory() as directory:
            environment = _write_environment(Path(directory), contents)
            arguments = docker_host_arguments(environment, lambda hostname: None)

        self.assertEqual(arguments, ())

    def test_rejects_unsafe_endpoint_parts_and_non_ipv4_resolver_output(self) -> None:
        unsafe_endpoints = """\
TWOXBRAINZ_AIGATE_URL=https://user:password@credential.ts.example
TWOXBRAINZ_TALKIES_WS_URL=wss://query.ts.example/stream?token=unsafe
"""
        shell_unsafe_hostname = "TWOXBRAINZ_AIGATE_URL=https://unsafe.ts.example;echo\n"
        valid_endpoint = "TWOXBRAINZ_AIGATE_URL=https://valid.ts.example\n"
        with tempfile.TemporaryDirectory() as directory:
            environment = _write_environment(Path(directory), unsafe_endpoints)
            unsafe_arguments = docker_host_arguments(
                environment,
                lambda hostname: "192.0.2.12",
            )
            environment = _write_environment(Path(directory), shell_unsafe_hostname)
            shell_unsafe_arguments = docker_host_arguments(
                environment,
                lambda hostname: "192.0.2.12",
            )
            environment = _write_environment(Path(directory), valid_endpoint)
            invalid_address_arguments = docker_host_arguments(
                environment,
                lambda hostname: "not-an-ip-address",
            )

        self.assertEqual(unsafe_arguments, ())
        self.assertEqual(shell_unsafe_arguments, ())
        self.assertEqual(invalid_address_arguments, ())

    def test_maps_safe_one_shot_endpoint_overrides(self) -> None:
        contents = "TWOXBRAINZ_AIGATE_TOKEN=not-in-output\n"
        overrides = (
            (ENV_AIGATE_URL, "http://aigate.ts.example"),
            (
                ENV_TALKIES_WS_URL,
                "ws://aigate.ts.example/talkies/v1/audio/transcriptions/stream",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            environment = _write_environment(Path(directory), contents)
            arguments = docker_host_arguments(
                environment,
                lambda hostname: "192.0.2.12"
                if hostname == "aigate.ts.example"
                else None,
                overrides,
            )

        self.assertEqual(arguments, ("--add-host=aigate.ts.example:192.0.2.12",))


def _write_environment(directory: Path, contents: str) -> Path:
    environment = directory / "test.env"
    environment.write_text(contents, encoding="utf-8")
    return environment
