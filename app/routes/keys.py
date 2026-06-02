"""Admin endpoints for managing gateway API keys.

Guarded by the admin key. The raw key is shown exactly once, at creation; only
its SHA-256 hash is persisted.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import generate_key, hash_key, require_admin
from app.db import get_session
from app.models import ApiKey

router = APIRouter(prefix="/admin/keys", tags=["admin"], dependencies=[Depends(require_admin)])


class CreateKeyRequest(BaseModel):
    name: str
    monthly_budget_usd: float | None = None
    rate_limit_per_min: int | None = None


class UpdateKeyRequest(BaseModel):
    """Partial update. Only fields present in the request body are changed;
    send an explicit ``null`` to clear a limit (i.e. fall back to the global
    default / unlimited). Omitting a field leaves it untouched."""

    name: str | None = None
    monthly_budget_usd: float | None = None
    rate_limit_per_min: int | None = None
    active: bool | None = None


class KeyInfo(BaseModel):
    id: int
    name: str
    key_prefix: str
    active: bool
    monthly_budget_usd: float | None
    rate_limit_per_min: int | None
    created_at: datetime


class CreatedKey(KeyInfo):
    api_key: str  # full key, shown once


@router.post("", response_model=CreatedKey, status_code=status.HTTP_201_CREATED)
async def create_key(
    body: CreateKeyRequest, session: AsyncSession = Depends(get_session)
) -> CreatedKey:
    raw = generate_key()
    key = ApiKey(
        name=body.name,
        key_hash=hash_key(raw),
        key_prefix=raw[:10],
        monthly_budget_usd=body.monthly_budget_usd,
        rate_limit_per_min=body.rate_limit_per_min,
    )
    session.add(key)
    await session.commit()
    await session.refresh(key)
    return CreatedKey(
        id=key.id,
        name=key.name,
        key_prefix=key.key_prefix,
        active=key.active,
        monthly_budget_usd=key.monthly_budget_usd,
        rate_limit_per_min=key.rate_limit_per_min,
        created_at=key.created_at,
        api_key=raw,
    )


@router.get("", response_model=list[KeyInfo])
async def list_keys(session: AsyncSession = Depends(get_session)) -> list[ApiKey]:
    result = await session.execute(select(ApiKey).order_by(ApiKey.id.desc()))
    return list(result.scalars().all())


@router.patch("/{key_id}", response_model=KeyInfo)
async def update_key(
    key_id: int, body: UpdateKeyRequest, session: AsyncSession = Depends(get_session)
) -> ApiKey:
    key = await session.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="Key not found.")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No fields to update.")
    for field, value in changes.items():
        setattr(key, field, value)
    await session.commit()
    await session.refresh(key)
    return key


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(key_id: int, session: AsyncSession = Depends(get_session)) -> None:
    key = await session.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="Key not found.")
    key.active = False
    await session.commit()
