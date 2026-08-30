import os
import re
import sys
import json
import asyncio
import datetime
from rich.console import Console
from rich.panel import Panel
from rich.padding import Padding
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
# Ensure the project root (where display.py lives) is importable even when this
# module is imported/run via an embedded interpreter from a different working
# directory (e.g. spawned indirectly or launched standalone).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from display import format_stats, render_gm_text, render_image
import i18n
from i18n import tr
import savemgr
from io_layer import TerminalIO

# Resolve data paths against the project root (this file's directory) rather
# than the current working directory, so gameplay works regardless of where the
# embedded interpreter is launched from.
LOCK_FILE = os.path.join(_HERE, "GameMaster_MCP.md")
OUTPUT_DIR = os.path.join(_HERE, "output")
TIMELINE_INTERVAL = 5  # rounds between timeline snapshots

# ── Persistence tuning ───────────────────────────────────────────────────────
# How many past messages to replay into a resumed session. Older ones are
# covered by the timeline summary.
HISTORY_WINDOW = savemgr.HISTORY_WINDOW
# Backups kept per save file (.bak, .bak.1, .bak.2).
MAX_BACKUPS = savemgr.MAX_BACKUPS
# Sentinel distinguishing "no file" from "file contained null / junk".
_MISSING = object()

# ── GM pause / checkpoint protocol tokens ─────────────────────────────────────
# 这些是协议控制 token，必须保持英文原样，禁止翻译（不传入 tr()）。
# 两种拼写等效；仅当回复"独立出现"（去空白后恰好等于 token）才视为暂停信号，
# 内联出现在剧情中的 token 一律剥离后当作正常剧情。
PAUSE_TOKENS = ("{{_NEED_AN_OTHER_PROMPT}}", "{{_NEED_ANOTHER_PROMPT}}")
MAX_RESUMES = 3  # 每个用户回合内恢复循环的安全上限（fix B）


def _is_pure_pause_token(text):
    """仅当 text 去掉首尾空白后「恰好等于」某个 token（独立出现）才算暂停。
    内联出现在剧情中的 token 不算暂停。"""
    if not text:
        return False
    return text.strip() in PAUSE_TOKENS


def _strip_pause_tokens(text):
    """剥离所有 pause token，返回去空白后的文本；token 绝不泄漏到历史/渲染/时间线。"""
    if not text:
        return ""
    out = text
    for tok in PAUSE_TOKENS:
        out = out.replace(tok, "")
    return out.strip()


# ── 思维链（<think>）识别与剥离 ──────────────────────────────────
# 本地模型（koboldcpp 等）常把推理内联写进 content，且在被输出长度截断时
# 不会补上 </think>。必须在渲染/入库前拆开，否则整段推理会被当成剧情输出。
THINK_BLOCK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
THINK_UNCLOSED_RE = re.compile(r"<think>(.*)\Z", re.DOTALL | re.IGNORECASE)


def _split_thinking(text):
    """把 content 拆成 (正文, 思维链)。

    - 闭合块 <think>...</think>：块内归入思维链，块外保留为正文。
    - 未闭合 <think>：从开标签到文本末尾全部视为思维链（模型被输出长度截断的
      典型形态），正文只保留开标签之前的部分。
    """
    if not text:
        return "", ""
    parts = []

    def _take(m):
        parts.append(m.group(1))
        return ""

    narrative = THINK_BLOCK_RE.sub(_take, text)
    m = THINK_UNCLOSED_RE.search(narrative)
    if m:
        parts.append(m.group(1))
        narrative = narrative[:m.start()]
    thinking = "\n\n".join(p.strip() for p in parts if p and p.strip())
    return narrative.strip(), thinking


def _process_response_content(raw_content, adapter_thinking_only=False):
    """归一化一次响应：拆思维链 → 判暂停 → 剥同步标记。

    返回 (content, thinking, is_pause, is_thinking_only)。
    协议判定只作用于剥离思维链后的正文，避免推理文本里提到的
    {{_NEED_AN_OTHER_PROMPT}} 干扰协议（本地模型常在推理中复述协议）。
    """
    narrative, thinking = _split_thinking(raw_content)
    if _is_pure_pause_token(narrative):
        return "", thinking, True, False
    content = _strip_pause_tokens(narrative)
    # 适配器自报的 thinking_only，或引擎剥离后正文为空但确实产出了思维链
    is_thinking_only = bool(adapter_thinking_only) or (bool(thinking) and not content)
    return content, thinking, False, is_thinking_only


TIMELINE_PROMPT = """SYSTEM INSTRUCTION: You have just completed several rounds of gameplay.
Write a session timeline entry in the following EXACT format. Replace bracketed
text with actual content. Keep it concise. Output ONLY the entry — no extra narration.

## Rounds X-Y | [current location] | [in-game time]
**Key Events**:
- [event 1 in one sentence]
- [event 2 in one sentence]
**NPCs**: [names and roles of new NPCs encountered, or "none"]
**Mechanical Changes**:
- Gold: [old]→[new] (reason)
- Items: [gained/used key items]
- Reputation: [changed factions, if any]
**Active Hooks**: [all unresolved plot threads, one per line]"""

# Language directives appended as extra system messages. The English GM
# protocol (GameMaster_MCP.md) is never modified; these only steer narration
# language while explicitly protecting protocol tokens and timeline markers.
ZH_DIRECTIVE = (
    "LANGUAGE DIRECTIVE: Narrate ALL story content, NPC dialogue, and "
    "descriptions in Simplified Chinese (简体中文). The player writes actions "
    "in Chinese. You MUST keep unchanged, in English: all {{...}} protocol "
    "tokens, tool calls and their arguments, and the timeline format markers "
    "(**Key Events**, **NPCs**, **Mechanical Changes**, **Active Hooks**)."
)
EN_DIRECTIVE = (
    "LANGUAGE DIRECTIVE: Narrate ALL story content in English again. "
    "The player may write actions in English."
)
# User-role notes used to propagate a mid-game /lang switch to incremental
# adapters (they cache the system message and would not re-read it).
LANG_SWITCH_NOTE_ZH = (
    "[SYSTEM INSTRUCTION - not a player action] The interface language has been "
    "switched to Chinese. From your next reply onward, narrate ALL story content "
    "in Simplified Chinese (简体中文), keeping all {{...}} protocol tokens and "
    "tool calls unchanged. Do not narrate about this instruction."
)
LANG_SWITCH_NOTE_EN = (
    "[SYSTEM INSTRUCTION - not a player action] The interface language has been "
    "switched to English. Narrate in English again from your next reply onward. "
    "Do not narrate about this instruction."
)

# Command prefixes recognised as system instructions (language-independent).
# Only '/' is implemented today; add '-' here if dash-commands are ever needed.
COMMAND_PREFIXES = ('/',)


def load_timeline(timeline_path):
    """Load existing timeline if available."""
    if os.path.exists(timeline_path):
        with open(timeline_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def append_timeline_file(timeline_path, entry):
    """Append a new timeline entry to the file."""
    # Ensure output dir exists
    os.makedirs(os.path.dirname(timeline_path) or ".", exist_ok=True)
    # Write header on first entry
    if not os.path.exists(timeline_path):
        header = "# Session Timeline\n\n"
    else:
        header = ""
    with open(timeline_path, "a", encoding="utf-8") as f:
        if header:
            f.write(header)
        f.write(entry.strip() + "\n\n")


console = Console()
VERBOSE = False
DEBUG = False
DEBUG_LOG = None
# When True, each round-end auto-save also calls os.fsync() to survive a power
# loss (not just a process kill). Off by default: fsync is a real disk wait and
# per-write flush() already covers the "closed the window" case the user hit.
DEBUG_FSYNC = False


class DebugLogger:
    """Writes a detailed, human-readable trace of a debug session to a file.

    Activated only when --debug is passed. Every write uses UTF-8 (consistent
    with the rest of the project) and is best-effort: a failing write never
    disrupts gameplay.
    """

    def __init__(self, path):
        self.path = path
        self._f = open(path, "w", encoding="utf-8")
        self._write("=== Project Infinity Debug Log ===")
        self._write(f"Started: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self._write(f"File:    {path}")
        self._write("")

    def _write(self, text):
        try:
            self._f.write(text + "\n")
            # Flush on every write so the log is observable in real time and
            # survives an abrupt termination (e.g. killing the window) instead
            # of being stuck in the OS file buffer until close().
            self._f.flush()
        except Exception:
            pass

    def log(self, section, payload):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._write(f"[{ts}] === {section} ===")
        if isinstance(payload, (dict, list)):
            try:
                text = json.dumps(payload, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                text = str(payload)
        else:
            text = str(payload)
        self._write(text)
        self._write("")

    def close(self):
        try:
            self._write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] === SESSION END ===")
            self._f.close()
        except Exception:
            pass

    def save(self):
        """Persist everything logged so far (round-end auto-save).

        Called at the end of every conversation round and after slash commands,
        so a session that ends by closing the window still keeps the latest log
        without relying on the ``/quit`` command. Per-write flush() already
        pushes each line to disk; this adds an optional fsync for power-loss
        protection and a marker line for easy verification.
        """
        try:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._write(f"[{ts}] === AUTO-SAVE (round end) ===")
            if DEBUG_FSYNC:
                os.fsync(self._f.fileno())
        except Exception:
            pass


def dbg(section, payload):
    """Emit a debug log entry if debug logging is active (no-op otherwise)."""
    if DEBUG_LOG is not None:
        DEBUG_LOG.log(section, payload)


def get_wwf_files():
    if not os.path.exists(OUTPUT_DIR):
        return []
    return [f for f in os.listdir(OUTPUT_DIR) if f.endswith(".wwf")]


async def select_wwf(io):
    files = get_wwf_files()
    if not files:
        console.print(f"[bold red]{tr('err.prefix')}[/bold red] {tr('err.no_wwf')}")
        sys.exit(1)

    console.print(Panel(f"[bold magenta]{tr('world.title')}[/bold magenta]", expand=False))
    for i, f in enumerate(files):
        console.print(f"[cyan]{i+1}[/cyan] {f}")

    choice = await io.read_input(tr("world.prompt"))
    try:
        idx = int(choice) - 1
        return os.path.join(OUTPUT_DIR, files[idx])
    except (ValueError, IndexError):
        console.print(f"[red]{tr('world.invalid')}[/red]")
        return os.path.join(OUTPUT_DIR, files[0])


async def run_game(chat_fn, model, context_window, verbose=False, debug=False,
                   image_gen_fn=None, image_frequency=0, io=None, wwf_path=None):
    """
    Run the game loop.

    chat_fn must be an async callable with signature:
        async def chat_fn(messages, tools, model, context_window) -> dict

    The returned dict must have the structure:
        {
            'prompt_eval_count': int,
            'message': {
                'content': str,
                'tool_calls': list[dict] | None
            }
        }

    Where each tool_calls entry is:
        {'function': {'name': str, 'arguments': dict}}

    io       -- a GameIO instance supplying the console and the input source.
                Defaults to TerminalIO, i.e. the unchanged CLI experience.
    wwf_path -- preselect the world file and skip the interactive picker.
                Web callers pass this; CLI callers leave it None.
    """
    global VERBOSE, DEBUG, console
    VERBOSE = verbose
    DEBUG = debug

    # Rebind the module-level console to this run's IO before anything prints,
    # so every nested closure (slash commands, narrative renderer, autosave)
    # writes to the right sink without touching ~60 call sites.
    io = io or TerminalIO()
    console = io.console

    # Load persisted UI language (missing/corrupt settings -> English).
    i18n.load_saved()
    # Surface untranslated keys on stderr in debug mode instead of silently
    # rendering the raw key name to the player.
    i18n.set_warn_missing(DEBUG)

    if VERBOSE:
        console.print(f"[dim]{tr('mode.verbose')}[/dim]")
    if DEBUG:
        console.print(f"[dim]{tr('mode.debug')}[/dim]")

    if wwf_path is None:
        wwf_path = await select_wwf(io)
    console.print(f"\n[green]{tr('world.selected')}[/green] {wwf_path}")

    player_path = os.path.splitext(wwf_path)[0] + ".player"
    timeline_path = os.path.splitext(wwf_path)[0] + ".timeline.md"
    session_path = os.path.splitext(wwf_path)[0] + ".session.json"
    history_path = os.path.splitext(wwf_path)[0] + ".history.json"

    # ── Debug logging setup (--debug) ─────────────────────────────
    # All interaction/output is recorded to a per-session log file in output/.
    global DEBUG_LOG
    DEBUG_LOG = None
    if debug:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = os.path.splitext(os.path.basename(wwf_path))[0]
        log_path = os.path.join(OUTPUT_DIR, f"{stem}_debug_{ts}.log")
        DEBUG_LOG = DebugLogger(log_path)
        dbg("SESSION START", {
            "model": model,
            "context_window": context_window,
            "wwf": wwf_path,
            "player": player_path,
            "timeline": timeline_path,
            "verbose": verbose,
            "debug": debug,
            "log_file": log_path,
        })
        console.print(f"[dim]Debug log: {log_path}[/dim]")

    with open(LOCK_FILE, "r", encoding="utf-8") as f:
        lock_content = f.read()
    with open(wwf_path, "r", encoding="utf-8") as f:
        key_content = f.read()

    # Last known-good snapshot, refreshed on every live autosave. The OUTER
    # finally runs after the MCP subprocess has already exited, so that path can
    # only rewrite what was cached here — it must never talk to the dice server.
    _cache = {"player": None, "session": None, "history": None}

    def _flush_cache():
        """Write the cached snapshot to disk. Sync, and never raises."""
        written = []
        try:
            for key, path in (("player", player_path),
                              ("session", session_path),
                              ("history", history_path)):
                if _cache[key] is None:
                    continue
                if savemgr.atomic_write_json(path, _cache[key], MAX_BACKUPS):
                    written.append(os.path.basename(path))
            if written and VERBOSE:
                console.print(
                    f"[dim]{tr('save.autosave', files=', '.join(written))}[/dim]")
            dbg("AUTO-SAVE (flush)", {"written": written})
        except Exception as e:
            dbg("AUTO-SAVE FLUSH ERROR", str(e))
        return written

    try:
        dice_server_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "dice_server.py"
        )
        async with stdio_client(StdioServerParameters(
            command=sys.executable,
            args=[dice_server_path, player_path],
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                mcp_tools = await session.list_tools()
                tools_schema = []
                for tool in mcp_tools.tools:
                    tools_schema.append({
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.inputSchema
                        }
                    })

                # ── Restore persisted session state ────────────────────
                existing_timeline = load_timeline(timeline_path)
                round_counter = 0
                narrative_counter = 0

                # The combat registry lives in the dice server's memory, so push
                # the last snapshot back in before the first GM turn.
                combatants_restored = 0
                saved_session = savemgr.safe_load_json(session_path, _MISSING)
                if isinstance(saved_session, dict):
                    try:
                        result = await session.call_tool(
                            "import_session_state", {"state": saved_session})
                        text = "\n".join(b.text for b in result.content
                                         if hasattr(b, "text"))
                        payload = json.loads(text) if text else {}
                        combatants_restored = payload.get("restored", 0)
                    except Exception as e:
                        dbg("SESSION RESTORE ERROR", str(e))
                        console.print(f"[yellow]{tr('save.session_fail')}[/yellow]")
                elif saved_session is not _MISSING:
                    console.print(f"[yellow]{tr('save.session_fail')}[/yellow]")

                # Replay the tail of the previous conversation. This must happen
                # before the first chat_fn call: the incremental adapters walk
                # `messages` from index 0 on their first invocation.
                raw_history = savemgr.safe_load_json(history_path, _MISSING)
                if isinstance(raw_history, list):
                    restored_turns = savemgr.fit_history_bytes(
                        savemgr.strip_system(raw_history))
                else:
                    if raw_history is not _MISSING:
                        console.print(f"[yellow]{tr('save.history_fail')}[/yellow]")
                    restored_turns = []
                if restored_turns and VERBOSE:
                    console.print(f"[dim]{tr('save.restored', msgs=len(restored_turns), npcs=combatants_restored)}[/dim]")

                # Single system message = GM protocol + language directive +
                # timeline. Most adapters cache the LAST system message they see
                # (e.g. play_with_gpt.py assigns `system_instruction = content`),
                # so a second system message REPLACES the protocol. The timeline
                # used to be appended as one, silently dropping the GM protocol
                # on every resumed session — always concatenate instead.
                system_content = lock_content
                if i18n.get_lang() == "zh":
                    system_content = lock_content + "\n\n" + ZH_DIRECTIVE
                if existing_timeline:
                    system_content += f"""

SESSION_TIMELINE — these are events that happened earlier this session.
Refer to them when the player asks about past events. Do not replay or re-describe them.

{existing_timeline}"""
                    if VERBOSE:
                        console.print(f"[dim]Timeline loaded: {timeline_path} ({len(existing_timeline)} chars)[/dim]")

                messages = [{"role": "system", "content": system_content}]
                messages.extend(restored_turns)

                current_context_tokens = 0

                async def chat_with_tools(role_content):
                    nonlocal messages, current_context_tokens
                    if isinstance(role_content, str):
                        messages.append({"role": "user", "content": role_content})
                    else:
                        messages.append(role_content)

                    while True:
                        response = await chat_fn(
                            messages=messages,
                            tools=tools_schema,
                            model=model,
                            context_window=context_window,
                        )

                        current_context_tokens = response.get('prompt_eval_count', current_context_tokens)

                        dbg("AI RESPONSE", response)

                        if DEBUG:
                            console.print(f"[dim]DEBUG RESPONSE: {response}[/dim]")
                            if response.get('thinking'):
                                console.print(Panel(
                                    response['thinking'],
                                    title="[bold yellow]DEBUG: Thinking (structured)[/bold yellow]",
                                    border_style="yellow",
                                ))

                        if response.get('malformed_function_call'):
                            dbg("GM OUTPUT (malformed)", tr('gm.malformed'))
                            return tr('gm.malformed')

                        response_msg = response['message']
                        raw_content = response_msg['content'] if response_msg else ""

                        # ── 思维链 + 暂停 / checkpoint token 处理 ──────────
                        # 先拆掉 <think> 思维链（本地模型常内联写入且可能不闭合），
                        # 协议判定只作用于剥离后的正文 —— 否则推理文本里复述的
                        # {{_NEED_AN_OTHER_PROMPT}} 会污染协议判定。
                        content, thinking, is_pause, is_thinking_only = _process_response_content(
                            raw_content, response.get('thinking_only'))

                        if is_pause:
                            if DEBUG:
                                console.print("[bold yellow]DEBUG: Checkpoint token detected. Pausing...[/bold yellow]")
                            dbg("CHECKPOINT", "{{_NEED_AN_OTHER_PROMPT}} detected — pausing for resume token")
                            return "__SYSTEM_PAUSE__"

                        # 只记摘要，不重复正文（正文由下方 GM OUTPUT 统一记录一份）。
                        if thinking or (raw_content and content != raw_content.strip()):
                            dbg("GM OUTPUT (cleaned)",
                                f"thinking={len(thinking)} chars; "
                                f"content {len(raw_content)} -> {len(content)} chars")

                        msg_entry = {
                            "role": "assistant",
                            "content": content,
                        }
                        if response_msg.get('tool_calls'):
                            msg_entry["tool_calls"] = response_msg['tool_calls']
                        # 回填 thinking：DeepSeek 依赖 msg["thinking"] 回显为
                        # reasoning_content；本地模型的思维链也一并保留以便审计。
                        _merged_thinking = "\n\n".join(
                            p for p in (response.get('thinking') or "", thinking)
                            if p and p.strip())
                        if _merged_thinking:
                            msg_entry["thinking"] = _merged_thinking
                        messages.append(msg_entry)

                        thinking_retries = 0
                        MAX_THINKING_RETRIES = 3
                        while is_thinking_only and thinking_retries < MAX_THINKING_RETRIES:
                            thinking_retries += 1
                            if DEBUG:
                                console.print(f"[bold yellow]DEBUG: Thinking-only response. Injecting 'Continue'... ({thinking_retries}/{MAX_THINKING_RETRIES})[/bold yellow]")
                            dbg("RETRY (thinking-only)", f"Injecting 'Continue' ({thinking_retries}/{MAX_THINKING_RETRIES})")
                            messages.append({"role": "user", "content": "Continue"})
                            response = await chat_fn(
                                messages=messages,
                                tools=tools_schema,
                                model=model,
                                context_window=context_window,
                            )
                            current_context_tokens = response.get('prompt_eval_count', current_context_tokens)
                            if DEBUG:
                                console.print(f"[dim]DEBUG RESPONSE: {response}[/dim]")
                                if response.get('thinking'):
                                    console.print(Panel(
                                        response['thinking'],
                                        title="[bold yellow]DEBUG: Thinking (structured)[/bold yellow]",
                                        border_style="yellow",
                                    ))
                            response_msg = response['message']
                            _raw = response_msg['content'] if response_msg else ""
                            content, thinking, is_pause, is_thinking_only = _process_response_content(
                                _raw, response.get('thinking_only'))
                            if is_pause:
                                if DEBUG:
                                    console.print("[bold yellow]DEBUG: Checkpoint token detected (post-thinking). Pausing...[/bold yellow]")
                                dbg("CHECKPOINT", "{{_NEED_AN_OTHER_PROMPT}} detected after thinking — pausing")
                                return "__SYSTEM_PAUSE__"
                            messages.append({
                                "role": "assistant",
                                "content": content,
                                "tool_calls": response_msg.get('tool_calls') or None,
                            } if response_msg.get('tool_calls') else {
                                "role": "assistant",
                                "content": content,
                            })

                        if is_thinking_only and thinking_retries >= MAX_THINKING_RETRIES:
                            dbg("GM OUTPUT (deep thought)", tr('gm.deep_thought'))
                            return tr('gm.deep_thought')

                        tool_calls_list = response_msg.get('tool_calls')
                        if tool_calls_list:
                            for tool_call in tool_calls_list:
                                tool_name = tool_call['function']['name']
                                tool_args = tool_call['function']['arguments']

                                if VERBOSE:
                                    console.print(f"[dim]🔧 Tool: {tool_name}({tool_args})[/dim]")

                                dbg("TOOL CALL →", {"name": tool_name, "arguments": tool_args})

                                result = await session.call_tool(tool_name, arguments=tool_args)

                                tool_result_text = "\n".join(
                                    block.text for block in result.content if hasattr(block, "text")
                                )
                                dbg("TOOL RESULT ←", tool_result_text)

                                # Structured dice/tool event for the WebUI. The
                                # terminal ignores it (GameIO base class no-ops);
                                # it only prints these under --verbose.
                                await io.emit_tool(tool_name, tool_args, tool_result_text)

                                if VERBOSE:
                                    console.print(f"[dim]   → {result.content}[/dim]")

                                messages.append({
                                    "role": "tool",
                                    "content": "\n".join(block.text for block in result.content if hasattr(block, "text")),
                                    "name": tool_name
                                })
                            if DEBUG:
                                console.print("[bold yellow]DEBUG: Tool calls executed alongside sync token. Ignoring token and continuing loop.[/bold yellow]")
                            continue

                        dbg("GM OUTPUT", content)
                        return content

                async def _auto_generate_image(narrative_text):
                    if not narrative_text:
                        return
                    try:
                        result = await session.call_tool("dump_player_db", {})
                        db_text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
                        db_data = json.loads(db_text)
                    except Exception:
                        db_data = {}
                    name = db_data.get("name", "the protagonist")
                    gender = db_data.get("gender", "")
                    race = db_data.get("race", "")
                    cls = db_data.get("character_class", "")
                    level = db_data.get("level", "")
                    hp = db_data.get("hit_points", "unknown")
                    max_hp = db_data.get("max_hp", "unknown")
                    stats = db_data.get("stats", {})
                    bg = db_data.get("background", "")
                    alignment = db_data.get("alignment", "")
                    stat_parts = []
                    for k, v in stats.items():
                        stat_parts.append(f"{k.upper()} {v}")
                    stat_str = ", ".join(stat_parts)
                    char_anchor = f"Character: {name}, a {gender} {race} {cls} (level {level}). {stat_str}. Background: {bg}, Alignment: {alignment}."
                    hp_info = f"{hp}/{max_hp}"
                    
                    image_path = os.path.join(OUTPUT_DIR, "current_scene.png")
                    os.makedirs(OUTPUT_DIR, exist_ok=True)
                    try:
                        with console.status(f"[bold magenta]{tr('img.generating')}[/bold magenta]"):
                            image = await image_gen_fn(narrative_text, char_anchor=char_anchor, hp_info=hp_info)
                        if image:
                            image.save(image_path)
                            if VERBOSE:
                                console.print(f"[dim]✅ Image saved to {image_path}[/dim]")
                            render_image(image_path)
                        else:
                            if VERBOSE:
                                console.print("[dim]⚠️ Image generation returned no image. Continuing without image.[/dim]")
                    except Exception as e:
                        if VERBOSE:
                            console.print(f"[dim]⚠️ Image generation failed: {e}. Continuing without image.[/dim]")

                async def _emit_narrative(text, title):
                    """渲染一段剧情到终端，并按段（fix C）自动生成场景图。
                    title 传入 tr('gm.awakens')（开场）或 tr('gm.title')（常规）。
                    图像节奏由外层 narrative_counter 控制。"""
                    text = _strip_pause_tokens(text)
                    if not text:
                        return
                    console.print(Panel(
                        Padding(render_gm_text(text), (1, 1)),
                        title=f"[bold magenta]{title}[/bold magenta]",
                        border_style="magenta"
                    ))
                    console.print("\n")
                    nonlocal narrative_counter
                    narrative_counter += 1
                    await io.emit_narrative(text, title)
                    if image_gen_fn and image_frequency > 0 and narrative_counter % image_frequency == 0:
                        await _auto_generate_image(text)

                async def handle_slash_command(cmd):
                    cmd = cmd.strip().lower()
                    if cmd == '/help':
                        console.print(Panel(tr('help.body'), title=f"[bold magenta]{tr('help.title')}[/bold magenta]", border_style="magenta", expand=False))
                    elif cmd == '/stats':
                        result = await session.call_tool("dump_player_db", arguments={})
                        if hasattr(result, 'content') and result.content:
                            text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
                            try:
                                db_data = json.loads(text)
                            except (json.JSONDecodeError, TypeError):
                                db_data = text
                            if isinstance(db_data, dict):
                                await io.emit_stats(db_data)
                                for panel in format_stats(db_data):
                                    console.print(panel)
                            else:
                                console.print(Panel(str(db_data), title=f"[bold green]{tr('stats.player_title')}[/bold green]", border_style="green", expand=False))
                        else:
                            console.print(f"[yellow]{tr('stats.fail')}[/yellow]")
                    elif cmd == '/sync':
                        console.print(f"[dim]{tr('sync.start')}[/dim]")
                        await chat_with_tools("{{_SYNC_DATABASE}}")
                        console.print(Panel(f"[green]{tr('sync.done')}[/green]", border_style="green", expand=False))
                    elif cmd == '/save':
                        # Pure snapshot: no buff revert, no state mutation. The
                        # old version rolled active effects back as part of
                        # saving, which made it unsafe to call automatically.
                        # Long-rest settlement is the `rest` MCP tool's job now.
                        result = await session.call_tool("dump_player_db", arguments={})
                        db_data = None
                        if hasattr(result, 'content') and result.content:
                            text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
                            try:
                                db_data = json.loads(text)
                            except (json.JSONDecodeError, TypeError):
                                db_data = None
                        if db_data:
                            if savemgr.atomic_write_json(player_path, db_data, MAX_BACKUPS):
                                _cache["player"] = db_data
                                console.print(Panel(
                                    f"[green]{tr('save.done', path=player_path)}[/green]",
                                    border_style="green", expand=False))
                            else:
                                console.print(f"[red]{tr('save.fail')}[/red]")
                        else:
                            console.print(f"[red]{tr('save.fail')}[/red]")
                    elif cmd == '/quit':
                        return 'quit'
                    elif cmd.startswith('/lang'):
                        parts = cmd.split()
                        if len(parts) == 1:
                            console.print(Panel(
                                f"{tr('lang.current', lang=i18n.get_lang())}\n"
                                f"[dim]{tr('lang.usage')}[/dim]",
                                border_style="magenta", expand=False))
                        elif parts[1] in i18n._SUPPORTED:
                            i18n.set_lang(parts[1])
                            directive = ZH_DIRECTIVE if parts[1] == 'zh' else EN_DIRECTIVE
                            # 1) Rewrite the single system message in place (works for
                            #    full-history adapters and persists across restarts).
                            base = messages[0]["content"]
                            for d in (ZH_DIRECTIVE, EN_DIRECTIVE):
                                base = base.replace("\n\n" + d, "")
                            messages[0]["content"] = base + "\n\n" + directive
                            # 2) Incremental adapters cache the system message, so
                            #    propagate the switch via a user-role note instead.
                            messages.append({"role": "user", "content":
                                             LANG_SWITCH_NOTE_ZH if parts[1] == 'zh' else LANG_SWITCH_NOTE_EN})
                            console.print(Panel(
                                tr('lang.switched', name=tr(f'lang.name.{parts[1]}')),
                                border_style="green", expand=False))
                        else:
                            console.print(f"[yellow]{tr('lang.invalid', lang=parts[1])}[/yellow]")
                    else:
                        console.print(f"[yellow]{tr('cmd.unknown', cmd=cmd)}[/yellow]")
                        console.print(f"[dim]{tr('cmd.hint_help')}[/dim]")
                    return None

                async def _autosave(live=True):
                    """Refresh the snapshot from the dice server, then flush it.

                    live=False skips the refresh and replays the cache, for the
                    shutdown path where MCP is already closed. A failed autosave
                    must never take the game down, so every step is guarded.
                    """
                    try:
                        if live:
                            try:
                                result = await session.call_tool("dump_player_db", {})
                                text = "\n".join(b.text for b in result.content
                                                 if hasattr(b, "text"))
                                _cache["player"] = json.loads(text) if text else None
                            except Exception:
                                pass
                            try:
                                result = await session.call_tool("export_session_state", {})
                                text = "\n".join(b.text for b in result.content
                                                 if hasattr(b, "text"))
                                payload = json.loads(text) if text else {}
                            except Exception:
                                payload = {}
                            if payload:
                                payload["round_counter"] = round_counter
                                payload["narrative_counter"] = narrative_counter
                                payload["context_tokens"] = current_context_tokens
                                payload["context_window"] = context_window
                                _cache["session"] = payload
                            _cache["history"] = savemgr.strip_system(messages)
                            # Push the refreshed sheet to the browser every
                            # round. Guarded for the same reason as the rest of
                            # this function: a UI update must never break a save.
                            try:
                                await io.emit_stats(_cache.get("player"), payload)
                            except Exception:
                                pass
                        _flush_cache()
                    except Exception as e:
                        dbg("AUTO-SAVE ERROR", str(e))
                        if VERBOSE:
                            console.print(f"[dim]{tr('save.autosave_fail', e=e)}[/dim]")

                console.print(f"\n[yellow]{tr('inject.world')}[/yellow]")
                if VERBOSE:
                    response_text = await chat_with_tools(key_content)
                else:
                    with console.status(f"[bold blue]{tr('gm.thinking')}[/bold blue]"):
                        response_text = await chat_with_tools(key_content)

                # ── 恢复循环 + 安全上限（fix B）──
                resume_count = 0
                while response_text == "__SYSTEM_PAUSE__":
                    resume_count += 1
                    if resume_count > MAX_RESUMES:
                        # 达到上限：纯暂停本身不带剧情，兜底发一条系统提示，保证界面不空白。
                        await _emit_narrative(tr('gm.resume_fallback'), title=tr('gm.awakens'))
                        response_text = None
                        break
                    if DEBUG:
                        console.print("[bold cyan]DEBUG: Injecting Resume Token ({{_CONTINUE_EXECUTION}})[/bold cyan]")
                    dbg("RESUME", "Injected {{_CONTINUE_EXECUTION}}")
                    if VERBOSE:
                        response_text = await chat_with_tools("{{_CONTINUE_EXECUTION}}")
                    else:
                        with console.status(f"[bold blue]{tr('gm.thinking')}[/bold blue]"):
                            response_text = await chat_with_tools("{{_CONTINUE_EXECUTION}}")

                # ── 渲染该回合剧情段（fix C，已含按段图像）──
                if response_text and response_text != "__SYSTEM_PAUSE__":
                    clean = _strip_pause_tokens(response_text)
                    if clean:
                        await _emit_narrative(clean, title=tr('gm.awakens'))

                console.print(f"\n[bold cyan]{tr('game.started')}[/bold cyan]\n")

                try:
                    while True:
                        if VERBOSE or DEBUG:
                            console.print(f"[dim]Context: {current_context_tokens:,} / {context_window:,} tokens[/dim]")
                        user_input = await io.read_input(tr("prompt.action"))
                        user_input = user_input.strip()

                        if not user_input:
                            continue

                        dbg("USER INPUT", user_input)

                        if user_input.startswith('/'):
                            dbg("COMMAND", user_input)
                            result = await handle_slash_command(user_input)
                            if DEBUG_LOG is not None:
                                DEBUG_LOG.save()
                            if result == 'quit':
                                # The try/finally below performs the live save.
                                console.print(f"[yellow]{tr('quit.goodbye')}[/yellow]")
                                break
                            await _autosave(live=True)
                            continue

                        try:
                            if VERBOSE:
                                gm_response = await chat_with_tools(user_input)
                            else:
                                with console.status(f"[bold blue]{tr('gm.thinking')}[/bold blue]"):
                                    gm_response = await chat_with_tools(user_input)
                        except KeyboardInterrupt:
                            console.print(f"\n[yellow]{tr('game.interrupted')}[/yellow]")
                            continue
                        except Exception as e:
                            dbg("GM ERROR", str(e))
                            console.print(f"[bold red]{tr('gm.error', e=e)}[/bold red]")
                            continue

                        # ── 恢复循环 + 安全上限（fix B）──
                        resume_count = 0
                        while gm_response == "__SYSTEM_PAUSE__":
                            resume_count += 1
                            if resume_count > MAX_RESUMES:
                                await _emit_narrative(tr('gm.resume_fallback'), title=tr('gm.title'))
                                gm_response = None
                                break
                            if DEBUG:
                                console.print("[bold cyan]DEBUG: Injecting Resume Token ({{_CONTINUE_EXECUTION}})[/bold cyan]")
                            dbg("RESUME", "Injected {{_CONTINUE_EXECUTION}}")
                            if VERBOSE:
                                gm_response = await chat_with_tools("{{_CONTINUE_EXECUTION}}")
                            else:
                                with console.status(f"[bold blue]{tr('gm.thinking')}[/bold blue]"):
                                    gm_response = await chat_with_tools("{{_CONTINUE_EXECUTION}}")

                        # ── 渲染该回合剧情段（fix C，已含按段图像）──
                        if gm_response and gm_response != "__SYSTEM_PAUSE__":
                            clean_response = _strip_pause_tokens(gm_response)
                            if clean_response:
                                await _emit_narrative(clean_response, title=tr('gm.title'))

                            # ── Timeline checkpoint ────────────────────────
                            round_counter += 1
                            if round_counter % TIMELINE_INTERVAL == 0:
                                if VERBOSE:
                                    console.print(f"\n[dim]⏳ Timeline checkpoint (round {round_counter})...[/dim]")
                                try:
                                    # Calculate round range for this entry
                                    start_round = round_counter - TIMELINE_INTERVAL + 1
                                    prompt = TIMELINE_PROMPT.replace("X-Y", f"{start_round}-{round_counter}")
                                    tl_response = await chat_with_tools(prompt)
                                    if tl_response and tl_response != "__SYSTEM_PAUSE__":
                                        # Clean up sync tokens from timeline entry
                                        entry = _strip_pause_tokens(tl_response)
                                        if entry and "**Key Events**" in entry:
                                            append_timeline_file(timeline_path, entry)
                                            dbg("TIMELINE ENTRY", entry)
                                            if VERBOSE:
                                                console.print(f"[dim]✅ Timeline saved ({len(entry)} chars)[/dim]")
                                        elif VERBOSE:
                                            console.print(f"[dim]⚠️ Timeline entry missing Key Events — skipped[/dim]")
                                except Exception as e:
                                    dbg("TIMELINE ERROR", str(e))
                                    if VERBOSE:
                                        console.print(f"[dim]⚠️ Timeline checkpoint failed: {e}[/dim]")

                        # Round-end auto-save: flush the debug log, then
                        # snapshot the game so that closing the window - not
                        # just /quit - keeps the latest round.
                        if DEBUG_LOG is not None:
                            DEBUG_LOG.save()
                        await _autosave(live=True)
                finally:
                    await _autosave(live=True)

    except KeyboardInterrupt:
        console.print(f"\n[yellow]{tr('game.bye')}[/yellow]")
    except Exception as e:
        import traceback
        traceback.print_exc()
        console.print(f"\n[bold red]{tr('game.fatal', e=e)}[/bold red]")
        console.print(f"[dim]{tr('game.ended')}[/dim]")
    finally:
        # The MCP subprocess is gone by now, so this can only replay the last
        # cached snapshot — still worth doing, because a crash mid-turn would
        # otherwise lose everything since the previous round-end save.
        try:
            _flush_cache()
        except Exception:
            pass
        if DEBUG_LOG is not None:
            dbg("SESSION END", {"reason": "see log above"})
            DEBUG_LOG.close()