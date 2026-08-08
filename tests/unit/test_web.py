from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from typing import cast

from two_x_brainz.audio_selection import (
    AudioDevice,
    AudioSelectionSetup,
)
from two_x_brainz.errors import WebConsoleError
from two_x_brainz.terminal import LiveTerminal
from two_x_brainz.web import WebConsole


class WebConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_uses_structured_sanitized_console_state(self) -> None:
        state = LiveTerminal(log_file="/tmp/2xbrainz-web.log", stream=io.StringIO())
        state.activate_presentation()
        state.set_audio_level("user", 54)
        state.set_audio_level("remote", 27)
        console = WebConsole(state)
        console.consume({"kind": "session", "state": "running", "action": "started"})
        console.consume(
            {
                "kind": "transcript",
                "speaker_role": "user",
                "is_final": False,
                "text": "A partial \x1b[31m control sequence",
            }
        )
        console.consume(
            {
                "kind": "timeline",
                "speaker_role": "remote",
                "text": "A finalized remote turn.",
            }
        )
        console.consume(
            {
                "kind": "draft",
                "status": "completed",
                "text": "A concise reply suggestion.",
            }
        )
        console.consume(
            {
                "kind": "commentary",
                "status": "completed",
                "text": "Keep the answer grounded in the conversation.",
            }
        )
        console.consume(
            {
                "kind": "summary",
                "status": "completed",
                "text": "The discussion has one current question.",
            }
        )

        snapshot = console.snapshot()
        payload = snapshot.payload()

        self.assertIn("RUNNING", snapshot.status)
        self.assertIn("Them", snapshot.conversation)
        self.assertIn("A partial �[31m control sequence", snapshot.conversation)
        self.assertNotIn("\x1b[31m", snapshot.conversation)
        self.assertIn("A concise reply suggestion.", snapshot.reply)
        self.assertIn("PRIVATE COACH", snapshot.coach)
        self.assertIn("STORY SO FAR", snapshot.story)
        self.assertEqual(payload["type"], "snapshot")
        active_audio = cast(dict[str, object], payload["activeAudio"])
        microphone = cast(dict[str, object], active_audio["microphone"])
        system = cast(dict[str, object], active_audio["system"])
        self.assertEqual(microphone["level"], 54)
        self.assertEqual(system["level"], 27)

    async def test_snapshot_lists_every_candidate_as_a_separate_meter(self) -> None:
        setup = _audio_setup()
        state = LiveTerminal(
            log_file="/tmp/2xbrainz-web.log",
            stream=io.StringIO(),
            audio_setup=setup,
            _setup_preview_enabled=False,
        )
        state.start_setup_audio_metering(setup.microphones, setup.system_monitors)
        state.set_setup_audio_level("user", "mic", 50)
        state.set_setup_audio_level("user", "backup-mic", 75)
        state.set_setup_audio_level("remote", "system", 25)
        state.set_setup_audio_level("remote", "backup-system", 100)

        snapshot = WebConsole(state).snapshot()

        self.assertEqual(
            [meter.label for meter in snapshot.microphones],
            ["Microphone", "Backup microphone"],
        )
        self.assertEqual([meter.level for meter in snapshot.microphones], [50, 75])
        self.assertEqual([meter.level for meter in snapshot.system_monitors], [25, 100])
        self.assertTrue(snapshot.microphones[0].is_default)
        self.assertTrue(all(meter.is_available for meter in snapshot.microphones))

    async def test_controls_use_the_existing_runtime_queue(self) -> None:
        state = LiveTerminal(log_file="/tmp/2xbrainz-web.log", stream=io.StringIO())
        console = WebConsole(state)

        console.pause()
        console.resume()

        self.assertEqual(await anext(console.control_lines()), "pause")
        self.assertEqual(await anext(console.control_lines()), "resume")

    async def test_provider_activity_history_is_bounded(self) -> None:
        console = WebConsole(
            LiveTerminal(log_file="/tmp/2xbrainz-web.log", stream=io.StringIO())
        )

        for index in range(100):
            console.record_provider_activity(
                {"phase": "request_completed", "model": f"model-{index}"}
            )

        activity = console.snapshot().provider_activity
        self.assertEqual(len(activity), 80)
        self.assertEqual(activity[0]["model"], "model-20")
        self.assertEqual(activity[-1]["model"], "model-99")

    async def test_adjacent_streaming_activity_updates_in_place(self) -> None:
        console = WebConsole(
            LiveTerminal(log_file="/tmp/2xbrainz-web.log", stream=io.StringIO())
        )
        with self.assertLogs("two_x_brainz.web", level="DEBUG") as captured:
            console.record_provider_activity(
                {
                    "phase": "tool_completed",
                    "flow_id": "flow-a",
                    "tool_result": "result",
                }
            )
            for output in ("A", "An", "Answer"):
                console.record_provider_activity(
                    {
                        "phase": "output_streaming",
                        "flow_id": "flow-a",
                        "output": output,
                    }
                )

        activity = console.snapshot().provider_activity

        self.assertEqual(len(activity), 2)
        self.assertEqual(activity[0]["tool_result"], "result")
        self.assertEqual(activity[1]["output"], "Answer")
        self.assertEqual(
            [record.getMessage() for record in captured.records],
            [
                "provider activity retained",
                "provider stream activity retained",
                "provider stream activity coalesced",
                "provider stream activity coalesced",
            ],
        )
        self.assertEqual(captured.records[-1].__dict__["activity_revision"], 4)

    async def test_intervening_activity_preserves_repeated_stream_phase_order(
        self,
    ) -> None:
        console = WebConsole(
            LiveTerminal(log_file="/tmp/2xbrainz-web.log", stream=io.StringIO())
        )
        for activity in (
            {
                "phase": "reasoning_streaming",
                "flow_id": "flow-a",
                "reasoning": "Reason before research",
            },
            {
                "phase": "tool_completed",
                "flow_id": "flow-a",
                "tool_result": "result",
            },
            {
                "phase": "reasoning_streaming",
                "flow_id": "flow-a",
                "reasoning": "Reason after research",
            },
        ):
            console.record_provider_activity(activity)

        retained = console.snapshot().provider_activity

        self.assertEqual(
            [entry["phase"] for entry in retained],
            ["reasoning_streaming", "tool_completed", "reasoning_streaming"],
        )
        self.assertEqual(retained[0]["reasoning"], "Reason before research")
        self.assertEqual(retained[2]["reasoning"], "Reason after research")

    async def test_parallel_flows_coalesce_independently(self) -> None:
        console = WebConsole(
            LiveTerminal(log_file="/tmp/2xbrainz-web.log", stream=io.StringIO())
        )
        for activity in (
            {
                "phase": "reasoning_streaming",
                "flow_id": "draft-flow",
                "output_kind": "draft",
                "reasoning": "Draft",
            },
            {
                "phase": "reasoning_streaming",
                "flow_id": "summary-flow",
                "output_kind": "summary",
                "reasoning": "Story",
            },
            {
                "phase": "reasoning_streaming",
                "flow_id": "draft-flow",
                "output_kind": "draft",
                "reasoning": "Draft reasoning complete",
            },
            {
                "phase": "reasoning_streaming",
                "flow_id": "summary-flow",
                "output_kind": "summary",
                "reasoning": "Story reasoning complete",
            },
        ):
            console.record_provider_activity(activity)

        retained = console.snapshot().provider_activity

        self.assertEqual(len(retained), 2)
        self.assertEqual(retained[0]["reasoning"], "Draft reasoning complete")
        self.assertEqual(retained[1]["reasoning"], "Story reasoning complete")

    async def test_reasoning_and_output_streams_coalesce_through_each_other(
        self,
    ) -> None:
        console = WebConsole(
            LiveTerminal(log_file="/tmp/2xbrainz-web.log", stream=io.StringIO())
        )
        for activity in (
            {
                "phase": "output_streaming",
                "flow_id": "flow-a",
                "output": "A",
            },
            {
                "phase": "reasoning_streaming",
                "flow_id": "flow-a",
                "reasoning": "Think",
            },
            {
                "phase": "output_streaming",
                "flow_id": "flow-a",
                "output": "Answer",
            },
            {
                "phase": "reasoning_streaming",
                "flow_id": "flow-a",
                "reasoning": "Thinking complete",
            },
        ):
            console.record_provider_activity(activity)

        retained = console.snapshot().provider_activity

        self.assertEqual(len(retained), 2)
        self.assertEqual(retained[0]["output"], "Answer")
        self.assertEqual(retained[1]["reasoning"], "Thinking complete")

    async def test_invalid_port_is_rejected_before_static_asset_check(self) -> None:
        console = WebConsole(
            LiveTerminal(log_file="/tmp/2xbrainz-web.log", stream=io.StringIO()),
            port=80,
            static_directory=Path("/does-not-exist"),
        )

        with self.assertRaisesRegex(ValueError, "outside the allowed range"):
            await console.open()

    async def test_missing_compiled_frontend_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            console = WebConsole(
                LiveTerminal(log_file="/tmp/2xbrainz-web.log", stream=io.StringIO()),
                static_directory=Path(temporary_directory),
            )
            with self.assertRaisesRegex(WebConsoleError, "compiled Svelte"):
                await console.open()


def _audio_setup() -> AudioSelectionSetup:
    return AudioSelectionSetup(
        microphones=(
            AudioDevice("1", "mic", "Audio/Source", "Microphone", True),
            AudioDevice("3", "backup-mic", "Audio/Source", "Backup microphone", False),
        ),
        system_monitors=(
            AudioDevice("2", "system", "Audio/Source", "System monitor", True),
            AudioDevice(
                "4",
                "backup-system",
                "Audio/Source",
                "Backup system monitor",
                False,
            ),
        ),
        selection=None,
    )
