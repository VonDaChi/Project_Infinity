"""IO abstraction separating the game engine from its terminal.

The engine only ever talks to a ``GameIO`` instance. ``TerminalIO`` reproduces
the original prompt_toolkit + rich behaviour; a future ``WebIO`` will push the
same content over a WebSocket. Keeping the seam here means the 900-line game
loop stays single-sourced instead of forking into a CLI copy and a web copy
that drift apart.

Why a global ``console`` swap instead of a per-call interface: the engine has
~60 ``console.print`` / ``console.status`` call sites spread across nested
closures. Rebinding the module-level ``console`` at ``run_game`` entry reaches
all of them with no logic changes and no regression risk.
"""

import os

from rich.console import Console


class GameIO:
    """Everything the engine needs from the outside world.

    Subclasses provide a ``console`` (any rich-compatible object) and an async
    ``read_input``. ``console.status`` must work as a context manager — rich
    degrades it to a single static line when the output is not a terminal,
    which is exactly the behaviour wanted for non-TTY consumers.
    """

    def __init__(self, console=None):
        self.console = console or Console()

    async def read_input(self, prompt):
        """Await one line of user input. ``prompt`` is a plain string."""
        raise NotImplementedError

    async def close(self):
        """Release any resources held by this IO (sessions, sockets)."""
        pass

    # ── Structured events ───────────────────────────────────────────────────
    # Web consumers render these as widgets instead of text. The terminal has
    # already printed the equivalent through the console, so the base class
    # ignores them — the engine can call these unconditionally.

    async def emit_narrative(self, text, title):
        pass

    async def emit_tool(self, name, arguments, result):
        pass

    async def emit_stats(self, db_data, combat=None):
        pass

    async def emit_status(self, text):
        pass


class TerminalIO(GameIO):
    """Prompt-toolkit prompt + rich console. Byte-identical to the old CLI.

    prompt_toolkit is imported lazily so that a web deployment never pays for
    it, and so importing this module cannot fail on a headless install.
    """

    def __init__(self, console=None, session=None):
        super().__init__(console or Console())
        self._session = session

    @property
    def session(self):
        """Built on first use, not at construction time.

        prompt_toolkit raises NoConsoleScreenBufferError while constructing a
        PromptSession if there is no console attached. Deferring means a
        headless process (the future web server) can import and instantiate
        this class safely; only actually prompting requires a terminal.
        """
        if self._session is None:
            from prompt_toolkit import PromptSession

            self._session = PromptSession()
        return self._session

    async def read_input(self, prompt):
        from prompt_toolkit.formatted_text import HTML

        markup = f'<ansicyan><b>{prompt}</b></ansicyan> '
        return await self.session.prompt_async(HTML(markup))


def default_io():
    """Build the IO used when a caller does not pass one explicitly.

    Kept as a factory so the 7 play_*.py entry points need no changes at all.
    """
    return TerminalIO()
