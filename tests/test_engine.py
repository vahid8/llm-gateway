"""Unit tests for the litellm engine helpers (no network)."""
from __future__ import annotations

import app.engine as engine
from app.engine import _litellm_kwargs, normalize_model, provider_of
from app.config import Settings
from app.schemas import ChatCompletionRequest


def test_normalize_bare_openai_unchanged():
    assert normalize_model("gpt-4o-mini", {}) == "gpt-4o-mini"
    assert normalize_model("o4-mini", {}) == "o4-mini"


def test_normalize_claude_prefixed():
    assert normalize_model("claude-3-5-sonnet-20241022", {}) == "anthropic/claude-3-5-sonnet-20241022"


def test_normalize_gemini_prefixed():
    assert normalize_model("gemini-1.5-flash", {}) == "gemini/gemini-1.5-flash"


def test_normalize_explicit_provider_kept():
    assert normalize_model("anthropic/claude-x", {}) == "anthropic/claude-x"


def test_normalize_alias_wins():
    aliases = {"fast": "gemini/gemini-1.5-flash"}
    assert normalize_model("fast", aliases) == "gemini/gemini-1.5-flash"


def test_litellm_kwargs_drops_injected_credentials_and_endpoints():
    # A malicious client tries to redirect the call / override credentials.
    request = ChatCompletionRequest.model_validate(
        {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.5,
            "api_base": "https://attacker.example",
            "api_key": "sk-attacker",
            "base_url": "https://attacker.example",
            "custom_llm_provider": "openai",
        }
    )
    kwargs = _litellm_kwargs(request, "openai/gpt-4o-mini", timeout=30.0)
    # Only safe, gateway-controlled params survive.
    assert kwargs["model"] == "openai/gpt-4o-mini"
    assert kwargs["temperature"] == 0.5
    assert kwargs["messages"]
    for dangerous in ("api_base", "api_key", "base_url", "custom_llm_provider", "fallback_models"):
        assert dangerous not in kwargs


# --- Doubleword (OpenAI-compatible) provider -----------------------------
def test_normalize_doubleword_prefix_kept():
    # Explicit provider/model with a slashed model id passes through untouched.
    m = "doubleword/Qwen/Qwen3-VL-235B-A22B-Instruct-FP8"
    assert normalize_model(m, {}) == m


def test_provider_of_doubleword():
    assert provider_of("doubleword/Qwen/Qwen3-VL-235B-A22B-Instruct-FP8") == "doubleword"


def test_doubleword_routed_through_openai_adapter_with_server_creds(monkeypatch):
    # configure_keys populates the server-side endpoint/key for doubleword.
    # Hermetic: ignore the developer's local .env AND any ambient *_API_KEY_FILE
    # that `uv run` may have loaded into the environment.
    settings = Settings(
        _env_file=None,
        doubleword_api_key="sk-server-doubleword",
        doubleword_api_key_file=None,
        doubleword_base_url="https://api.doubleword.ai/v1",
    )
    engine.configure_keys(settings)
    try:
        request = ChatCompletionRequest.model_validate(
            {
                "model": "doubleword/Qwen/Qwen3-VL-235B-A22B-Instruct-FP8",
                "messages": [{"role": "user", "content": "hi"}],
                # Client attempts to hijack the endpoint/key — must be ignored.
                "api_base": "https://attacker.example",
                "api_key": "sk-attacker",
            }
        )
        kwargs = _litellm_kwargs(
            request, "doubleword/Qwen/Qwen3-VL-235B-A22B-Instruct-FP8", timeout=30.0
        )
        # doubleword/<id> is sent to litellm's openai adapter, preserving the
        # slashed id, with the SERVER's endpoint + key (not the client's).
        assert kwargs["model"] == "openai/Qwen/Qwen3-VL-235B-A22B-Instruct-FP8"
        assert kwargs["api_base"] == "https://api.doubleword.ai/v1"
        assert kwargs["api_key"] == "sk-server-doubleword"
    finally:
        # reset module state without picking up the ambient key file
        engine.configure_keys(Settings(_env_file=None, doubleword_api_key_file=None))
