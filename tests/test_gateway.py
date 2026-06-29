"""End-to-end tests for the gateway routes, with litellm calls stubbed."""
from __future__ import annotations



import app.routing as routing


def _fake_completion(model: str, content: str = "hello") -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["providers"]["openai"] is True


async def test_chat_requires_auth(client):
    resp = await client.post("/v1/chat/completions", json={"model": "gpt-4o-mini", "messages": []})
    assert resp.status_code == 401


async def test_models_list(client, api_key):
    resp = await client.get("/v1/models", headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 200
    ids = {m["id"] for m in resp.json()["data"]}
    assert "gpt-4o-mini" in ids
    assert any(i.startswith("claude") for i in ids)


async def test_chat_completion_and_logging(client, api_key, monkeypatch):
    async def fake_acomplete(request, model, timeout):
        return _fake_completion(model)

    monkeypatch.setattr(routing, "acomplete", fake_acomplete)
    monkeypatch.setattr(routing, "cost_of", lambda m, p, c: 0.001)

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "hello"
    assert body["gateway"]["cost_usd"] == 0.001
    assert body["gateway"]["attempts"] == 1

    # The request should now appear in stats.
    stats = await client.get("/api/stats", headers={"Authorization": "Bearer test-admin"})
    assert stats.status_code == 200
    assert stats.json()["totals"]["requests"] >= 1


async def test_stats_by_key_attributes_spend(client, api_key, monkeypatch):
    """by_key surfaces which gateway key consumed the most, with its name."""
    async def fake_acomplete(request, model, timeout):
        return _fake_completion(model)

    monkeypatch.setattr(routing, "acomplete", fake_acomplete)
    monkeypatch.setattr(routing, "cost_of", lambda m, p, c: 0.002)

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text

    stats = await client.get(
        "/api/stats?days=1", headers={"Authorization": "Bearer test-admin"}
    )
    assert stats.status_code == 200
    by_key = stats.json()["by_key"]
    assert by_key, "expected at least one key bucket"
    top = by_key[0]
    # The fixture creates a key named "test"; spend must be attributed to it by name.
    assert top["name"] == "test"
    assert top["requests"] >= 1
    assert top["cost_usd"] >= 0.002


async def test_fallback_to_second_model(client, api_key, monkeypatch):
    calls = []

    async def fake_acomplete(request, model, timeout):
        calls.append(model)
        if model == "gpt-4o-mini":
            raise RuntimeError("primary down")
        return _fake_completion(model)

    monkeypatch.setattr(routing, "acomplete", fake_acomplete)
    monkeypatch.setattr(routing, "cost_of", lambda m, p, c: 0.0)

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "fallback_models": ["gemini-1.5-flash"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["gateway"]["resolved_model"] == "gemini/gemini-1.5-flash"
    assert calls == ["gpt-4o-mini", "gemini/gemini-1.5-flash"]


async def test_all_candidates_fail(client, api_key, monkeypatch):
    async def fake_acomplete(request, model, timeout):
        raise RuntimeError("everything down")

    monkeypatch.setattr(routing, "acomplete", fake_acomplete)

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "upstream_error"


async def test_streaming(client, api_key, monkeypatch):
    async def fake_astream(request, model, timeout):
        yield {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}
        yield {"choices": [{"index": 0, "delta": {"content": "hi"}}]}
        yield {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}

    monkeypatch.setattr(routing, "astream", fake_astream)
    monkeypatch.setattr(routing, "cost_of", lambda m, p, c: 0.0)

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    text = resp.text
    assert "data: " in text
    assert "[DONE]" in text
    # The streamed content chunk made it through.
    assert any('"content": "hi"' in line or '"content":"hi"' in line for line in text.splitlines())


async def test_stats_requires_admin(client, api_key):
    # A normal gateway key is not an admin key.
    resp = await client.get("/api/stats", headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 403


async def test_revoked_key_rejected(client, monkeypatch):
    create = await client.post(
        "/admin/keys", headers={"Authorization": "Bearer test-admin"}, json={"name": "temp"}
    )
    key = create.json()["api_key"]
    key_id = create.json()["id"]

    revoke = await client.delete(
        f"/admin/keys/{key_id}", headers={"Authorization": "Bearer test-admin"}
    )
    assert revoke.status_code == 204

    resp = await client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 401


_ADMIN = {"Authorization": "Bearer test-admin"}


async def test_patch_key_requires_admin(client, api_key):
    # A normal gateway key cannot update keys.
    resp = await client.patch("/admin/keys/1", headers={"Authorization": f"Bearer {api_key}"},
                              json={"monthly_budget_usd": 5})
    assert resp.status_code == 403


async def test_patch_key_updates_limits(client):
    create = await client.post(
        "/admin/keys", headers=_ADMIN,
        json={"name": "patchme", "monthly_budget_usd": 10, "rate_limit_per_min": 30},
    )
    key_id = create.json()["id"]

    resp = await client.patch(
        f"/admin/keys/{key_id}", headers=_ADMIN,
        json={"monthly_budget_usd": 50, "name": "renamed"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["monthly_budget_usd"] == 50
    assert body["name"] == "renamed"
    # Untouched field is preserved.
    assert body["rate_limit_per_min"] == 30


async def test_patch_key_clears_limit_with_null(client):
    create = await client.post(
        "/admin/keys", headers=_ADMIN, json={"name": "clearme", "monthly_budget_usd": 10}
    )
    key_id = create.json()["id"]

    resp = await client.patch(
        f"/admin/keys/{key_id}", headers=_ADMIN, json={"monthly_budget_usd": None}
    )
    assert resp.status_code == 200
    assert resp.json()["monthly_budget_usd"] is None


async def test_patch_key_can_reactivate(client):
    create = await client.post("/admin/keys", headers=_ADMIN, json={"name": "revived"})
    key = create.json()["api_key"]
    key_id = create.json()["id"]

    await client.delete(f"/admin/keys/{key_id}", headers=_ADMIN)
    assert (await client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})).status_code == 401

    resp = await client.patch(f"/admin/keys/{key_id}", headers=_ADMIN, json={"active": True})
    assert resp.status_code == 200
    assert resp.json()["active"] is True
    assert (await client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})).status_code == 200


async def test_patch_key_404(client):
    resp = await client.patch("/admin/keys/999999", headers=_ADMIN, json={"name": "x"})
    assert resp.status_code == 404


async def test_patch_key_empty_body_rejected(client):
    create = await client.post("/admin/keys", headers=_ADMIN, json={"name": "noop"})
    key_id = create.json()["id"]
    resp = await client.patch(f"/admin/keys/{key_id}", headers=_ADMIN, json={})
    assert resp.status_code == 400
