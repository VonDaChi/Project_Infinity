#!/usr/bin/env python3
"""Project Infinity x KoboldCpp — local OpenAI-compatible API adapter.

Connects to a KoboldCpp instance (running locally or anywhere on the LAN) that
exposes the OpenAI-compatible Chat Completions API, so the Game Master can be
driven by a self-hosted / local model with no cloud API key.

KoboldCpp serves the OpenAI-compatible API under /v1 by default:
    Local:  http://localhost:5001/v1
    LAN:    http://<LAN_IP>:5001/v1

Usage:
    # Local default endpoint (http://localhost:5001/v1), model label "koboldcpp"
    python play_with_kobold.py

    # KoboldCpp on another machine in the LAN
    python play_with_kobold.py --base-url http://192.168.1.50:5001/v1

    # Custom loaded-model label / sampling temperature
    python play_with_kobold.py --model my-llama --temperature 0.7

Environment variables (all optional):
    KOBOLD_BASE_URL   default http://localhost:5001/v1
    KOBOLD_MODEL      default koboldcpp
    KOBOLD_API_KEY    default not-needed (KoboldCpp ignores it)

Note: the loaded model must support OpenAI-style function/tool calling for the
dice engine, combat resolution, and state tools to work. Models that only do
plain chat will not emit the tool_calls this game depends on.
"""

import os
import sys
import json
import argparse
import asyncio
from collections import deque
from rich.panel import Panel
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from openai import AsyncOpenAI, APIStatusError
# Ensure the project root (where game_engine.py lives) is importable even when
# launched via an embedded interpreter from a different working directory.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import i18n
from i18n import tr
from game_engine import run_game, console

# ── KoboldCpp configuration ──────────────────────────────────────
# KoboldCpp serves an OpenAI-compatible Chat Completions API under /v1.
# Point --base-url (or KOBOLD_BASE_URL) at a LAN address to play over the network.
DEFAULT_BASE_URL = "http://localhost:5001/v1"
DEFAULT_MODEL = "koboldcpp"
DEFAULT_API_KEY = "not-needed"
DEFAULT_TEMP = 0.0
DEFAULT_MAX_OUTPUT_TOKENS = 8192
# Local models rarely tolerate the million-token windows of cloud models; keep
# this realistic so the in-game token counter is meaningful. Override as needed.
# When --context-window is not given, we query the KoboldCpp backend for its
# actual max context length; FALLBACK_CONTEXT_WINDOW is used if that query fails.
FALLBACK_CONTEXT_WINDOW = 8192


def parse_args():
    parser = argparse.ArgumentParser(
        description="Project Infinity: D&D RPG powered by a local KoboldCpp API"
    )
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show detailed MCP tool calls and responses")
    parser.add_argument("--debug", "-d", action="store_true",
                        help="Show raw LLM responses and tool calls")
    parser.add_argument("--temperature", "-t", type=float, default=DEFAULT_TEMP,
                        help=f"Sampling temperature (default: {DEFAULT_TEMP})")
    parser.add_argument("--base-url", default=os.environ.get("KOBOLD_BASE_URL", DEFAULT_BASE_URL),
                        help=f"KoboldCpp OpenAI-compatible base URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--model", default=os.environ.get("KOBOLD_MODEL", DEFAULT_MODEL),
                        help=f"Model label sent to KoboldCpp (default: {DEFAULT_MODEL})")
    parser.add_argument("--api-key", default=os.environ.get("KOBOLD_API_KEY", DEFAULT_API_KEY),
                        help="API key (KoboldCpp ignores it; default: not-needed)")
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS,
                        help=f"Max output tokens per response (default: {DEFAULT_MAX_OUTPUT_TOKENS})")
    parser.add_argument("--context-window", type=int, default=None,
                        help="Context window in tokens for the in-game counter. "
                             "If omitted, auto-detected from the KoboldCpp backend's max context "
                             "length; pass explicitly to override.")
    return parser.parse_args()


async def fetch_kobold_context_length(base_url, timeout=5.0):
    """Query the KoboldCpp backend for the context-length upper limit it exposes.

    Tries the OpenAI-compatible-facing config endpoint first, then falls back to
    the launcher's true loaded value. Returns an int on success, else None.
    """
    import httpx
    native = base_url.rstrip("/")
    if native.endswith("/v1"):
        native = native[:-3]

    async def _get_json(url):
        try:
            async with httpx.AsyncClient(timeout=timeout) as hc:
                r = await hc.get(url)
                if r.status_code == 200:
                    try:
                        return r.json()
                    except Exception:
                        return None
        except Exception:
            return None
        return None

    # Primary: the max context length the OpenAI-compatible API surface reports.
    data = await _get_json(native + "/api/v1/config/max_context_length")
    if isinstance(data, dict):
        val = data.get("result", data.get("value", data.get("max_context_length")))
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    elif isinstance(data, int):
        return data

    # Fallback: the actual context length loaded from the launcher.
    data = await _get_json(native + "/api/extra/true_max_context_length")
    if isinstance(data, dict):
        val = data.get("result", data.get("value"))
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    return None


def create_kobold_chat_fn(base_url, api_key, model, context_window,
                          max_output_tokens, temperature=DEFAULT_TEMP, debug=False):
    """Create a KoboldCpp chat function compatible with the Project Infinity engine."""
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
    )
    openai_messages = []
    system_instruction = None
    last_processed = 0
    tool_call_id_counter = 0

    def _next_tool_call_id():
        nonlocal tool_call_id_counter
        tool_call_id_counter += 1
        return f"tc_{tool_call_id_counter}"

    async def chat_fn(messages, tools, model, context_window):
        nonlocal openai_messages, system_instruction, last_processed

        pending_tool_call_ids = deque()

        # ── Build OpenAI-format messages incrementally ─────────
        for msg in messages[last_processed:]:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                system_instruction = content
                continue

            if role == "user":
                openai_messages.append({"role": "user", "content": content})

            elif role == "assistant":
                tool_calls_raw = msg.get("tool_calls")
                if tool_calls_raw:
                    oai_tool_calls = []
                    for tc in tool_calls_raw:
                        fn = tc.get("function", {})
                        tc_id = _next_tool_call_id()
                        fn_args = fn.get("arguments", {})
                        if isinstance(fn_args, dict):
                            fn_args = json.dumps(fn_args)
                        oai_tool_calls.append({
                            "id": tc_id,
                            "type": "function",
                            "function": {"name": fn.get("name", ""), "arguments": fn_args},
                        })
                        pending_tool_call_ids.append(tc_id)
                    oai_msg = {
                        "role": "assistant",
                        "content": content or None,
                        "tool_calls": oai_tool_calls,
                    }
                    openai_messages.append(oai_msg)
                else:
                    openai_messages.append({"role": "assistant", "content": content or ""})

            elif role == "tool":
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": (
                        pending_tool_call_ids.popleft() if pending_tool_call_ids
                        else _next_tool_call_id()
                    ),
                    "content": msg.get("content", ""),
                }
                openai_messages.append(tool_msg)

        last_processed = len(messages)

        # ── Build tools ───────────────────────────────────────
        openai_tools = []
        if tools:
            for tool in tools:
                fn = tool.get("function", {})
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {}),
                    }
                })

        # ── Chat Completions API call (OpenAI-compatible) ──────
        max_tokens = min(context_window, max_output_tokens)
        kwargs = {
            "model": model,
            "messages": openai_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if openai_tools:
            kwargs["tools"] = openai_tools
            kwargs["tool_choice"] = "auto"

        # Inject system prompt
        if system_instruction:
            has_system = any(m.get("role") == "system" for m in openai_messages)
            if not has_system:
                openai_messages.insert(0, {"role": "system", "content": system_instruction})
            kwargs["messages"] = openai_messages

        max_retries = 3
        max_empty_retries = 3
        empty_retries = 0

        for attempt in range(max_retries + max_empty_retries):
            try:
                response = await client.chat.completions.create(**kwargs)

                prompt_tokens = 0
                if response.usage:
                    prompt_tokens = response.usage.prompt_tokens or 0

                choice = response.choices[0] if response.choices else None
                if not choice:
                    return {
                        "prompt_eval_count": prompt_tokens,
                        "message": {"content": "[No response generated.]", "tool_calls": None},
                    }

                msg = choice.message
                content = msg.content or ""

                tool_calls = None
                if msg.tool_calls:
                    tool_calls = []
                    for tc in msg.tool_calls:
                        args = {}
                        if tc.function.arguments:
                            try:
                                args = json.loads(tc.function.arguments)
                            except (json.JSONDecodeError, TypeError):
                                args = {}
                        tool_calls.append({
                            "function": {
                                "name": tc.function.name,
                                "arguments": args,
                            }
                        })

                # Auto-retry on empty response (local models can stall mid-generation)
                if not content.strip() and tool_calls is None:
                    empty_retries += 1
                    if empty_retries <= max_empty_retries:
                        if debug:
                            console.print(
                                f"[bold yellow]DEBUG: Empty response. "
                                f"Retrying... ({empty_retries}/{max_empty_retries})[/bold yellow]"
                            )
                        last_user_msg = None
                        for m in reversed(messages):
                            if m.get("role") == "user":
                                last_user_msg = m.get("content", "")
                                break
                        if last_user_msg:
                            openai_messages.append({"role": "user", "content": last_user_msg})
                            kwargs["messages"] = openai_messages
                        await asyncio.sleep(1)
                        continue
                    if debug:
                        console.print("[bold red]DEBUG: Empty response persists.[/bold red]")

                return {
                    "prompt_eval_count": prompt_tokens,
                    "message": {
                        "content": content,
                        "tool_calls": tool_calls,
                    },
                }

            except APIStatusError as e:
                if e.status_code in (429, 500, 502, 503) and attempt < max_retries - 1:
                    if debug:
                        console.print(
                            f"[bold yellow]DEBUG: API error {e.status_code}. "
                            f"Retrying... ({attempt+1}/{max_retries})[/bold yellow]"
                        )
                    await asyncio.sleep(2)
                    continue
                raise e
            except Exception as e:
                if debug:
                    import traceback
                    traceback.print_exc()
                    console.print(
                        f"[bold red]DEBUG: {type(e).__name__}: {e}[/bold red]"
                    )
                raise e

        return {
            "prompt_eval_count": 0,
            "message": {
                "content": "[Error: max retries exceeded]",
                "tool_calls": None,
            },
        }

    return chat_fn


async def main():
    i18n.load_saved()
    args = parse_args()
    debug = args.debug
    verbose = args.verbose or args.debug

    model = args.model
    # Resolve context window: explicit override > backend-reported max > fallback.
    if args.context_window is not None:
        context_window = args.context_window
        ctx_source = "override"
    else:
        backend_ctx = await fetch_kobold_context_length(args.base_url)
        if backend_ctx and backend_ctx > 0:
            context_window = backend_ctx
            ctx_source = "backend"
        else:
            context_window = FALLBACK_CONTEXT_WINDOW
            ctx_source = "fallback"
    ctx_label = tr(f'kobold.ctx.{ctx_source}')
    if verbose:
        console.print(f"[dim]Context window: {context_window:,} tokens ({ctx_label})[/dim]")

    console.print(Panel(
        f"[bold cyan]Project Infinity × KoboldCpp[/bold cyan]\n"
        f"[dim]Base URL: {args.base_url}\n"
        f"[dim]{tr('kobold.panel', model=model, ctx=f'{context_window:,}', ctx_label=ctx_label, max_out=args.max_output_tokens)}[/dim]",
        expand=False
    ))

    # Basic connectivity sanity check (non-fatal)
    if verbose or debug:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as hc:
                health = await hc.get(args.base_url.replace("/v1", "/api/v1/info/version"))
                if health.status_code == 200:
                    console.print(f"[dim]KoboldCpp reachable: {health.text[:120]}[/dim]")
        except Exception as e:
            console.print(f"[yellow]Could not probe KoboldCpp at {args.base_url}: {e}[/yellow]")

    chat_fn = create_kobold_chat_fn(
        base_url=args.base_url,
        api_key=args.api_key,
        model=model,
        context_window=context_window,
        max_output_tokens=args.max_output_tokens,
        temperature=args.temperature,
        debug=debug,
    )

    await run_game(chat_fn, model, context_window, verbose=verbose, debug=debug)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print(f"\n[dim]{tr('entry.goodbye')}[/dim]")
    except SystemExit as e:
        sys.exit(e.code)
