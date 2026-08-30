"""Starlette server for the Project Infinity WebUI.

REST endpoints handle control (list worlds, pick a backend, store API keys,
start/stop a session); one WebSocket carries the game stream. Starlette is
already installed as an ``mcp`` dependency, so this adds no new requirement.
"""

import asyncio
import os
import socket
import sys
import threading
import time
import webbrowser

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

import game_engine
from webui import backends, config, session as sessions

AUTH_COOKIE = "pi_auth"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


# ── access control ──────────────────────────────────────────────────────────


class AuthMiddleware(BaseHTTPMiddleware):
    """Gate /api/* behind the PIN-issued token.

    Static files stay public so the login screen can load its own CSS and JS.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and not path.startswith("/api/login") \
                and not path.startswith("/api/pin"):
            if not config.token_ok(request.cookies.get(AUTH_COOKIE)):
                return JSONResponse({"ok": False, "error": "unauthorized"}, 401)
        return await call_next(request)


# ── REST ────────────────────────────────────────────────────────────────────


async def index(request: Request):
    from starlette.responses import FileResponse

    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


async def api_login(request: Request):
    data = await request.json()
    cfg = request.app.state.cfg
    if not config.pin_ok(cfg, data.get("pin")):
        return JSONResponse({"ok": False, "error": "PIN 不正确"}, 401)
    token = config.issue_token()
    response = JSONResponse({"ok": True})
    response.set_cookie(AUTH_COOKIE, token, httponly=True, samesite="lax")
    return response


async def api_logout(request: Request):
    response = JSONResponse({"ok": True})
    response.delete_cookie(AUTH_COOKIE)
    return response


async def api_get_pin(request: Request):
    # Only the machine running the server may read its own PIN. A LAN peer
    # hitting this from another IP gets 403, so the PIN never leaves localhost
    # — equivalent to the console banner, which is also only visible locally.
    host = (request.client.host if request.client else "") or ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse({"ok": False, "error": "forbidden"}, 403)
    return JSONResponse({"ok": True, "pin": request.app.state.cfg.get("pin", "")})


async def api_state(request: Request):
    cfg = request.app.state.cfg
    sess = request.app.state.session
    return JSONResponse({
        "ok": True,
        "worlds": sessions.list_worlds(),
        "backends": backends.describe(cfg),
        "backend": cfg.get("backend", backends.DEFAULT_BACKEND),
        "session": sess.info() if sess else None,
    })


async def api_backends(request: Request):
    return JSONResponse({
        "ok": True,
        "backends": backends.describe(request.app.state.cfg),
    })


async def api_save_backend(request: Request):
    backend_id = request.path_params["backend_id"]
    if backend_id not in backends.BACKENDS:
        return JSONResponse({"ok": False, "error": "未知后端"}, 404)
    data = await request.json()
    cfg = request.app.state.cfg
    options = config.set_backend_options(cfg, backend_id, data or {})
    if data.get("select"):
        cfg["backend"] = backend_id
        config.save(cfg)
    return JSONResponse({"ok": True, "options": options})


async def api_set_pin(request: Request):
    data = await request.json()
    new_pin = str(data.get("pin") or "").strip()
    if not new_pin:
        return JSONResponse({"ok": False, "error": "PIN 不能为空"}, 400)
    cfg = request.app.state.cfg
    cfg["pin"] = new_pin
    config.save(cfg)
    return JSONResponse({"ok": True})


def _resolve_world(world):
    if os.path.isabs(world):
        return world
    return os.path.join(game_engine.OUTPUT_DIR, world)


async def api_start_session(request: Request):
    cfg = request.app.state.cfg
    data = await request.json()
    backend_id = data.get("backend") or cfg.get("backend") or backends.DEFAULT_BACKEND
    options = data.get("options") or {}

    if backend_id not in backends.BACKENDS:
        return JSONResponse({"ok": False, "error": "未知后端"}, 404)

    stored = dict(config.backend_options(cfg, backend_id))
    stored.update({k: v for k, v in options.items() if v not in (None, "")})
    if stored:
        config.set_backend_options(cfg, backend_id, stored)
    cfg["backend"] = backend_id
    config.save(cfg)

    world = data.get("world")
    if not world:
        return JSONResponse({"ok": False, "error": "未选择世界"}, 400)

    try:
        sess = await sessions.create(_resolve_world(world), backend_id, stored)
    except sessions.SessionBusy as exc:
        return JSONResponse(
            {"ok": False, "error": f"存档「{exc}」已有会话在运行"}, 409)
    except backends.BackendError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 400)

    previous = request.app.state.session
    if previous is not None and previous is not sess:
        await previous.stop()
    request.app.state.session = sess
    return JSONResponse({"ok": True, "session": sess.info()})


async def api_stop_session(request: Request):
    sess = request.app.state.session
    if sess is None:
        return JSONResponse({"ok": True})
    await sess.stop()
    request.app.state.session = None
    return JSONResponse({"ok": True})


# ── WebSocket ───────────────────────────────────────────────────────────────


async def ws_game(websocket):
    if not config.token_ok(websocket.cookies.get(AUTH_COOKIE)):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    sess = websocket.app.state.session
    if sess is None:
        await websocket.send_json({"type": "error", "text": "没有进行中的会话"})
        await websocket.close()
        return

    async def read_loop():
        try:
            while True:
                message = await websocket.receive_json()
                if message.get("type") == "input" and message.get("text"):
                    sess.submit(message["text"])
        except Exception:
            return

    reader = asyncio.create_task(read_loop())
    try:
        async for event in sess.stream(replay=True):
            await websocket.send_json(event)
    except Exception:
        pass
    finally:
        reader.cancel()


# ── app ─────────────────────────────────────────────────────────────────────

routes = [
    Route("/", index),
    Route("/api/login", api_login, methods=["POST"]),
    Route("/api/pin", api_get_pin, methods=["GET"]),
    Route("/api/logout", api_logout, methods=["POST"]),
    Route("/api/state", api_state),
    Route("/api/backends", api_backends),
    Route("/api/backends/{backend_id}", api_save_backend, methods=["POST"]),
    Route("/api/pin", api_set_pin, methods=["POST"]),
    Route("/api/session", api_start_session, methods=["POST"]),
    Route("/api/session", api_stop_session, methods=["DELETE"]),
    WebSocketRoute("/ws", ws_game),
    Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
]


@asynccontextmanager
async def lifespan(app):
    yield
    await sessions.stop_all()


def create_app():
    cfg = config.load()
    fresh_pin = config.ensure_pin(cfg)
    app = Starlette(
        routes=routes,
        middleware=[Middleware(AuthMiddleware)],
        lifespan=lifespan,
    )
    app.state.cfg = cfg
    app.state.session = None
    app.state.fresh_pin = fresh_pin
    return app


app = create_app()


def _local_addresses():
    found = set()
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        found.add(probe.getsockname()[0])
        probe.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except Exception:
        pass
    found.discard("127.0.0.1")
    return sorted(found)


def _open_browser_when_ready(port, timeout=15.0):
    """Fire the OS default browser once the server actually accepts connections.

    uvicorn binds the port asynchronously, so opening too early hits a dead
    socket. A short probe loop waits for the listener before calling out.
    """
    url = f"http://127.0.0.1:{port}/"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                try:
                    webbrowser.open(url)
                except Exception:
                    pass  # headless / no default browser: URL is already printed
                return
        except OSError:
            time.sleep(0.25)
    # Give up silently — the URL is already printed for the user to click.


def main():
    cfg = app.state.cfg
    host = cfg.get("host") or config.DEFAULT_HOST
    port = int(cfg.get("port") or config.DEFAULT_PORT)

    # If the port is already taken, a previous instance is still running.
    # Don't stack another server + another auto-opened browser tab on a
    # repeated double-click — just point the user at the live one.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((str(host), port))
    except OSError:
        print()
        print("  Project Infinity — WebUI")
        print("  " + "-" * 42)
        print(f"  端口 {port} 已被占用，WebUI 应该已经在运行。")
        print(f"  直接访问    http://127.0.0.1:{port}")
        print()
        return
    finally:
        probe.close()

    print()
    print("  Project Infinity — WebUI")
    print("  " + "-" * 42)
    print(f"  本机访问    http://127.0.0.1:{port}")
    for address in _local_addresses():
        print(f"  局域网访问  http://{address}:{port}")
    if str(host) not in ("127.0.0.1", "localhost"):
        print(f"  监听地址    {host}:{port}  (局域网可访问)")
    print(f"  访问 PIN    {cfg.get('pin')}"
          + ("   ← 首次启动自动生成" if app.state.fresh_pin else ""))
    print(f"  配置文件    {config.CONFIG_PATH}")
    print()
    print("  按 Ctrl+C 停止服务")
    print()

    if str(cfg.get("open_browser", True)).lower() not in ("0", "false", "no", "off"):
        threading.Thread(
            target=_open_browser_when_ready, args=(port,), daemon=True
        ).start()

    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
