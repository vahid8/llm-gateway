"""Tests for per-key monthly budget enforcement."""
from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import datetime, timezone

from fastapi.testclient import TestClient

ADMIN = {"Authorization": "Bearer admin-secret"}
BODY = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}


def _fresh_app(monkeypatch, tmp_path, **env):
    """Import a fresh app instance with an isolated DB and given env."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-secret")
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    for mod in list(sys.modules):
        if mod.startswith("app."):
            del sys.modules[mod]

    import app.config as config

    config.get_settings.cache_clear()

    import app.main as main

    importlib.reload(main)
    return main


def _make_completion_patch(monkeypatch, cost=0.01):
    """Patch the engine so each completion logs ``cost`` USD without network.

    Mirrors what ``routing.run_complete`` writes on success, using the same
    ``routing._log`` helper, so month-to-date spend accumulates exactly as in
    production.
    """
    async def fake_run_complete(request, settings, aliases, api_key_id):
        from app.routing import _log

        await _log(
            api_key_id=api_key_id,
            provider="openai",
            model="gpt-4o-mini",
            requested_model=request.model,
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            cost_usd=cost,
            latency_ms=1,
            streamed=False,
            status="ok",
            attempts=1,
        )
        return {"id": "x", "choices": []}

    monkeypatch.setattr("app.routes.chat.run_complete", fake_run_complete)


def _create_key(client, **body):
    resp = client.post("/admin/keys", json={"name": "test", **body}, headers=ADMIN)
    assert resp.status_code == 201, resp.text
    return resp.json()["api_key"], resp.json()["id"]


def test_budget_blocks_when_exceeded(monkeypatch, tmp_path):
    main = _fresh_app(monkeypatch, tmp_path)
    _make_completion_patch(monkeypatch, cost=0.01)
    with TestClient(main.app) as client:
        api_key, _ = _create_key(client, monthly_budget_usd=0.025)
        headers = {"Authorization": f"Bearer {api_key}"}
        # spend before each call: 0.00, 0.01, 0.02 -> all under 0.025
        for _ in range(3):
            r = client.post("/v1/chat/completions", json=BODY, headers=headers)
            assert r.status_code == 200, r.text
        # spend is now 0.03 >= 0.025 -> blocked
        r = client.post("/v1/chat/completions", json=BODY, headers=headers)
        assert r.status_code == 402, r.text
        assert "budget" in r.json()["detail"].lower()


def test_per_key_budget_overrides_global(monkeypatch, tmp_path):
    # Generous global default, but the key's own tiny cap must win.
    main = _fresh_app(monkeypatch, tmp_path, MONTHLY_BUDGET_USD="1000")
    _make_completion_patch(monkeypatch, cost=0.01)
    with TestClient(main.app) as client:
        api_key, _ = _create_key(client, monthly_budget_usd=0.025)
        headers = {"Authorization": f"Bearer {api_key}"}
        for _ in range(3):
            assert (
                client.post("/v1/chat/completions", json=BODY, headers=headers).status_code
                == 200
            )
        r = client.post("/v1/chat/completions", json=BODY, headers=headers)
        assert r.status_code == 402, r.text


def test_global_default_budget_applies(monkeypatch, tmp_path):
    # Key has no own budget -> the global default caps it.
    main = _fresh_app(monkeypatch, tmp_path, MONTHLY_BUDGET_USD="0.025")
    _make_completion_patch(monkeypatch, cost=0.01)
    with TestClient(main.app) as client:
        api_key, _ = _create_key(client)
        headers = {"Authorization": f"Bearer {api_key}"}
        for _ in range(3):
            assert (
                client.post("/v1/chat/completions", json=BODY, headers=headers).status_code
                == 200
            )
        r = client.post("/v1/chat/completions", json=BODY, headers=headers)
        assert r.status_code == 402, r.text


def test_no_budget_is_unlimited(monkeypatch, tmp_path):
    main = _fresh_app(monkeypatch, tmp_path, MONTHLY_BUDGET_USD="0")
    _make_completion_patch(monkeypatch, cost=0.01)
    with TestClient(main.app) as client:
        api_key, _ = _create_key(client)
        headers = {"Authorization": f"Bearer {api_key}"}
        for _ in range(6):
            r = client.post("/v1/chat/completions", json=BODY, headers=headers)
            assert r.status_code == 200, r.text


def test_prior_month_spend_excluded(monkeypatch, tmp_path):
    main = _fresh_app(monkeypatch, tmp_path)
    _make_completion_patch(monkeypatch, cost=0.01)
    with TestClient(main.app) as client:
        api_key, key_id = _create_key(client, monthly_budget_usd=0.025)
        headers = {"Authorization": f"Bearer {api_key}"}

        # Seed a large spend dated to a prior month; it must not count.
        async def _seed_old():
            from app.db import SessionLocal
            from app.models import RequestLog

            async with SessionLocal() as session:
                old = RequestLog(
                    api_key_id=key_id,
                    model="gpt-4o-mini",
                    requested_model="gpt-4o-mini",
                    provider="openai",
                    cost_usd=5.0,
                    status="ok",
                )
                old.created_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
                session.add(old)
                await session.commit()

        asyncio.run(_seed_old())

        # Current-month spend is 0, so the request is allowed despite the $5 history.
        r = client.post("/v1/chat/completions", json=BODY, headers=headers)
        assert r.status_code == 200, r.text
