"""Test fixtures.

DATABASE_URL and keys are set *before* importing the app, because app.db binds
its async engine at import time.
"""
from __future__ import annotations

import os
import tempfile

import pytest_asyncio

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_PATH}"
os.environ["ADMIN_API_KEY"] = "test-admin"
os.environ["OPENAI_API_KEY"] = "sk-test-openai"
os.environ["ANTHROPIC_API_KEY"] = "sk-test-anthropic"
os.environ["GEMINI_API_KEY"] = "test-gemini"

import httpx  # noqa: E402
from asgi_lifespan import LifespanManager  # noqa: E402

from app.main import create_app  # noqa: E402


def pytest_unconfigure(config):
    os.close(_DB_FD)
    if os.path.exists(_DB_PATH):
        os.remove(_DB_PATH)


@pytest_asyncio.fixture
async def client():
    app = create_app()
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            yield c


@pytest_asyncio.fixture
async def api_key(client):
    """Create a gateway key via the admin API and return the raw key string."""
    resp = await client.post(
        "/admin/keys",
        headers={"Authorization": "Bearer test-admin"},
        json={"name": "test"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["api_key"]
