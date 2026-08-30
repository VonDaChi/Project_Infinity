"""LLM backend registry for the WebUI.

Every entry points at a ``create_*_chat_fn`` factory in one of the existing
``play*.py`` scripts. The factory is imported, never copied, so the WebUI and
the CLI cannot drift apart — adding a backend to the CLI still means adding
exactly one entry here.

Factory signatures differ across adapters (only kobold takes base_url; only
gpt/claude take max_output_tokens), so arguments are filtered through
``inspect.signature`` and anything the factory does not accept is dropped.
"""

import importlib
import inspect
import json
import urllib.request

from webui import config

BACKENDS = {
    "ollama": {
        "label": "Ollama（本地）",
        "module": "play",
        "factory": "create_ollama_chat_fn",
        "needs_key": False,
        "fields": ["temperature"],
    },
    "deepseek": {
        "label": "DeepSeek",
        "module": "play_with_deepseek",
        "factory": "create_deepseek_chat_fn",
        "needs_key": True,
        "fields": ["api_key", "temperature"],
    },
    "gpt": {
        "label": "OpenAI GPT",
        "module": "play_with_gpt",
        "factory": "create_gpt_chat_fn",
        "needs_key": True,
        "fields": ["api_key", "temperature"],
    },
    "claude": {
        "label": "Anthropic Claude",
        "module": "play_with_claude",
        "factory": "create_claude_chat_fn",
        "needs_key": True,
        "fields": ["api_key", "temperature"],
    },
    "gemini": {
        "label": "Google Gemini",
        "module": "play_with_gemini",
        "factory": "create_gemini_chat_fn",
        "needs_key": True,
        "fields": ["api_key", "temperature"],
    },
    "nano": {
        "label": "Gemini Nano（生图）",
        "module": "play_with_nano",
        "factory": "create_gemini_chat_fn",
        "needs_key": True,
        "fields": ["api_key", "temperature"],
    },
    "kobold": {
        "label": "KoboldCpp / 本地 OpenAI 兼容",
        "module": "play_with_kobold",
        "factory": "create_kobold_chat_fn",
        "needs_key": False,
        "fields": ["base_url", "api_key", "max_output_tokens", "temperature"],
    },
}

DEFAULT_BACKEND = "ollama"
FALLBACK_CONTEXT_WINDOW = 8192
# Local / OpenAI-compatible servers (kobold) have no max_output_tokens default
# in their factory; gpt/claude default to 16384, so we match that convention.
DEFAULT_MAX_OUTPUT_TOKENS = 16384


class BackendError(Exception):
    """Raised when a backend cannot be constructed from the stored options."""


def _module(backend_id):
    return importlib.import_module(BACKENDS[backend_id]["module"])


def models_for(backend_id):
    """Model names the adapter advertises, best effort."""
    module = _module(backend_id)
    listed = getattr(module, "AVAILABLE_MODELS", None)
    if listed:
        return list(listed)
    lengths = getattr(module, "MODEL_CONTEXT_LENGTHS", None)
    if lengths:
        return list(lengths)
    single = getattr(module, "DEFAULT_MODEL", None)
    return [single] if single else []


def default_model(backend_id):
    models = models_for(backend_id)
    return models[0] if models else ""


# Cache real KoboldCpp context length per base_url — the network probe runs
# once per server, not on every describe()/build call.
_KOBOLD_CTX_CACHE = {}


def _kobold_real_context_length(base_url, timeout=2.0):
    """Synchronously probe KoboldCpp for its real max context length.

    KoboldCpp exposes this via its OpenAI-compatible surface. We use urllib
    (stdlib, no extra deps) so the call stays safe from a synchronous context
    such as inside an asyncio request handler — no event-loop juggling needed.
    Returns an int on success, else None.
    """
    native = (base_url or "").rstrip("/")
    if native.endswith("/v1"):
        native = native[:-3]
    if not native:
        return None
    for path in ("/api/v1/config/max_context_length", "/api/extra/true_max_context_length"):
        try:
            req = urllib.request.Request(
                native + path, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status != 200:
                    continue
                data = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            continue
        val = None
        if isinstance(data, dict):
            val = data.get("result", data.get("value", data.get("max_context_length")))
        elif isinstance(data, int):
            val = data
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    return None


def context_window_for(backend_id, model, base_url=None):
    module = _module(backend_id)
    lengths = getattr(module, "MODEL_CONTEXT_LENGTHS", None) or {}
    if model in lengths:
        return int(lengths[model])
    if lengths:
        return int(next(iter(lengths.values())))
    if backend_id == "kobold":
        # Ask the real backend for its context length instead of trusting the
        # 8192 fallback — otherwise a 16k model still gets capped and long GM
        # replies are silently truncated.
        cache_key = base_url or "<default>"
        if cache_key not in _KOBOLD_CTX_CACHE:
            real = _kobold_real_context_length(base_url) if base_url else None
            _KOBOLD_CTX_CACHE[cache_key] = int(real) if real else int(
                getattr(module, "FALLBACK_CONTEXT_WINDOW", FALLBACK_CONTEXT_WINDOW))
        return _KOBOLD_CTX_CACHE[cache_key]
    return int(getattr(module, "FALLBACK_CONTEXT_WINDOW", FALLBACK_CONTEXT_WINDOW))


def default_temperature(backend_id):
    module = _module(backend_id)
    for name in ("DEFAULT_TEMP", "DEFAULT_TEMPERATURE"):
        if hasattr(module, name):
            return getattr(module, name)
    return 0.0


def build_chat_fn(backend_id, options, debug=False):
    """Construct a chat_fn. Raises BackendError naming what is missing."""
    spec = BACKENDS[backend_id]
    factory = getattr(_module(backend_id), spec["factory"])

    model = options.get("model") or default_model(backend_id)
    kwargs = dict(options)
    kwargs["model"] = model
    # kobold's factory takes the context window directly; everyone else ignores it.
    kwargs["context_window"] = context_window_for(backend_id, model, options.get("base_url"))
    kwargs["debug"] = debug
    # Local / OpenAI-compatible servers (kobold) accept any non-empty key; when the
    # registry says no key is needed, inject a harmless placeholder so the OpenAI
    # client is happy. Backends that don't accept api_key ignore it (the signature
    # filter below drops it).
    if not spec["needs_key"] and not kwargs.get("api_key"):
        kwargs["api_key"] = "not-needed"
    # max_output_tokens: supply a sane default for factories without one (kobold's
    # factory has no default). Backends that don't accept it ignore it via the filter.
    kwargs.setdefault("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)

    signature = inspect.signature(factory)
    accepted = {k: v for k, v in kwargs.items() if k in signature.parameters}
    missing = [
        name
        for name, param in signature.parameters.items()
        if param.default is inspect.Parameter.empty and name not in accepted
    ]
    if missing:
        raise BackendError(
            f"{spec['label']} 缺少必需配置：{'、'.join(missing)}"
        )
    return factory(**accepted)


def describe(cfg):
    """Backend list for the settings UI, with stored options merged in."""
    out = []
    for backend_id, spec in BACKENDS.items():
        stored = config.backend_options(cfg, backend_id)
        options = {"temperature": default_temperature(backend_id)}
        # Show a field whenever the backend advertises it. api_key is required
        # only when needs_key is True; otherwise it stays optional (may be empty).
        if "api_key" in spec["fields"]:
            options["api_key"] = ""
        if "base_url" in spec["fields"]:
            options["base_url"] = "http://localhost:5001/v1"
        if "max_output_tokens" in spec["fields"]:
            options["max_output_tokens"] = DEFAULT_MAX_OUTPUT_TOKENS
        options["model"] = default_model(backend_id)
        options.update({k: v for k, v in stored.items() if v not in (None, "")})

        out.append({
            "id": backend_id,
            "label": spec["label"],
            "models": models_for(backend_id),
            "needs_key": spec["needs_key"],
            "fields": spec["fields"],
            "options": options,
            "context_window": context_window_for(
                backend_id, options.get("model") or default_model(backend_id),
                options.get("base_url")),
        })
    return out
