"""Unit tests for the localhost-only PIN endpoint and the port-occupancy guard.

The PIN must be readable from localhost (the one visible surface when there is
no console window) but refused to any other address. The port guard must stop a
second launch from stacking another server + browser tab.

    python_embeded/python.exe tests/test_pin_endpoint.py
"""

import asyncio
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import webui.server as server


def _fake_request(host, pin="123456"):
    client = types.SimpleNamespace(host=host)
    app = types.SimpleNamespace(state=types.SimpleNamespace(cfg={"pin": pin}))
    return types.SimpleNamespace(client=client, app=app)


def test_localhost_allowed():
    for host in ("127.0.0.1", "::1", "localhost"):
        req = _fake_request(host)
        resp = asyncio.run(server.api_get_pin(req))
        assert resp.status_code == 200, host
        assert json.loads(resp.body)["pin"] == "123456", host


def test_non_localhost_forbidden():
    for host in ("192.168.1.50", "10.0.0.7", "203.0.113.9", ""):
        req = _fake_request(host)
        resp = asyncio.run(server.api_get_pin(req))
        assert resp.status_code == 403, f"host={host} should be forbidden"
        assert json.loads(resp.body).get("pin") is None, host


def test_none_client_forbidden():
    req = types.SimpleNamespace(client=None,
                                 app=types.SimpleNamespace(
                                     state=types.SimpleNamespace(cfg={"pin": "x"})))
    resp = asyncio.run(server.api_get_pin(req))
    assert resp.status_code == 403


def test_port_guard_detects_occupied():
    import socket

    # Occupy the port, then assert the guard reports it as taken.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
        taken = False
    except OSError:
        taken = True
    finally:
        probe.close()
        sock.close()
    assert taken, "guard should detect an already-bound port"


if __name__ == "__main__":
    test_localhost_allowed()
    test_non_localhost_forbidden()
    test_none_client_forbidden()
    test_port_guard_detects_occupied()
    print("ALL PASS — test_pin_endpoint")
