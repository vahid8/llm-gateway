"""Rate-limiter unit tests + an end-to-end 429 check."""
from __future__ import annotations

import app.routing as routing
from app.ratelimit import WINDOW_SECONDS, RateLimiter


def test_unlimited_when_limit_non_positive():
    rl = RateLimiter()
    for _ in range(100):
        allowed, retry = rl.check(1, 0, now=0.0)
        assert allowed and retry == 0


def test_allows_up_to_limit_then_blocks():
    rl = RateLimiter()
    assert rl.check(1, 2, now=0.0) == (True, 0)
    assert rl.check(1, 2, now=0.0) == (True, 0)
    allowed, retry = rl.check(1, 2, now=0.0)
    assert not allowed
    assert retry == WINDOW_SECONDS + 1  # oldest hit ages out a full window later


def test_keys_are_independent():
    rl = RateLimiter()
    assert rl.check(1, 1, now=0.0)[0] is True
    assert rl.check(1, 1, now=0.0)[0] is False
    assert rl.check(2, 1, now=0.0)[0] is True  # different key unaffected


def test_window_slides():
    rl = RateLimiter()
    assert rl.check(1, 1, now=0.0)[0] is True
    assert rl.check(1, 1, now=10.0)[0] is False
    # Past the window the old hit is evicted and a new request is allowed.
    assert rl.check(1, 1, now=WINDOW_SECONDS + 0.1)[0] is True


async def _make_key(client, rate_limit_per_min):
    resp = await client.post(
        "/admin/keys",
        headers={"Authorization": "Bearer test-admin"},
        json={"name": "limited", "rate_limit_per_min": rate_limit_per_min},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["rate_limit_per_min"] == rate_limit_per_min
    return body["api_key"]


async def test_chat_429_over_per_key_limit(client, monkeypatch):
    async def fake_acomplete(request, model, timeout):
        return {
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(routing, "acomplete", fake_acomplete)
    monkeypatch.setattr(routing, "cost_of", lambda m, p, c: 0.0)

    key = await _make_key(client, rate_limit_per_min=2)
    headers = {"Authorization": f"Bearer {key}"}
    payload = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}

    assert (await client.post("/v1/chat/completions", headers=headers, json=payload)).status_code == 200
    assert (await client.post("/v1/chat/completions", headers=headers, json=payload)).status_code == 200
    blocked = await client.post("/v1/chat/completions", headers=headers, json=payload)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
