"""WebUI configuration and access control.

State lives in ``config/webui.yml``, which is git-ignored because it holds API
keys and the access PIN. A corrupt or missing file falls back to defaults and
regenerates a PIN rather than locking the user out of their own server.
"""

import os
import secrets
import threading

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_HERE)
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "webui.yml")

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000

_DEFAULTS = {
    "host": DEFAULT_HOST,
    "port": DEFAULT_PORT,
    "pin": "",
    "backend": "ollama",
    "backends": {},
}

_write_lock = threading.Lock()
_tokens = set()


def _new_pin():
    return "".join(secrets.choice("0123456789") for _ in range(6))


def load():
    """Return the config dict, merged over defaults. Never raises."""
    cfg = dict(_DEFAULTS)
    cfg["backends"] = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if isinstance(data, dict):
                for key, value in data.items():
                    if value is not None:
                        cfg[key] = value
        except Exception:
            pass
    if not isinstance(cfg.get("backends"), dict):
        cfg["backends"] = {}
    return cfg


def save(cfg):
    """Persist the config atomically enough for a settings file."""
    with _write_lock:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, CONFIG_PATH)


def ensure_pin(cfg):
    """Generate and persist a PIN if none is set. Returns True if it did."""
    if cfg.get("pin"):
        return False
    cfg["pin"] = _new_pin()
    save(cfg)
    return True


def backend_options(cfg, backend_id):
    """Stored options for one backend (api_key, model, temperature, ...)."""
    return cfg.get("backends", {}).get(backend_id) or {}


def set_backend_options(cfg, backend_id, values):
    """Merge ``values`` into one backend's options and persist."""
    current = dict(backend_options(cfg, backend_id))
    current.update({k: v for k, v in values.items() if v is not None})
    cfg.setdefault("backends", {})[backend_id] = current
    save(cfg)
    return current


def pin_ok(cfg, submitted):
    """Constant-time PIN check. An unset PIN always fails."""
    pin = str(cfg.get("pin") or "")
    if not pin:
        return False
    return secrets.compare_digest(str(submitted or ""), pin)


def issue_token():
    """Mint an opaque session token.

    The PIN itself is never stored in the cookie — only this random value, kept
    in memory. Restarting the server invalidates every session.
    """
    token = secrets.token_urlsafe(32)
    _tokens.add(token)
    return token


def token_ok(token):
    return bool(token) and token in _tokens
