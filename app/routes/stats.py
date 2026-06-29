"""Usage analytics for the dashboard. Admin-guarded JSON aggregates."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.db import get_session
from app.models import ApiKey, RequestLog

router = APIRouter(prefix="/api", tags=["stats"], dependencies=[Depends(require_admin)])


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round((pct / 100) * (len(ordered) - 1))))
    return ordered[k]


@router.get("/stats")
async def stats(
    days: int = Query(default=30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    base = select(RequestLog).where(RequestLog.created_at >= since)

    # Totals
    totals_row = (
        await session.execute(
            select(
                func.count(RequestLog.id),
                func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
                func.coalesce(func.sum(RequestLog.total_tokens), 0),
                func.coalesce(func.avg(RequestLog.latency_ms), 0.0),
                func.coalesce(
                    func.sum(case((RequestLog.status == "error", 1), else_=0)), 0
                ),
            ).where(RequestLog.created_at >= since)
        )
    ).one()
    total_requests, total_cost, total_tokens, avg_latency, errors = totals_row

    # Latency percentiles (ok requests only)
    latencies = list(
        (
            await session.execute(
                select(RequestLog.latency_ms).where(
                    RequestLog.created_at >= since, RequestLog.status == "ok"
                )
            )
        )
        .scalars()
        .all()
    )

    async def grouped(column):
        rows = (
            await session.execute(
                select(
                    column,
                    func.count(RequestLog.id),
                    func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
                    func.coalesce(func.sum(RequestLog.total_tokens), 0),
                )
                .where(RequestLog.created_at >= since)
                .group_by(column)
                .order_by(func.sum(RequestLog.cost_usd).desc())
            )
        ).all()
        return [
            {"key": k, "requests": c, "cost_usd": round(cost, 6), "tokens": tok}
            for k, c, cost, tok in rows
        ]

    by_provider = await grouped(RequestLog.provider)
    by_model = await grouped(RequestLog.model)

    # Per-key spend — which gateway key (system/user) consumed the most.
    # Outer join so logs with a deleted/unknown api_key_id still surface.
    key_rows = (
        await session.execute(
            select(
                RequestLog.api_key_id,
                ApiKey.name,
                func.count(RequestLog.id),
                func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
                func.coalesce(func.sum(RequestLog.total_tokens), 0),
            )
            .join(ApiKey, ApiKey.id == RequestLog.api_key_id, isouter=True)
            .where(RequestLog.created_at >= since)
            .group_by(RequestLog.api_key_id, ApiKey.name)
            .order_by(func.sum(RequestLog.cost_usd).desc())
        )
    ).all()
    by_key = [
        {
            "key_id": kid,
            "name": name or "(unknown)",
            "requests": c,
            "cost_usd": round(cost, 6),
            "tokens": tok,
        }
        for kid, name, c, cost, tok in key_rows
    ]

    # Daily timeseries
    day = func.date(RequestLog.created_at)
    ts_rows = (
        await session.execute(
            select(
                day,
                func.count(RequestLog.id),
                func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
            )
            .where(RequestLog.created_at >= since)
            .group_by(day)
            .order_by(day)
        )
    ).all()
    timeseries = [
        {"date": str(d), "requests": c, "cost_usd": round(cost, 6)}
        for d, c, cost in ts_rows
    ]

    # Recent requests
    recent_rows = (
        await session.execute(base.order_by(RequestLog.id.desc()).limit(50))
    ).scalars().all()
    recent = [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat(),
            "provider": r.provider,
            "model": r.model,
            "tokens": r.total_tokens,
            "cost_usd": round(r.cost_usd, 6),
            "latency_ms": r.latency_ms,
            "streamed": r.streamed,
            "status": r.status,
        }
        for r in recent_rows
    ]

    return {
        "window_days": days,
        "totals": {
            "requests": total_requests,
            "cost_usd": round(float(total_cost), 6),
            "tokens": int(total_tokens),
            "errors": int(errors),
            "avg_latency_ms": round(float(avg_latency), 1),
            "p50_latency_ms": _percentile(latencies, 50),
            "p95_latency_ms": _percentile(latencies, 95),
        },
        "by_provider": by_provider,
        "by_model": by_model,
        "by_key": by_key,
        "timeseries": timeseries,
        "recent": recent,
    }
