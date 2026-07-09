"""Server-side default fallback chains (FALLBACK_CHAINS)."""
from __future__ import annotations

from app import routing
from app.config import Settings, get_settings
from app.routing import _candidates
from app.schemas import ChatCompletionRequest, EmbeddingRequest


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def _chat(model: str, fallbacks=None) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        fallback_models=fallbacks,
    )


# --- _candidates unit tests ----------------------------------------------
def test_exact_chain_applies_when_client_sends_none():
    s = _settings(
        fallback_chains='{"anthropic/claude-haiku-4-5": ["gemini/gemini-2.5-flash"]}'
    )
    # Bare model name normalizes to the anthropic/ key.
    assert _candidates(_chat("claude-haiku-4-5"), s, {}) == [
        "claude-haiku-4-5",
        "gemini/gemini-2.5-flash",
    ]


def test_wildcard_chain_with_exact_entry_precedence():
    s = _settings(
        fallback_chains=(
            '{"anthropic/*": ["gemini/gemini-2.5-flash"],'
            ' "anthropic/claude-x": ["gpt-4o-mini"]}'
        )
    )
    assert _candidates(_chat("claude-opus-9"), s, {}) == [
        "claude-opus-9",
        "gemini/gemini-2.5-flash",
    ]
    # An exact model entry wins over the provider wildcard.
    assert _candidates(_chat("anthropic/claude-x"), s, {}) == [
        "anthropic/claude-x",
        "gpt-4o-mini",
    ]


def test_client_fallbacks_win_over_server_chain():
    s = _settings(fallback_chains='{"anthropic/*": ["gemini/gemini-2.5-flash"]}')
    assert _candidates(_chat("claude-haiku-4-5", ["gpt-4o-mini"]), s, {}) == [
        "claude-haiku-4-5",
        "gpt-4o-mini",
    ]


def test_no_chain_leaves_candidates_unchanged():
    assert _candidates(_chat("claude-haiku-4-5"), _settings(), {}) == [
        "claude-haiku-4-5"
    ]


def test_chain_entry_equal_to_primary_is_deduped():
    # "haiku" aliases to the primary itself — must not become a second candidate.
    aliases = {"haiku": "anthropic/claude-haiku-4-5"}
    s = _settings(
        fallback_chains='{"anthropic/*": ["haiku", "gemini/gemini-2.5-flash"]}'
    )
    assert _candidates(_chat("claude-haiku-4-5"), s, aliases) == [
        "claude-haiku-4-5",
        "gemini/gemini-2.5-flash",
    ]


def test_embeddings_ignore_wildcard_but_use_exact_entry():
    s = _settings(
        fallback_chains=(
            '{"gemini/*": ["anthropic/claude-haiku-4-5"],'
            ' "gemini/gemini-embedding-001": ["openai/text-embedding-3-small"]}'
        )
    )
    exact = EmbeddingRequest(model="gemini/gemini-embedding-001", input="hi")
    assert _candidates(exact, s, {}, wildcard=False) == [
        "gemini/gemini-embedding-001",
        "openai/text-embedding-3-small",
    ]
    # Provider-wide chat chain must NOT leak into embedding requests.
    other = EmbeddingRequest(model="gemini/some-other-embed", input="hi")
    assert _candidates(other, s, {}, wildcard=False) == ["gemini/some-other-embed"]


# --- config parsing -------------------------------------------------------
def test_fallback_chain_map_parses_and_coerces():
    s = _settings(
        fallback_chains=(
            '{"a/*": "b/c", "d/e": ["f/g", 1, ""], "": ["x"], "h/i": []}'
        )
    )
    assert s.fallback_chain_map == {"a/*": ["b/c"], "d/e": ["f/g"]}


def test_fallback_chain_map_malformed_degrades_to_empty():
    assert _settings(fallback_chains="not json").fallback_chain_map == {}
    assert _settings(fallback_chains='["a/b"]').fallback_chain_map == {}


# --- end-to-end through the chat route ------------------------------------
async def test_chat_falls_back_via_server_chain(client, api_key, monkeypatch):
    # get_settings() is lru_cached — patch the live instance the route uses.
    monkeypatch.setattr(
        get_settings(), "fallback_chains", '{"openai/*": ["gemini/gemini-2.5-flash"]}'
    )
    calls = []

    async def fake_acomplete(request, model, timeout):
        calls.append(model)
        if model == "gpt-4o-mini":
            raise RuntimeError("primary down")
        return {
            "id": "cmpl-1",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(routing, "acomplete", fake_acomplete)
    monkeypatch.setattr(routing, "cost_of", lambda m, p, c: 0.0)

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            # no fallback_models — the server chain must kick in
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["gateway"]["resolved_model"] == "gemini/gemini-2.5-flash"
    assert calls == ["gpt-4o-mini", "gemini/gemini-2.5-flash"]
