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
        "fields": ["base_url", "temperature"],
    },
}

DEFAULT_BACKEND = "ollama"
FALLBACK_CONTEXT_WINDOW = 8192


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


def context_window_for(backend_id, model):
    module = _module(backend_id)
    lengths = getattr(module, "MODEL_CONTEXT_LENGTHS", None) or {}
    if model in lengths:
        return int(lengths[model])
    if lengths:
        return int(next(iter(lengths.values())))
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
    kwargs["context_window"] = context_window_for(backend_id, model)
    kwargs["debug"] = debug

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
        if spec["needs_key"]:
            options["api_key"] = ""
        if backend_id == "kobold":
            options["base_url"] = "http://localhost:5001/v1"
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
                backend_id, options.get("model") or default_model(backend_id)),
        })
    return out
