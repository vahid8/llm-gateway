"""/v1/embeddings: passthrough, cost logging, fallback, credential safety.

litellm is stubbed (no network). These assert the gateway's contract: the
embedding request reaches the provider via the allowlist, the response is
returned OpenAI-shaped with a gateway block, usage is logged for cost, and
client credential overrides are never forwarded.
"""
from __future__ import annotations

import app.routing as routing
from app.engine import _embedding_kwargs
from app.schemas import EmbeddingRequest

EMBED = {"object": "list", "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}]}


def _fake_aembedding(usage_tokens=5):
    async def fake_aembed(request, model, timeout):
        return {
            **EMBED,
            "model": model,
            "usage": {"prompt_tokens": usage_tokens, "total_tokens": usage_tokens},
        }

    return fake_aembed


# --- Unit: engine kwargs --------------------------------------------------
def test_embedding_params_forwarded():
    req = EmbeddingRequest(
        model="text-embedding-3-small",
        input="hello world",
        dimensions=256,
        encoding_format="float",
    )
    kwargs = _embedding_kwargs(req, "text-embedding-3-small", 30.0)
    assert kwargs["input"] == "hello world"
    assert kwargs["dimensions"] == 256
    assert kwargs["encoding_format"] == "float"
    assert kwargs["model"] == "text-embedding-3-small"


def test_embedding_credential_overrides_dropped():
    req = EmbeddingRequest(
        model="text-embedding-3-small",
        input="x",
        api_base="https://attacker.example",
        api_key="sk-attacker",
        custom_llm_provider="openai",
    )
    kwargs = _embedding_kwargs(req, "text-embedding-3-small", 30.0)
    for forbidden in ("api_base", "api_key", "custom_llm_provider", "base_url"):
        assert forbidden not in kwargs


def test_embedding_accepts_list_input():
    req = EmbeddingRequest(model="m", input=["a", "b", "c"])
    assert _embedding_kwargs(req, "m", 30.0)["input"] == ["a", "b", "c"]


# --- End-to-end through the route -----------------------------------------
async def test_embeddings_endpoint_ok(client, api_key, monkeypatch):
    monkeypatch.setattr(routing, "aembed", _fake_aembedding(usage_tokens=7))
    monkeypatch.setattr(routing, "cost_of", lambda m, p, c: 0.00002)

    resp = await client.post(
        "/v1/embeddings",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "text-embedding-3-small", "input": "hello"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"][0]["embedding"] == [0.1, 0.2, 0.3]
    # Gateway annotates cost/provider like it does for completions.
    assert body["gateway"]["cost_usd"] == 0.00002
    assert body["gateway"]["resolved_model"] == "text-embedding-3-small"


async def test_embeddings_requires_auth(client):
    resp = await client.post(
        "/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": "hello"},
    )
    assert resp.status_code == 401, resp.text


async def test_embeddings_logged_for_stats(client, api_key, monkeypatch):
    # Verify logging via the admin /api/stats endpoint through the *same* client,
    # rather than reaching into the global SessionLocal — another test
    # (test_budget) reloads app modules against a different DB, so global module
    # state isn't a reliable handle on what this app instance wrote.
    # Unique token/cost values make the assertion robust to other rows.
    monkeypatch.setattr(routing, "aembed", _fake_aembedding(usage_tokens=4242))
    monkeypatch.setattr(routing, "cost_of", lambda m, p, c: 0.0424242)

    resp = await client.post(
        "/v1/embeddings",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "text-embedding-3-small", "input": "hello"},
    )
    assert resp.status_code == 200, resp.text

    stats = await client.get(
        "/api/stats?days=1", headers={"Authorization": "Bearer test-admin"}
    )
    assert stats.status_code == 200, stats.text
    recent = stats.json()["recent"]
    # Find this call by its unique token count; embeddings log completion=0, so
    # total_tokens == prompt_tokens == 4242 and the stats row reflects it.
    mine = [r for r in recent if r["tokens"] == 4242]
    assert len(mine) == 1, recent
    assert mine[0]["model"] == "text-embedding-3-small"
    assert mine[0]["cost_usd"] == round(0.0424242, 6)


async def test_embeddings_fallback_on_error(client, api_key, monkeypatch):
    from app.engine import APIConnectionError

    calls = []

    async def flaky_aembed(request, model, timeout):
        calls.append(model)
        if model == "text-embedding-3-small":
            raise APIConnectionError(
                message="boom", llm_provider="openai", model=model
            )
        return {
            **EMBED,
            "model": model,
            "usage": {"prompt_tokens": 3, "total_tokens": 3},
        }

    monkeypatch.setattr(routing, "aembed", flaky_aembed)
    monkeypatch.setattr(routing, "cost_of", lambda m, p, c: 0.0)

    resp = await client.post(
        "/v1/embeddings",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "text-embedding-3-small",
            "input": "hello",
            "fallback_models": ["gemini/text-embedding-004"],
        },
    )
    assert resp.status_code == 200, resp.text
    # Primary was retried then the fallback succeeded.
    assert "text-embedding-3-small" in calls
    assert resp.json()["gateway"]["resolved_model"] == "gemini/text-embedding-004"
