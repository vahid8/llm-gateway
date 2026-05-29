"""Unit tests for the litellm engine helpers (no network)."""
from __future__ import annotations

from app.engine import _litellm_kwargs, normalize_model
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
