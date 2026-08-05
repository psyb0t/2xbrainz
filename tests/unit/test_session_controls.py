from __future__ import annotations

import asyncio
import unittest

from two_x_brainz.session_controls import (
    SessionCommand,
    SessionController,
    SessionState,
    parse_session_command,
)


class SessionControllerTests(unittest.TestCase):
    def test_can_start_paused_without_opening_the_capture_gate(self) -> None:
        async def exercise() -> None:
            controller = SessionController(start_paused=True)
            waiter = asyncio.create_task(controller.wait_for_forwarding())
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())
            self.assertEqual(controller.state, SessionState.PAUSED)
            self.assertTrue(controller.resume())
            self.assertTrue(await waiter)

        asyncio.run(exercise())

    def test_new_session_allows_capture_forwarding(self) -> None:
        asyncio.run(self._assert_new_session_allows_forwarding())

    def test_pause_blocks_until_resume(self) -> None:
        asyncio.run(self._assert_pause_blocks_until_resume())

    def test_stop_wakes_a_paused_waiter(self) -> None:
        asyncio.run(self._assert_stop_wakes_paused_waiter())

    def test_repeated_controls_are_idempotent(self) -> None:
        controller = SessionController()

        self.assertTrue(controller.pause())
        self.assertFalse(controller.pause())
        self.assertTrue(controller.resume())
        self.assertFalse(controller.resume())
        self.assertTrue(controller.stop())
        self.assertFalse(controller.stop())
        self.assertFalse(controller.resume())
        self.assertEqual(controller.state, SessionState.STOPPED)

    def test_only_exact_short_control_lines_are_accepted(self) -> None:
        self.assertEqual(parse_session_command(" PAUSE "), SessionCommand.PAUSE)
        self.assertEqual(parse_session_command("resume\n"), SessionCommand.RESUME)
        self.assertIsNone(parse_session_command(""))
        self.assertIsNone(parse_session_command("status"))
        self.assertIsNone(parse_session_command("x" * 33))

    def test_removed_reply_actions_are_not_session_commands(self) -> None:
        for command in ("accept", "dismiss", "edit", "regenerate"):
            self.assertIsNone(parse_session_command(command))

    async def _assert_new_session_allows_forwarding(self) -> None:
        controller = SessionController()

        self.assertTrue(await controller.wait_for_forwarding())
        self.assertEqual(controller.state, SessionState.RUNNING)

    async def _assert_pause_blocks_until_resume(self) -> None:
        controller = SessionController()
        self.assertTrue(controller.pause())
        waiter = asyncio.create_task(controller.wait_for_forwarding())

        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        self.assertTrue(controller.resume())

        self.assertTrue(await asyncio.wait_for(waiter, timeout=1))
        self.assertEqual(controller.state, SessionState.RUNNING)

    async def _assert_stop_wakes_paused_waiter(self) -> None:
        controller = SessionController()
        self.assertTrue(controller.pause())
        waiter = asyncio.create_task(controller.wait_for_forwarding())

        await asyncio.sleep(0)
        self.assertTrue(controller.stop())

        self.assertFalse(await asyncio.wait_for(waiter, timeout=1))
        self.assertEqual(controller.state, SessionState.STOPPED)
