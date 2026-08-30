"""End-to-end test for the WebUI.

Boots the Starlette app in a background thread with a scripted fake LLM, then
drives it the way a browser would: login, start a session, stream events over
the WebSocket, send an action, read the dice result, quit.

The save directory is redirected to a temp dir first, so a run never touches
the real output/.

    python_embeded/python.exe tests/test_webui_e2e.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

import httpx
import uvicorn
from websockets.sync.client import connect

import game_engine
from webui import backends, config

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
CALLS = {"n": 0}


# ── scripted backend ────────────────────────────────────────────────────────


async def _fake_chat_fn(messages, tools, model, context_window):
    CALLS["n"] += 1
    n = CALLS["n"]
    if n == 1:
        return {"prompt_eval_count": 100,
                "message": {"content": "你在冰冷的石室中醒来。**铁门**半掩着。",
                            "tool_calls": None}}
    if n == 2:
        return {"prompt_eval_count": 120,
                "message": {"content": "", "tool_calls": [{
                    "function": {"name": "perform_check",
                                 "arguments": {"modifier": 5, "dc": 14,
                                               "check_name": "力量"}}}]}}
    return {"prompt_eval_count": 140,
            "message": {"content": "你推开铁门，走进祭室。", "tool_calls": None}}


# ── harness ─────────────────────────────────────────────────────────────────


def _isolate_saves():
    tmp = tempfile.mkdtemp(prefix="pi_webui_test_")
    for ext in (".wwf", ".player"):
        src = os.path.join(_ROOT, "output", "pui_weave" + ext)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(tmp, "pui_weave" + ext))
    game_engine.OUTPUT_DIR = tmp
    return tmp


def _start_server():
    import webui.server as server

    server_config = uvicorn.Config(
        server.app, host="127.0.0.1", port=PORT, log_level="error")
    uvi = uvicorn.Server(server_config)
    uvi.should_exit = False
    thread = threading.Thread(target=uvi.run, daemon=True)
    thread.start()
    for _ in range(60):
        try:
            httpx.get(BASE + "/", timeout=1.0)
            return uvi
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("server did not come up")


def _drain(ws, seconds=8):
    """Collect events until a prompt/closed arrives or we run out of time."""
    events = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            raw = ws.recv(timeout=max(0.2, deadline - time.time()))
        except Exception:
            break
        event = json.loads(raw)
        events.append(event)
        if event.get("type") in ("prompt", "closed"):
            break
    return events


def main():
    tmp = _isolate_saves()
    backends.build_chat_fn = lambda *a, **k: _fake_chat_fn

    cfg = config.load()
    pin = cfg.get("pin") or ""
    uvi = _start_server()

    checks = []

    def check(name, ok, detail=""):
        checks.append((name, ok, detail))

    try:
        # 1. unauthenticated API is refused
        r = httpx.get(BASE + "/api/state", timeout=5)
        check("未登录时 /api/state 返回 401", r.status_code == 401, str(r.status_code))

        # 2. wrong PIN rejected
        r = httpx.post(BASE + "/api/login", json={"pin": "0000000"}, timeout=5)
        check("错误 PIN 被拒绝", r.status_code == 401, str(r.status_code))

        # 3. login
        client = httpx.Client(base_url=BASE, timeout=10)
        r = client.post("/api/login", json={"pin": pin})
        check("正确 PIN 登录成功", r.status_code == 200, str(r.status_code))
        token = client.cookies.get("pi_auth")
        check("登录下发 token cookie", bool(token))

        # 4. state after login
        r = client.get("/api/state")
        data = r.json()
        worlds = data.get("worlds") or []
        check("登录后可读取 /api/state", r.status_code == 200)
        check("世界列表非空", len(worlds) > 0, f"{len(worlds)} 个")
        check("后端清单含 7 个后端", len(data.get("backends") or []) == 7,
              str(len(data.get("backends") or [])))

        # 5. start a session
        r = client.post("/api/session", json={
            "world": "pui_weave.wwf",
            "backend": "ollama",
            "options": {"model": "kimi-k2.6:cloud", "temperature": 0.0},
        })
        started = r.json()
        check("会话创建成功", r.status_code == 200 and started.get("ok"),
              str(started.get("error", "")))

        # 6. second session on the same save is refused
        r = client.post("/api/session", json={
            "world": "pui_weave.wwf", "backend": "ollama", "options": {}})
        check("同存档二次开局被拒绝（独占锁）", r.status_code == 409, str(r.status_code))

        # 7. stream: opening + first prompt
        with connect(f"ws://127.0.0.1:{PORT}/ws",
                     additional_headers=[("Cookie", f"pi_auth={token}")]) as ws:
            opening = _drain(ws)
            types = [e["type"] for e in opening]
            check("WebSocket 收到开场叙事", "narrative" in types, str(types))
            check("开场后进入等待输入", "prompt" in types, str(types))
            opening_text = " ".join(
                e.get("text", "") for e in opening if e["type"] == "narrative")
            check("叙事文本含开场内容", "石室" in opening_text)

            # 8. send an action -> dice tool -> narrative -> stats -> prompt
            ws.send(json.dumps({"type": "input", "text": "我用力推开铁门"}))
            turn = _drain(ws, seconds=12)
            ttypes = [e["type"] for e in turn]
            check("回合内收到工具事件", "tool" in ttypes, str(ttypes))

            tool = next((e for e in turn if e["type"] == "tool"), None)
            if tool:
                check("工具事件带 perform_check", tool.get("name") == "perform_check",
                      str(tool.get("name")))
                parsed = json.loads(tool.get("result") or "{}")
                check("骰子结果含 outcome/total",
                      "outcome" in parsed and "total" in parsed, str(parsed))
            else:
                check("工具事件带 perform_check", False)
                check("骰子结果含 outcome/total", False)

            check("回合内收到叙事", "narrative" in ttypes, str(ttypes))
            stats = next((e for e in turn if e["type"] == "stats"), None)
            check("回合内推送角色面板数据", stats is not None
                  and isinstance(stats.get("data"), dict))
            check("角色面板数据含角色名",
                  bool(stats and (stats.get("data") or {}).get("name")))
            check("回合末回到等待输入", "prompt" in ttypes, str(ttypes))

            # 9. quit
            ws.send(json.dumps({"type": "input", "text": "/quit"}))
            ending = _drain(ws, seconds=15)
            check("退出后收到 closed 事件",
                  any(e["type"] == "closed" for e in ending),
                  str([e["type"] for e in ending]))

        # 10. home page serves
        r = httpx.get(BASE + "/", timeout=5)
        check("首页可访问", r.status_code == 200 and "Project Infinity" in r.text)

        # 11. static assets
        for asset in ("/static/app.js", "/static/style.css"):
            r = httpx.get(BASE + asset, timeout=5)
            check(f"静态资源 {asset}", r.status_code == 200, str(r.status_code))

    finally:
        try:
            uvi.should_exit = True
        except Exception:
            pass
        time.sleep(0.5)
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 62)
    print("WebUI END-TO-END")
    print("=" * 62)
    failed = 0
    for name, ok, detail in checks:
        if not ok:
            failed += 1
        extra = f"   ({detail})" if detail and not ok else ""
        print(("  PASS  " if ok else "  FAIL  ") + name + extra)
    print()
    print("RESULT:", "ALL PASS" if not failed else f"{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
