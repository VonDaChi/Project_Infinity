import os
import sys
import json
import asyncio
import datetime
from rich.console import Console
from rich.panel import Panel
from rich.padding import Padding
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
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

# Resolve data paths against the project root (this file's directory) rather
# than the current working directory, so gameplay works regardless of where the
# embedded interpreter is launched from.
LOCK_FILE = os.path.join(_HERE, "GameMaster_MCP.md")
OUTPUT_DIR = os.path.join(_HERE, "output")
TIMELINE_INTERVAL = 5  # rounds between timeline snapshots

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


async def select_wwf(input_session):
    files = get_wwf_files()
    if not files:
        console.print(f"[bold red]{tr('err.prefix')}[/bold red] {tr('err.no_wwf')}")
        sys.exit(1)

    console.print(Panel(f"[bold magenta]{tr('world.title')}[/bold magenta]", expand=False))
    for i, f in enumerate(files):
        console.print(f"[cyan]{i+1}[/cyan] {f}")

    choice = await input_session.prompt_async(HTML(f'<ansicyan><b>{tr("world.prompt")}</b></ansicyan> '))
    try:
        idx = int(choice) - 1
        return os.path.join(OUTPUT_DIR, files[idx])
    except (ValueError, IndexError):
        console.print(f"[red]{tr('world.invalid')}[/red]")
        return os.path.join(OUTPUT_DIR, files[0])


async def run_game(chat_fn, model, context_window, verbose=False, debug=False,
                   image_gen_fn=None, image_frequency=0):
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
    """
    global VERBOSE, DEBUG
    VERBOSE = verbose
    DEBUG = debug

    # Load persisted UI language (missing/corrupt settings -> English).
    i18n.load_saved()

    if VERBOSE:
        console.print(f"[dim]{tr('mode.verbose')}[/dim]")
    if DEBUG:
        console.print(f"[dim]{tr('mode.debug')}[/dim]")

    input_session = PromptSession()

    wwf_path = await select_wwf(input_session)
    console.print(f"\n[green]{tr('world.selected')}[/green] {wwf_path}")

    player_path = os.path.splitext(wwf_path)[0] + ".player"
    timeline_path = os.path.splitext(wwf_path)[0] + ".timeline.md"

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

                # ── Load session timeline ─────────────────────────────
                existing_timeline = load_timeline(timeline_path)
                round_counter = 0
                narrative_counter = 0

                # Single system message = GM protocol + optional language directive.
                # Most adapters cache the LAST system message they see, so a second
                # system message would REPLACE the protocol — always concatenate.
                system_content = lock_content
                if i18n.get_lang() == "zh":
                    system_content = lock_content + "\n\n" + ZH_DIRECTIVE
                messages = [
                    {"role": "system", "content": system_content}
                ]
                if existing_timeline:
                    messages.append({
                        "role": "system",
                        "content": f"""SESSION_TIMELINE — these are events that happened earlier this session.
Refer to them when the player asks about past events. Do not replay or re-describe them.

{existing_timeline}"""
                    })
                    if VERBOSE:
                        console.print(f"[dim]Timeline loaded: {timeline_path} ({len(existing_timeline)} chars)[/dim]")

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
                        content = response_msg['content'] if response_msg else ""

                        msg_entry = {
                            "role": "assistant",
                            "content": content or "",
                        }
                        if response_msg.get('tool_calls'):
                            msg_entry["tool_calls"] = response_msg['tool_calls']
                        if response.get('thinking'):
                            msg_entry["thinking"] = response['thinking']
                        messages.append(msg_entry)

                        thinking_retries = 0
                        MAX_THINKING_RETRIES = 3
                        while response.get('thinking_only') and thinking_retries < MAX_THINKING_RETRIES:
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
                            content = response_msg['content'] if response_msg else ""
                            messages.append({
                                "role": "assistant",
                                "content": content or "",
                                "tool_calls": response_msg.get('tool_calls') or None,
                            } if response_msg.get('tool_calls') else {
                                "role": "assistant",
                                "content": content or "",
                            })

                        if response.get('thinking_only') and thinking_retries >= MAX_THINKING_RETRIES:
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

                        if any(token in (content or "") for token in ["{{_NEED_AN_OTHER_PROMPT}}", "{{_NEED_ANOTHER_PROMPT}}"]):
                            if DEBUG:
                                console.print("[bold yellow]DEBUG: Checkpoint token detected. Pausing...[/bold yellow]")
                            dbg("CHECKPOINT", "{{_NEED_AN_OTHER_PROMPT}} detected — pausing for resume token")
                            return "__SYSTEM_PAUSE__"

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
                        result = await session.call_tool("dump_player_db", arguments={})
                        if hasattr(result, 'content') and result.content:
                            text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
                            try:
                                db_data = json.loads(text)
                            except (json.JSONDecodeError, TypeError):
                                db_data = {}
                            buff_data = db_data.get("_active_buff_data", {})
                            if isinstance(buff_data, str):
                                try:
                                    buff_data = json.loads(buff_data)
                                except (json.JSONDecodeError, TypeError):
                                    buff_data = {}
                            cleared = []
                            for spell_name, entries in buff_data.items():
                                for entry in entries:
                                    field = entry["field"]
                                    delta = entry["delta"]
                                    if field == "temporary_hit_points":
                                        db_data[field] = 0
                                    else:
                                        current_val = db_data.get(field, 0)
                                        if isinstance(current_val, str):
                                            try:
                                                current_val = int(current_val)
                                            except (ValueError, TypeError):
                                                continue
                                        db_data[field] = current_val - delta
                                cleared.append(spell_name)
                            db_data["active_effects"] = []
                            db_data["_active_buff_data"] = {}
                            with open(player_path, "w", encoding="utf-8") as f:
                                json.dump(db_data, f, indent=2)
                            msg = f"[green]{tr('save.done', path=player_path)}[/green]"
                            if cleared:
                                msg += f"\n[dim]{tr('save.reverted', names=', '.join(cleared))}[/dim]"
                            console.print(Panel(msg, border_style="green", expand=False))
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

                console.print(f"\n[yellow]{tr('inject.world')}[/yellow]")
                if VERBOSE:
                    response_text = await chat_with_tools(key_content)
                else:
                    with console.status(f"[bold blue]{tr('gm.thinking')}[/bold blue]"):
                        response_text = await chat_with_tools(key_content)

                while response_text == "__SYSTEM_PAUSE__":
                    if DEBUG:
                        console.print("[bold cyan]DEBUG: Injecting Resume Token ({{_CONTINUE_EXECUTION}})[/bold cyan]")
                    dbg("RESUME", "Injected {{_CONTINUE_EXECUTION}}")
                    if VERBOSE:
                        response_text = await chat_with_tools("{{_CONTINUE_EXECUTION}}")
                    else:
                        with console.status(f"[bold blue]{tr('gm.thinking')}[/bold blue]"):
                            response_text = await chat_with_tools("{{_CONTINUE_EXECUTION}}")

                if image_gen_fn and image_frequency > 0 and response_text and response_text != "__SYSTEM_PAUSE__":
                    clean = response_text.replace("{{_NEED_AN_OTHER_PROMPT}}", "").replace("{{_NEED_ANOTHER_PROMPT}}", "").strip()
                    if clean:
                        narrative_counter += 1
                        if narrative_counter % image_frequency == 0:
                            await _auto_generate_image(clean)

                console.print(Panel(
                    Padding(render_gm_text(response_text), (1, 1)),
                    title=f"[bold magenta]{tr('gm.awakens')}[/bold magenta]",
                    border_style="magenta"
                ))

                console.print(f"\n[bold cyan]{tr('game.started')}[/bold cyan]\n")

                while True:
                    if VERBOSE or DEBUG:
                        console.print(f"[dim]Context: {current_context_tokens:,} / {context_window:,} tokens[/dim]")
                    user_input = await input_session.prompt_async(HTML(f'<ansicyan><b>{tr("prompt.action")}</b></ansicyan> '))
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
                            console.print(f"[yellow]{tr('quit.goodbye')}[/yellow]")
                            break
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

                    while gm_response == "__SYSTEM_PAUSE__":
                        if DEBUG:
                            console.print("[bold cyan]DEBUG: Injecting Resume Token ({{_CONTINUE_EXECUTION}})[/bold cyan]")
                        dbg("RESUME", "Injected {{_CONTINUE_EXECUTION}}")
                        if VERBOSE:
                            gm_response = await chat_with_tools("{{_CONTINUE_EXECUTION}}")
                        else:
                            with console.status(f"[bold blue]{tr('gm.thinking')}[/bold blue]"):
                                gm_response = await chat_with_tools("{{_CONTINUE_EXECUTION}}")

                    if gm_response and gm_response != "__SYSTEM_PAUSE__":
                        clean_response = gm_response.replace("{{_NEED_AN_OTHER_PROMPT}}", "").replace("{{_NEED_ANOTHER_PROMPT}}", "").strip()

                        if image_gen_fn and image_frequency > 0 and clean_response:
                            narrative_counter += 1
                            if narrative_counter % image_frequency == 0:
                                await _auto_generate_image(clean_response)

                        if clean_response:
                            console.print(Panel(
                                Padding(render_gm_text(clean_response), (1, 1)),
                                title=f"[bold magenta]{tr('gm.title')}[/bold magenta]",
                                border_style="magenta"
                            ))
                            console.print("\n")

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
                                    entry = tl_response.replace("{{_NEED_AN_OTHER_PROMPT}}", "").replace("{{_NEED_ANOTHER_PROMPT}}", "").strip()
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

                    # Round-end auto-save (debug mode): persist the log now so
                    # that closing the window - not just /quit - keeps the
                    # latest round. See DebugLogger.save().
                    if DEBUG_LOG is not None:
                        DEBUG_LOG.save()

    except KeyboardInterrupt:
        console.print(f"\n[yellow]{tr('game.bye')}[/yellow]")
    except Exception as e:
        import traceback
        traceback.print_exc()
        console.print(f"\n[bold red]{tr('game.fatal', e=e)}[/bold red]")
        console.print(f"[dim]{tr('game.ended')}[/dim]")
    finally:
        if DEBUG_LOG is not None:
            dbg("SESSION END", {"reason": "see log above"})
            DEBUG_LOG.close()