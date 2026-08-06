from __future__ import annotations

import asyncio
import importlib.util
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from two_x_brainz.config import Settings
from two_x_brainz.fixture_trace import FixtureTrace

_CONCURRENCY_SCRIPT = Path("tests/integration/real_talkies_concurrency.py")


def _load_concurrency_module() -> Any:
    specification = importlib.util.spec_from_file_location(
        "real_talkies_concurrency",
        _CONCURRENCY_SCRIPT,
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


_CONCURRENCY = _load_concurrency_module()


class ReadyBarrierTests(unittest.TestCase):
    def test_releases_only_after_two_streams_are_ready(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as directory:
                trace = FixtureTrace(Path(directory), "ready-barrier")
                barrier = _CONCURRENCY.ReadyBarrier(2, trace)
                first = asyncio.create_task(barrier.wait("first"))
                await asyncio.sleep(0)
                self.assertFalse(first.done())

                second = asyncio.create_task(barrier.wait("second"))
                await asyncio.gather(first, second)
                trace.close()

        asyncio.run(run())

    def test_rejects_a_model_that_advertises_only_one_request(self) -> None:
        class SerialClient:
            def __init__(self, _config: object) -> None:
                return

            async def configured_model_max_concurrency(self) -> int:
                return 1

        async def run() -> None:
            with TemporaryDirectory() as directory:
                directory_path = Path(directory)
                trace = FixtureTrace(directory_path, "serial-model")
                settings = Settings(
                    talkies_ws_url=(
                        "ws://aigate.example/talkies/v1/audio/transcriptions/stream"
                    ),
                    talkies_model="fixture-model",
                    talkies_token="test-token",
                    aigate_url="http://aigate.example/v1",
                    aigate_reply_model=None,
                    aigate_token="test-token",
                    log_level="INFO",
                    log_file=directory_path / "test.log",
                )
                with (
                    patch.object(_CONCURRENCY, "TalkiesClient", SerialClient),
                    patch.dict(
                        os.environ,
                        {
                            _CONCURRENCY._AUDIO_PATH_ENV: str(
                                Path("tests/fixtures/commons-audio-cc0.wav").resolve()
                            )
                        },
                    ),
                    self.assertRaisesRegex(
                        _CONCURRENCY.TalkiesConcurrencyFixtureError,
                        "does not advertise two",
                    ),
                ):
                    await _CONCURRENCY._run_with_trace(settings, trace)
                trace.close()

        asyncio.run(run())

    def test_times_out_when_a_serialized_second_stream_never_reaches_ready(
        self,
    ) -> None:
        async def run() -> None:
            with TemporaryDirectory() as directory:
                trace = FixtureTrace(Path(directory), "serialized-stream")
                barrier = _CONCURRENCY.ReadyBarrier(2, trace)
                try:
                    with (
                        patch.object(
                            _CONCURRENCY,
                            "_READY_BARRIER_TIMEOUT_SECONDS",
                            0.01,
                        ),
                        self.assertRaises(TimeoutError),
                    ):
                        await barrier.wait("only-stream")
                finally:
                    trace.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
