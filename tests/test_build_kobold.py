"""KoboldCpp / local OpenAI-compatible backend construction.

Regression guard for the BackendError: "缺少必需配置：api_key、max_output_tokens".
The factory create_kobold_chat_fn requires api_key and max_output_tokens with no
default; build_chat_fn must fill them so a LAN server with no key connects.
"""

import asyncio
import sys

sys.path.insert(0, ".")

from webui import backends
from webui.backends import BackendError


def test_kobold_connects_without_key_or_max_tokens():
    """Minimal options (only what the UI sends for a bare LAN server) build OK."""
    options = {
        "base_url": "http://192.168.1.50:5001/v1",
        "model": backends.default_model("kobold"),
    }
    fn = backends.build_chat_fn("kobold", options)
    assert callable(fn), "build_chat_fn should return a chat_fn"


def test_kobold_accepts_explicit_key_and_max_tokens():
    options = {
        "base_url": "http://192.168.1.50:5001/v1",
        "model": backends.default_model("kobold"),
        "api_key": "super-secret-lan-token",
        "max_output_tokens": 4096,
    }
    fn = backends.build_chat_fn("kobold", options)
    assert callable(fn)


def test_required_backends_still_reject_empty_key():
    """needs_key=True backends must still fail on a missing api_key."""
    for bid in ("deepseek", "gpt", "claude", "gemini", "nano"):
        options = {"model": backends.default_model(bid)}
        try:
            backends.build_chat_fn(bid, options)
            raised = False
        except BackendError:
            raised = True
        assert raised, f"{bid} should require api_key"


def test_describe_exposes_kobold_optional_fields():
    cfg = {"backends": {}}
    listed = backends.describe(cfg)
    kobold = next(b for b in listed if b["id"] == "kobold")
    assert "api_key" in kobold["fields"]
    assert "max_output_tokens" in kobold["fields"]
    assert kobold["needs_key"] is False
    # optional api_key present in options (empty), so the UI can show it
    assert "api_key" in kobold["options"]
    assert kobold["options"]["max_output_tokens"] == backends.DEFAULT_MAX_OUTPUT_TOKENS


if __name__ == "__main__":
    for fn in (
        test_kobold_connects_without_key_or_max_tokens,
        test_kobold_accepts_explicit_key_and_max_tokens,
        test_required_backends_still_reject_empty_key,
        test_describe_exposes_kobold_optional_fields,
    ):
        fn()
        print("PASS", fn.__name__)
    print("all kobold build tests passed")
