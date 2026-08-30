# savemgr.py
# Persistence primitives for Project Infinity: atomic writes, backup rotation,
# corruption-tolerant reads, and tool-call-pair-safe history truncation.
#
# This module is deliberately dumb: no MCP, no game rules, no business logic.
# It only knows how to put JSON on disk safely and how to cut a conversation
# down to size without breaking provider protocols.
#
# Standard library only — the embedded interpreter has no third-party packages
# beyond what the project already vendors (notably: no extra deps allowed).

import json
import os

# Ensure the project root is importable even when launched via an embedded
# interpreter from a different working directory (mirrors game_engine.py:17-19).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in __import__("sys").path:
    __import__("sys").path.insert(0, _HERE)


# ── Tunables ─────────────────────────────────────────────────────────────────
# Newest backup is <file>.bak, then .bak.1, .bak.2 ... up to MAX_BACKUPS-1.
MAX_BACKUPS = 3
# How many conversation messages to load back at startup.
HISTORY_WINDOW = 40
# Hard ceiling for the serialized history; budget is halved until it fits.
MAX_HISTORY_BYTES = 2 * 1024 * 1024


# ── Backup rotation ──────────────────────────────────────────────────────────

def _backup_path(path, index):
    """index 0 -> <path>.bak, 1 -> <path>.bak.1, ..."""
    return path + ".bak" if index == 0 else f"{path}.bak.{index}"


def backup_chain(path, keep=MAX_BACKUPS):
    """Candidate read order: live file first, then newest backup to oldest."""
    return [path] + [_backup_path(path, i) for i in range(keep)]


def rotate_backup(path, keep=MAX_BACKUPS):
    """Shift existing backups up one slot, then move the live file to .bak.

    Called *before* writing, so a failed write still leaves the last good copy
    in .bak. Uses os.replace (a rename, not a copy) so it is atomic on Windows.
    """
    if keep <= 0 or not os.path.exists(path):
        return
    # Drop the oldest slot.
    oldest = _backup_path(path, keep - 1)
    if os.path.exists(oldest):
        try:
            os.remove(oldest)
        except OSError:
            pass
    # Shift .bak.(i-1) -> .bak.i, oldest first so nothing is clobbered.
    for i in range(keep - 1, 0, -1):
        src = _backup_path(path, i - 1)
        dst = _backup_path(path, i)
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    try:
        os.replace(path, _backup_path(path, 0))
    except OSError:
        pass


# ── Atomic write / tolerant read ─────────────────────────────────────────────

def atomic_write_json(path, data, keep=MAX_BACKUPS):
    """Write JSON via a temp file + os.replace, keeping `keep` backups.

    The temp name carries the pid so concurrent processes never collide.
    Returns True on success. Never raises for expected filesystem failures —
    autosave must not take the game down.
    """
    try:
        directory = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(directory, exist_ok=True)
        rotate_backup(path, keep)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def safe_load_json(path, default=None, keep=MAX_BACKUPS):
    """Read JSON, falling back through the backup chain on missing/corrupt data.

    Returns `default` when every candidate is unreadable.
    """
    for candidate in backup_chain(path, keep):
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError, UnicodeDecodeError):
            continue
    return default


# ── Conversation history: tool-call pairing ──────────────────────────────────
# OpenAI / Gemini / Claude reject a request where an assistant message carries
# tool_calls that are not immediately followed by exactly that many role:"tool"
# messages. A naive "keep the last N" slice can cut right through such a group,
# so truncation has to operate on whole turn blocks and repair or drop any
# half-finished tool exchange.

def _split_blocks(turns):
    """Cut the message list into blocks, each starting at a user message.

    Anything before the first user message is dropped (orphaned assistant/tool
    entries that cannot stand alone in a request).
    """
    blocks = []
    current = []
    for msg in turns:
        if msg.get("role") == "user":
            if current:
                blocks.append(current)
            current = [msg]
        elif current:
            current.append(msg)
    if current:
        blocks.append(current)
    return blocks


def _degrade_pending(out):
    """The previous assistant declared tool_calls but got no results.

    If it has text content, strip the tool_calls and keep it as a plain
    assistant message; otherwise drop it (and anything trailing it).
    """
    i = len(out) - 1
    while i >= 0 and out[i].get("role") != "assistant":
        i -= 1
    if i < 0:
        return
    msg = out[i]
    if msg.get("content"):
        replacement = {"role": "assistant", "content": msg["content"]}
        # Keep the thinking payload: DeepSeek echoes it back as
        # reasoning_content and dropping it silently is worse than keeping it.
        if msg.get("thinking"):
            replacement["thinking"] = msg["thinking"]
        out[i] = replacement
    else:
        del out[i:]


def _repair_block(block):
    """Drop orphan tool messages and repair/drop tool_calls missing results."""
    out = []
    expected = 0
    for msg in block:
        role = msg.get("role")
        if role == "tool":
            if expected > 0:
                out.append(msg)
                expected -= 1
            continue
        if expected > 0:
            _degrade_pending(out)
            expected = 0
        if role == "assistant":
            calls = msg.get("tool_calls") or []
            out.append(msg)
            expected = len(calls)
        else:
            out.append(msg)
    if expected > 0:
        _degrade_pending(out)
    return out


def validate_pairs(turns):
    """True when every assistant tool_calls group is fully answered."""
    i = 0
    n = len(turns)
    while i < n:
        msg = turns[i]
        calls = msg.get("tool_calls") if msg.get("role") == "assistant" else None
        if not calls:
            i += 1
            continue
        need = len(calls)
        j = i + 1
        while j < n and turns[j].get("role") == "tool":
            j += 1
        if j - (i + 1) != need:
            return False
        i = j
    return True


def truncate_history(turns, budget=HISTORY_WINDOW):
    """Keep the most recent whole turns, never splitting a tool exchange."""
    if budget <= 0 or not turns:
        return []
    blocks = []
    for block in _split_blocks(turns):
        repaired = _repair_block(block)
        if repaired:
            blocks.append(repaired)
    if not blocks:
        return []
    collected = []
    for block in reversed(blocks):
        if len(collected) + len(block) <= budget:
            collected = block + collected
        else:
            # Integrity beats budget: always keep at least the latest block.
            if not collected:
                collected = block
            break
    return collected


def prepare_history(turns, budget=HISTORY_WINDOW):
    """Truncate and verify. The result is always safe to send to a provider."""
    result = truncate_history(turns, budget)
    while result and not validate_pairs(result):
        repaired = _repair_block(result)
        if repaired == result:
            return []
        result = repaired
    return result


def fit_history_bytes(turns, max_bytes=MAX_HISTORY_BYTES, budget=HISTORY_WINDOW):
    """Same as prepare_history, additionally shrinking the budget to fit bytes."""
    while budget > 0:
        out = prepare_history(turns, budget)
        if not out:
            return out
        try:
            size = len(json.dumps(out, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            return []
        if size <= max_bytes:
            return out
        budget //= 2
    return []


def strip_system(messages):
    """System messages are rebuilt at startup; never replay them from disk."""
    return [m for m in messages if m.get("role") != "system"]
