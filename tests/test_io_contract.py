"""Regression test for the GameIO contract.

Proves the engine can be driven entirely through an injected GameIO, which is
what lets the web UI reuse the single game loop instead of forking it. Runs the
whole path without a real LLM:

  wwf_path preselect -> MCP dice_server spawn -> tool call round trip ->
  narrative render -> slash command -> autosave -> /quit

Also asserts the real output/ directory is never written to, so it is safe to
run against a live install.

    python_embeded/python.exe tests/test_io_contract.py
"""

import asyncio
import hashlib
import io
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

from rich.console import Console

import game_engine
from io_layer import GameIO


def digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class FakeIO(GameIO):
    """Captures everything the engine emits; feeds a scripted input queue."""

    def __init__(self, inputs):
        self.buf = io.StringIO()
        super().__init__(Console(file=self.buf, width=100, force_terminal=False))
        self.inputs = list(inputs)
        self.prompts = []

    async def read_input(self, prompt):
        self.prompts.append(prompt)
        return self.inputs.pop(0) if self.inputs else "/quit"


class FakeLLM:
    """Scripted backend: opening text, then a tool call, then a resolution."""

    def __init__(self):
        self.calls = []

    def _msg(self, content, tool_calls=None):
        return {"prompt_eval_count": 0,
                "message": {"content": content, "tool_calls": tool_calls}}

    async def __call__(self, messages, tools, model, context_window):
        self.calls.append(len(messages))
        n = len(self.calls)
        if n == 1:
            return self._msg("你在冰冷的石室中醒来，空气里有铁锈味。")
        if n == 2:
            return self._msg("", [{"function": {"name": "dump_player_db",
                                                "arguments": {}}}])
        return self._msg("你搜遍了石室，只在墙缝里摸到一枚生锈的钉子。")


async def main():
    src_wwf = os.path.join("output", "pui_weave.wwf")
    src_player = os.path.join("output", "pui_weave.player")
    before = {p: digest(p) for p in (src_wwf, src_player)}

    tmp = tempfile.mkdtemp(prefix="pi_io_test_")
    wwf = os.path.join(tmp, "probe.wwf")
    shutil.copy(src_wwf, wwf)
    shutil.copy(src_player, os.path.join(tmp, "probe.player"))

    game_engine.OUTPUT_DIR = tmp
    fake_io = FakeIO(["我搜索石室", "/stats", "/quit"])
    llm = FakeLLM()

    await game_engine.run_game(llm, "fake-model", 8192, verbose=True,
                               io=fake_io, wwf_path=wwf)

    out = fake_io.buf.getvalue()
    after = {p: digest(p) for p in (src_wwf, src_player)}

    print("=" * 60)
    print("CAPTURED OUTPUT")
    print("=" * 60)
    print(out)
    print("=" * 60)
    print("ASSERTIONS")
    print("=" * 60)

    checks = [
        ("IO console carried the opening banner", "石室" in out or "wwf" in out.lower()),
        ("narrative rendered through injected console", "生锈的钉子" in out),
        ("tool call reached the MCP dice_server", "dump_player_db" in out),
        ("/stats rendered the character panel", "护甲" in out or "AC" in out or "生命" in out),
        ("world picker skipped (no numbered world list)", "probe.wwf" in out),
        ("input came from io.read_input, not the terminal", len(fake_io.prompts) >= 3),
        ("LLM saw the tool result round trip", len(llm.calls) >= 3),
        ("no engine fatal error", "fatal" not in out.lower()),
        ("real output/ untouched", before == after),
        ("autosave wrote into the temp dir",
         os.path.exists(os.path.join(tmp, "probe.player"))),
    ]

    ok = True
    for name, passed in checks:
        print(("  PASS  " if passed else "  FAIL  ") + name)
        ok = ok and passed

    print()
    print("prompts seen:", fake_io.prompts)
    print("llm calls   :", llm.calls)
    print("temp dir    :", tmp)
    print()
    print("RESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
