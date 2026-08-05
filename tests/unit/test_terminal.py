from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar, cast
from unittest.mock import Mock, patch

from textual.containers import VerticalScroll
from textual.widgets import Input, OptionList, Static

from two_x_brainz.audio_selection import (
    AudioDevice,
    AudioSelectionSetup,
    AudioSelectionStore,
)
from two_x_brainz.errors import CaptureError
from two_x_brainz.terminal import LiveTerminal, OperatorConsole

_STATUS_SELECTOR = "#status"
_CONVERSATION_SELECTOR = "#conversation"
_CONVERSATION_CONTENT_SELECTOR = "#conversation-content"
_GUIDANCE_SELECTOR = "#guidance"
_SETUP_SELECTOR = "#audio-setup"


class LiveTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_console_shows_status_conversation_and_guidance(
        self,
    ) -> None:
        terminal = _terminal()
        terminal.consume({"kind": "session", "state": "running", "action": "started"})
        terminal.consume(
            {
                "kind": "transcript",
                "speaker_role": "user",
                "is_final": False,
                "text": "A partial \x1b]unsafe terminal control",
            }
        )
        terminal.consume(
            {
                "kind": "timeline",
                "speaker_role": "remote",
                "text": "A finalized remote turn.",
            }
        )
        terminal.consume(
            {
                "kind": "summary",
                "status": "completed",
                "text": "The call is discussing a current topic.",
            }
        )
        terminal.consume(
            {
                "kind": "draft",
                "status": "running",
                "text": "",
            }
        )

        app = OperatorConsole(terminal)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            status = app.query_one(_STATUS_SELECTOR, Static)
            conversation = app.query_one(_CONVERSATION_SELECTOR, VerticalScroll)
            conversation_content = app.query_one(
                _CONVERSATION_CONTENT_SELECTOR,
                Static,
            )
            guidance = app.query_one("#guidance-content", Static)

            self.assertIn("RUNNING", str(status.content))
            self.assertIn("Calling reply LLM", str(status.content))
            self.assertIn("Them", str(conversation_content.content))
            self.assertIn(
                "A partial �]unsafe terminal control", str(conversation_content.content)
            )
            self.assertIn("STORY SO FAR", str(guidance.content))
            self.assertNotIn("\x1b]unsafe", str(conversation_content.content))
            self.assertGreaterEqual(conversation.max_scroll_y, 0)

    async def test_operator_console_submits_commands_through_existing_control_channel(
        self,
    ) -> None:
        terminal = _terminal()
        app = OperatorConsole(terminal)

        async with app.run_test() as pilot:
            command_input = app.query_one(Input)
            command_input.focus()
            command_input.value = "pause"
            await pilot.press("enter")
            self.assertEqual(await anext(terminal.control_lines()), "pause")

    async def test_ctrl_q_queues_clean_stop_command(self) -> None:
        terminal = _terminal()
        app = OperatorConsole(terminal)

        async with app.run_test() as pilot:
            await pilot.press("ctrl+q")
            self.assertEqual(await anext(terminal.control_lines()), "stop")

    async def test_ctrl_c_queues_clean_stop_command(self) -> None:
        terminal = _terminal()
        app = OperatorConsole(terminal)

        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
            self.assertEqual(await anext(terminal.control_lines()), "stop")

    async def test_close_cancels_a_terminal_task_after_the_shutdown_deadline(
        self,
    ) -> None:
        terminal = _close_harness()
        app_mock = Mock()
        app = cast(OperatorConsole, app_mock)
        task = asyncio.create_task(_wait_forever())
        terminal.set_running_app(app, task)

        with patch("two_x_brainz.terminal._TERMINAL_CLOSE_TIMEOUT_SECONDS", 0):
            await terminal.close()

        app_mock.exit.assert_called_once()
        self.assertTrue(task.cancelled())

    async def test_first_run_audio_setup_saves_then_opens_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            setup = _audio_setup(Path(temporary_directory) / "audio.json")
            terminal = _terminal(audio_setup=setup)
            app = OperatorConsole(terminal)

            async with app.run_test(size=(100, 30)) as pilot:
                self.assertTrue(app.query_one(_SETUP_SELECTOR).display)
                self.assertFalse(app.query_one("#main").display)

                await pilot.press("enter", "enter")

                selection = _require_selection(self, setup)
                self.assertEqual(selection.mic_node, "default-mic")
                self.assertEqual(selection.system_node, "default-sink.monitor")
                self.assertTrue(app.query_one(_CONVERSATION_SELECTOR).display)
                self.assertFalse(app.query_one(_SETUP_SELECTOR).display)

    async def test_audio_setup_shows_a_live_level_for_every_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            setup = _audio_setup(Path(temporary_directory) / "audio.json")
            terminal = _terminal(audio_setup=setup)
            app = OperatorConsole(terminal)

            async with app.run_test(size=(100, 30)):
                terminal.set_setup_audio_level("user", "default-mic", 75)
                terminal.set_setup_audio_level("user", "backup-mic", 50)
                terminal.set_setup_audio_level("remote", "default-sink.monitor", 25)
                terminal.set_setup_audio_level("remote", "backup-sink.monitor", 100)
                await asyncio.sleep(0.3)
                microphone_list = app.query_one("#setup-microphone", OptionList)
                system_list = app.query_one("#setup-system", OptionList)

                microphone_labels = _option_prompts(microphone_list)
                system_labels = _option_prompts(system_list)
                self.assertIn("Default microphone", microphone_labels)
                self.assertIn("Backup microphone", microphone_labels)
                self.assertIn("75%", microphone_labels)
                self.assertIn("50%", microphone_labels)
                self.assertIn("Default system monitor", system_labels)
                self.assertIn("Backup system monitor", system_labels)
                self.assertIn("25%", system_labels)
                self.assertIn("100%", system_labels)

    async def test_audio_setup_starts_one_meter_set_for_every_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            setup = _audio_setup(Path(temporary_directory) / "audio.json")
            terminal = _terminal(audio_setup=setup)
            app = OperatorConsole(terminal)

            with patch.object(
                LiveTerminal,
                "start_setup_audio_metering",
            ) as metering:
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.press("down")

                microphones, system_monitors = metering.call_args.args
                self.assertEqual(microphones, setup.microphones)
                self.assertEqual(system_monitors, setup.system_monitors)
                self.assertEqual(metering.call_count, 1)

    async def test_setup_meter_failure_does_not_stop_other_candidate_meters(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            setup = _audio_setup(Path(temporary_directory) / "audio.json")
            setup.system_monitors = (
                *setup.system_monitors,
                AudioDevice(
                    node_id="5",
                    name="direct-sink",
                    media_class="Audio/Sink",
                    label="Direct system sink",
                    is_default=False,
                ),
            )
            terminal = LiveTerminal(
                log_file="/logs/session.log",
                audio_setup=setup,
                stream=io.StringIO(),
            )

            _FakePipeWireSource.calls = []
            with patch("two_x_brainz.terminal.PipeWireSource", _FakePipeWireSource):
                terminal.start_setup_audio_metering(
                    setup.microphones,
                    setup.system_monitors,
                )
                await asyncio.sleep(0)
                await asyncio.sleep(0)

            microphone_labels = terminal.setup_audio_devices_text("user").plain
            system_labels = terminal.setup_audio_devices_text("remote").plain
            self.assertIn("100%", microphone_labels)
            self.assertIn("unavailable", microphone_labels)
            self.assertIn("100%", system_labels)
            self.assertIn(("default-mic", False), _FakePipeWireSource.calls)
            self.assertIn(("default-sink.monitor", False), _FakePipeWireSource.calls)
            self.assertIn(("direct-sink", True), _FakePipeWireSource.calls)
            await terminal.stop_setup_audio_preview()

    async def test_setup_shortcut_saves_a_next_session_pair_without_replacing_capture(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            setup = _audio_setup(Path(temporary_directory) / "audio.json")
            terminal = _terminal(audio_setup=setup)
            initial_selection = setup.select(0, 0)
            terminal.apply_audio_selection(initial_selection)
            app = OperatorConsole(terminal)

            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.press("f3")
                await pilot.press("down", "enter", "down", "enter")

                selection = _require_selection(self, setup)
                self.assertEqual(selection.mic_node, "backup-mic")
                self.assertEqual(selection.system_node, "backup-sink.monitor")
                self.assertEqual(terminal.microphone_node, "default-mic")
                self.assertEqual(terminal.system_node, "default-sink.monitor")
                self.assertTrue(app.query_one(_CONVERSATION_SELECTOR).display)

    async def test_ctrl_q_cancels_first_run_setup_without_a_capture_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            terminal = _terminal(
                audio_setup=_audio_setup(Path(temporary_directory) / "audio.json")
            )
            app = OperatorConsole(terminal)

            async with app.run_test() as pilot:
                await pilot.press("ctrl+q")

            self.assertTrue(terminal.audio_setup_cancelled)

    async def test_guidance_scrolls_and_view_mode_can_expand_it(self) -> None:
        terminal = _terminal()
        terminal.consume(
            {
                "kind": "draft",
                "status": "completed",
                "text": "long guidance " * 200,
            }
        )
        app = OperatorConsole(terminal)

        async with app.run_test(size=(80, 12)) as pilot:
            guidance = app.query_one(_GUIDANCE_SELECTOR, VerticalScroll)
            self.assertGreater(guidance.max_scroll_y, 0)
            guidance.focus()
            await pilot.press("end")
            self.assertEqual(guidance.scroll_y, guidance.max_scroll_y)

            await pilot.press("f2", "f2")
            self.assertEqual(app.view_mode, "guidance")
            self.assertTrue(guidance.display)
            self.assertFalse(
                app.query_one(_CONVERSATION_SELECTOR, VerticalScroll).display
            )

    def test_selected_sources_and_levels_remain_visible(self) -> None:
        terminal = _terminal()
        terminal.set_audio_level("user", 75)
        terminal.set_audio_level("remote", 0)

        rendered = str(terminal.sources_text())

        self.assertIn("Desk microphone", rendered)
        self.assertIn("Headphones monitor", rendered)
        self.assertIn("75%", rendered)
        self.assertIn("0%", rendered)

    async def test_console_keeps_manual_scroll_position(
        self,
    ) -> None:
        terminal = _terminal()
        for index in range(40):
            terminal.consume(
                {
                    "kind": "timeline",
                    "speaker_role": "user",
                    "text": f"Turn {index}",
                }
            )
        app = OperatorConsole(terminal)

        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            conversation = app.query_one(_CONVERSATION_SELECTOR, VerticalScroll)
            await pilot.press("home")
            await pilot.pause()
            before_update = conversation.scroll_y
            terminal.consume(
                {
                    "kind": "timeline",
                    "speaker_role": "remote",
                    "text": "A new remote turn.",
                }
            )
            await pilot.pause()
            self.assertEqual(conversation.scroll_y, before_update)


class NonInteractiveTerminalTests(unittest.TestCase):
    def test_noninteractive_output_is_readable_and_not_json(self) -> None:
        stream = io.StringIO()
        terminal = LiveTerminal(
            microphone_node="mic-node",
            system_node="system-node",
            log_file="/audio-config/2xbrainz.log",
            stream=stream,
        )

        terminal.consume(
            {
                "kind": "timeline",
                "speaker_role": "user",
                "text": "A finalized local turn.",
            }
        )
        terminal.consume(
            {
                "kind": "draft",
                "status": "completed",
                "text": "A readable suggestion.",
            }
        )

        rendered = stream.getvalue()
        self.assertIn("You: A finalized local turn.", rendered)
        self.assertIn("Reply suggestion: A readable suggestion.", rendered)
        self.assertNotIn('"schema_version"', rendered)


def _terminal(audio_setup: AudioSelectionSetup | None = None) -> LiveTerminal:
    return LiveTerminal(
        microphone_node="mic-node",
        system_node="system-node",
        log_file="/logs/session.log",
        microphone_label="Desk microphone",
        system_label="Headphones monitor",
        audio_setup=audio_setup,
        _setup_preview_enabled=False,
    )


class _TerminalCloseHarness(LiveTerminal):
    def set_running_app(
        self,
        app: OperatorConsole,
        task: asyncio.Task[object],
    ) -> None:
        self._app = app
        self._app_task = task


def _close_harness() -> _TerminalCloseHarness:
    return _TerminalCloseHarness(
        microphone_node="mic-node",
        system_node="system-node",
        log_file="/logs/session.log",
        microphone_label="Desk microphone",
        system_label="Headphones monitor",
        _setup_preview_enabled=False,
    )


async def _wait_forever() -> object:
    await asyncio.Event().wait()
    return None


def _audio_setup(path: Path) -> AudioSelectionSetup:
    return AudioSelectionSetup(
        store=AudioSelectionStore(path),
        microphones=(
            AudioDevice(
                node_id="1",
                name="default-mic",
                media_class="Audio/Source",
                label="Default microphone",
                is_default=True,
            ),
            AudioDevice(
                node_id="2",
                name="backup-mic",
                media_class="Audio/Source",
                label="Backup microphone",
                is_default=False,
            ),
        ),
        system_monitors=(
            AudioDevice(
                node_id="3",
                name="default-sink.monitor",
                media_class="Audio/Source",
                label="Default system monitor",
                is_default=True,
            ),
            AudioDevice(
                node_id="4",
                name="backup-sink.monitor",
                media_class="Audio/Source",
                label="Backup system monitor",
                is_default=False,
            ),
        ),
        selection=None,
    )


def _require_selection(
    test_case: unittest.TestCase,
    setup: AudioSelectionSetup,
):
    selection = setup.selection
    if selection is None:
        test_case.fail("audio setup did not persist a selection")
    return selection


def _option_prompts(option_list: OptionList) -> str:
    return "\n".join(
        str(option_list.get_option_at_index(index).prompt)
        for index in range(option_list.option_count)
    )


class _FakePipeWireSource:
    calls: ClassVar[list[tuple[str, bool]]] = []

    def __init__(self, node_name: str, *, capture_sink: bool = False) -> None:
        self._node_name = node_name
        self.calls.append((node_name, capture_sink))

    async def frames(self):
        if self._node_name == "backup-mic":
            raise CaptureError("fixture PipeWire meter unavailable")
        yield b"\xff\x7f"
        await asyncio.Event().wait()
