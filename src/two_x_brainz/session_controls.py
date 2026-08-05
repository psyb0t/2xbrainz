"""In-process controls for a single live CLI session."""

from __future__ import annotations

import asyncio
from enum import StrEnum

_MAX_CONTROL_LINE_CHARACTERS = 32


class SessionState(StrEnum):
    """States exposed by the line-oriented live-session control channel."""

    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"


class SessionCommand(StrEnum):
    """Exact commands accepted from the local live-session standard input."""

    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"


class SessionController:
    """Gate capture forwarding without creating a separate control service."""

    def __init__(self) -> None:
        self._state = SessionState.RUNNING
        self._capture_gate = asyncio.Event()
        self._stopped = asyncio.Event()
        self._capture_gate.set()

    @property
    def state(self) -> SessionState:
        """Return the current session state for a fixed-shape CLI record."""
        return self._state

    def pause(self) -> bool:
        """Stop future frames at the local gate; return whether state changed."""
        if self._state is not SessionState.RUNNING:
            return False
        self._state = SessionState.PAUSED
        self._capture_gate.clear()
        return True

    def resume(self) -> bool:
        """Allow frame forwarding again; return whether state changed."""
        if self._state is not SessionState.PAUSED:
            return False
        self._state = SessionState.RUNNING
        self._capture_gate.set()
        return True

    def stop(self) -> bool:
        """Wake all waiters so the enclosing live session can terminate."""
        if self._state is SessionState.STOPPED:
            return False
        self._state = SessionState.STOPPED
        self._capture_gate.set()
        self._stopped.set()
        return True

    async def wait_for_forwarding(self) -> bool:
        """Wait until capture may continue, or return false once stopped."""
        await self._capture_gate.wait()
        return self._state is SessionState.RUNNING

    async def wait_for_stop(self) -> None:
        """Block until an explicit stop wakes the live-session owner."""
        await self._stopped.wait()


def parse_session_command(line: str) -> SessionCommand | None:
    """Accept only short, exact local control commands from standard input."""
    if len(line) > _MAX_CONTROL_LINE_CHARACTERS:
        return None
    normalized = line.strip().lower()
    try:
        return SessionCommand(normalized)
    except ValueError:
        return None
