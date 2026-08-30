"""WebIO — a GameIO implementation that streams to a browser.

Console output is captured by handing rich a file-like sink, so every existing
``console.print`` in the engine lands in the browser without touching it. The
three things that deserve better than a wall of text — narrative, dice results,
character stats — additionally arrive as structured events.

Event flow is one-way-push: the engine writes into an asyncio.Queue from
synchronous code (rich calls ``file.write`` synchronously), and the session
forwards queued events to the WebSocket.
"""

import asyncio

from rich.console import Console

from io_layer import GameIO

CONSOLE_WIDTH = 110
_HISTORY_LIMIT = 500


class _EventFile:
    """Minimal file-like object that turns rich writes into events."""

    def __init__(self, push):
        self._push = push

    def write(self, text):
        if text:
            self._push({"type": "out", "text": text})
        return len(text or "")

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        pass

    def isatty(self):
        # False keeps rich from emitting colour escapes and spinner frames.
        return False


class WebIO(GameIO):
    """Drives the engine from a browser tab."""

    def __init__(self):
        self.inbox = asyncio.Queue()
        self.events = asyncio.Queue()
        self._history = []
        self._closed = False
        super().__init__(
            Console(file=_EventFile(self._push), force_terminal=False,
                    width=CONSOLE_WIDTH)
        )

    # ── event plumbing ──────────────────────────────────────────────────────

    def _push(self, event):
        self._history.append(event)
        if len(self._history) > _HISTORY_LIMIT:
            del self._history[: len(self._history) - _HISTORY_LIMIT]
        self.events.put_nowait(event)

    def history(self):
        """Snapshot for replaying into a reconnecting browser tab."""
        return list(self._history)

    async def _emit(self, event):
        self._push(event)

    # ── GameIO contract ─────────────────────────────────────────────────────

    async def read_input(self, prompt):
        """Announce that input is wanted, then block until the browser sends it."""
        await self._emit({"type": "prompt", "text": prompt})
        return await self.inbox.get()

    def submit(self, text):
        """Called by the server when the browser sends a line."""
        if not self._closed:
            self.inbox.put_nowait(text)
            return True
        return False

    # ── structured events (no-ops for the terminal) ─────────────────────────

    async def emit_narrative(self, text, title):
        await self._emit({"type": "narrative", "text": text, "title": title})

    async def emit_tool(self, name, arguments, result):
        await self._emit({
            "type": "tool",
            "name": name,
            "arguments": arguments,
            "result": result,
        })

    async def emit_stats(self, db_data, combat=None):
        await self._emit({"type": "stats", "data": db_data, "combat": combat})

    async def emit_status(self, text):
        await self._emit({"type": "status", "text": text})

    async def close(self):
        self._closed = True
        await self._emit({"type": "closed", "text": "会话已结束"})
