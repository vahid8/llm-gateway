"""Per-key monthly spend budgets.

A gateway key may carry a ``monthly_budget_usd`` cap; if it doesn't, the global
``MONTHLY_BUDGET_USD`` default applies. Before each chat completion we sum the
key's ``cost_usd`` logged since the start of the current UTC calendar month and
reject with HTTP 402 once that month-to-date spend reaches the cap.

The cap is checked *before* the upstream call, so the cost of the request that
crosses the line is not yet known — a key can overshoot slightly on that one
request. Enforcement is layered on top of the rate-limit dependency, so the full
gate chain for a chat completion is: authenticate → rate limit → budget.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import ApiKey, RequestLog
from app.ratelimit import enforce_rate_limit


def _month_start() -> datetime:
    """Start of the current UTC calendar month as a tz-aware datetime.

    Matches the tz-aware UTC timestamps written by the ``RequestLog.created_at``
    default, so the ``>=`` comparison is a correct lower bound on both SQLite
    (ISO-string storage) and Postgres.
    """
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def month_to_date_spend(key_id: int) -> float:
    """Total ``cost_usd`` logged for ``key_id`` since the start of the UTC month."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(func.coalesce(func.sum(RequestLog.cost_usd), 0.0)).where(
                RequestLog.api_key_id == key_id,
                RequestLog.created_at >= _month_start(),
            )
        )
        return float(result.scalar_one())


def effective_budget(api_key: ApiKey, default_budget: float) -> float:
    """Per-key cap wins; otherwise fall back to the global default.

    A value ``<= 0`` from either source means unlimited.
    """
    if api_key.monthly_budget_usd is not None:
        return api_key.monthly_budget_usd
    return default_budget


async def enforce_budget(
    api_key: ApiKey = Depends(enforce_rate_limit),
) -> ApiKey:
    """Dependency: authenticate + rate limit, then enforce the monthly budget.

    Returns the authenticated ``ApiKey`` so routes can depend on this in place of
    ``enforce_rate_limit`` / ``require_api_key`` directly.
    """
    budget = effective_budget(api_key, get_settings().monthly_budget_usd)
    if budget <= 0:
        return api_key
    spent = await month_to_date_spend(api_key.id)
    if spent >= budget:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Monthly budget exceeded: ${spent:.4f} of ${budget:.2f} used "
                "this month."
            ),
        )
    return api_key
