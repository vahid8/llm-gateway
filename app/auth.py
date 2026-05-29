"""Gateway authentication.

Two credential types:
  * Gateway API keys (`sk-...`) — issued to clients, stored as SHA-256 hashes.
  * Admin key — single shared secret (env) guarding key management + dashboard.

Provider keys are never accepted from clients; they live only in settings.
"""
from __future__ import annotations

import hashlib
import secrets

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import ApiKey

KEY_BYTES = 24


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_key() -> str:
    return "sk-" + secrets.token_urlsafe(KEY_BYTES)


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header.",
        )
    return authorization.split(" ", 1)[1].strip()


async def require_api_key(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> ApiKey:
    raw = _extract_bearer(authorization)
    digest = hash_key(raw)
    result = await session.execute(
        select(ApiKey).where(ApiKey.key_hash == digest, ApiKey.active.is_(True))
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key."
        )
    return key


async def require_admin(authorization: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ADMIN_API_KEY is not configured.",
        )
    raw = _extract_bearer(authorization)
    if not secrets.compare_digest(raw, settings.admin_api_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required."
        )
