"""Game session lifecycle for the WebUI.

One session = one asyncio task running ``run_game`` = one dice_server
subprocess. The dice server holds the character's SQLite state in memory and
flushes it on autosave, so two sessions on the same save file would silently
overwrite each other. Saves are therefore locked exclusively.
"""

import asyncio
import json
import os
import time

import game_engine
from webui import backends
from webui.webio import WebIO

QUIT_GRACE_SECONDS = 15


class SessionBusy(Exception):
    """Raised when the requested save file already has a live session."""


_sessions = {}
_locks = {}


def _lock_key(wwf_path):
    return os.path.normcase(os.path.abspath(wwf_path))


def get(session_id):
    return _sessions.get(session_id)


def list_worlds():
    """Worlds in output/, newest first, with save progress attached."""
    output_dir = game_engine.OUTPUT_DIR
    if not os.path.isdir(output_dir):
        return []

    worlds = []
    for name in sorted(os.listdir(output_dir)):
        if not name.endswith(".wwf"):
            continue
        path = os.path.join(output_dir, name)
        stem = os.path.splitext(path)[0]
        history = stem + ".history.json"
        rounds = 0
        if os.path.exists(history):
            try:
                with open(history, "r", encoding="utf-8") as f:
                    turns = json.load(f)
                if isinstance(turns, list):
                    rounds = sum(1 for t in turns if t.get("role") == "user")
            except Exception:
                rounds = 0
        worlds.append({
            "file": name,
            "name": os.path.splitext(name)[0],
            "path": path,
            "has_save": os.path.exists(stem + ".player"),
            "rounds": rounds,
            "updated": os.path.getmtime(path),
        })
    worlds.sort(key=lambda w: w["updated"], reverse=True)
    return worlds


class GameSession:
    """Owns one running game and its event stream."""

    def __init__(self, wwf_path, backend_id, options=None, debug=False):
        self.id = os.path.splitext(os.path.basename(wwf_path))[0]
        self.wwf_path = os.path.abspath(wwf_path)
        self.backend_id = backend_id
        self.options = dict(options or {})
        self.debug = debug
        self.io = WebIO()
        self.started = time.time()
        self.error = None
        self._subscribers = []
        self.task = asyncio.create_task(self._run(), name=f"game-{self.id}")
        self._pump = asyncio.create_task(self._fan_out(), name=f"pump-{self.id}")

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def _fan_out(self):
        """Copy engine events to every connected tab.

        An asyncio.Queue is consume-once, so two tabs watching the same session
        would each receive only half the events. Every subscriber gets its own
        queue instead.
        """
        while True:
            try:
                event = await asyncio.wait_for(self.io.events.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # The engine can die without emitting "closed" (cancelled task,
                # hard crash). Wake periodically so this task never outlives it.
                if self.task.done() and self.io.events.empty():
                    return
                continue
            for queue in list(self._subscribers):
                queue.put_nowait(event)
            if event.get("type") == "closed":
                return

    async def _run(self):
        try:
            chat_fn = backends.build_chat_fn(
                self.backend_id, self.options, debug=self.debug)
            model = self.options.get("model") or backends.default_model(self.backend_id)
            context_window = backends.context_window_for(
                self.backend_id, model, self.options.get("base_url"))
            await game_engine.run_game(
                chat_fn, model, context_window,
                verbose=False, debug=self.debug,
                io=self.io, wwf_path=self.wwf_path,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            await self.io._emit({"type": "error", "text": self.error})
        finally:
            await self.io.close()
            _release(self)

    async def stop(self):
        """Ask the engine to quit cleanly so its final autosave runs."""
        if self.task.done():
            return
        self.io.submit("/quit")
        try:
            await asyncio.wait_for(asyncio.shield(self.task), QUIT_GRACE_SECONDS)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self.task.cancel()
        finally:
            _release(self)

    # ── event stream ───────────────────────────────────────────────────────

    def subscribe(self):
        queue = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue):
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    async def stream(self, replay=False):
        """Yield events until the session ends.

        ``replay`` sends the buffered transcript first, which is how a
        reconnecting tab catches up on what it missed.
        """
        queue = self.subscribe()
        try:
            if replay:
                for event in self.io.history():
                    yield event
                    if event.get("type") == "closed":
                        return
            while True:
                event = await queue.get()
                yield event
                if event.get("type") == "closed":
                    return
        finally:
            self.unsubscribe(queue)

    def submit(self, text):
        return self.io.submit(text)

    def info(self):
        return {
            "id": self.id,
            "world": os.path.splitext(os.path.basename(self.wwf_path))[0],
            "backend": self.backend_id,
            "alive": not self.task.done(),
            "error": self.error,
            "busy": not self.io.inbox.empty(),
        }


def _release(session):
    _sessions.pop(session.id, None)
    key = _lock_key(session.wwf_path)
    if _locks.get(key) is session:
        del _locks[key]


async def create(wwf_path, backend_id, options=None, debug=False):
    """Start a session, refusing if the save file is already in use."""
    key = _lock_key(wwf_path)
    existing = _locks.get(key)
    if existing is not None and not existing.task.done():
        raise SessionBusy(os.path.splitext(os.path.basename(wwf_path))[0])

    session = GameSession(wwf_path, backend_id, options, debug)
    _locks[key] = session
    _sessions[session.id] = session
    return session


async def stop_all():
    for session in list(_sessions.values()):
        await session.stop()
