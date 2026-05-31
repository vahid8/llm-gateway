"""In-process per-key request rate limiting (sliding-window log).

Each gateway API key gets a rolling ``WINDOW_SECONDS`` window; once it has made
``limit`` requests inside that window further requests are rejected with HTTP 429
until the oldest request ages out.

State lives in memory, so it is *per-process*: with multiple Uvicorn workers each
worker enforces the limit independently (effective limit ≈ limit × workers). That
is an acceptable approximation for the default single-worker / SQLite deployment;
for a strict global limit across workers, back this with Redis (documented
upgrade path, not built).

The limiter is created per-app and stored on ``app.state.rate_limiter`` so tests
(and multiple ``create_app`` calls) start with clean state.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request, status

from app.auth import require_api_key
from app.config import get_settings
from app.models import ApiKey

WINDOW_SECONDS = 60.0


class RateLimiter:
    """Sliding-window request counter keyed by gateway API key id.

    Single-event-loop safe: ``check`` does not ``await`` between reading and
    mutating the per-key deque, so no lock is needed under asyncio.
    """

    def __init__(self) -> None:
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def check(
        self, key_id: int, limit: int, now: float | None = None
    ) -> tuple[bool, int]:
        """Record a request attempt for ``key_id``.

        Returns ``(allowed, retry_after_seconds)``. ``limit <= 0`` means
        unlimited. ``retry_after`` is 0 when the request is allowed.
        """
        if limit <= 0:
            return True, 0
        now = time.monotonic() if now is None else now
        window_start = now - WINDOW_SECONDS
        hits = self._hits[key_id]
        while hits and hits[0] <= window_start:
            hits.popleft()
        if len(hits) >= limit:
            retry_after = int(WINDOW_SECONDS - (now - hits[0])) + 1
            return False, max(retry_after, 1)
        hits.append(now)
        return True, 0


def effective_limit(api_key: ApiKey, default_limit: int) -> int:
    """Per-key override wins; otherwise fall back to the global default."""
    if api_key.rate_limit_per_min is not None:
        return api_key.rate_limit_per_min
    return default_limit


async def enforce_rate_limit(
    request: Request,
    api_key: ApiKey = Depends(require_api_key),
) -> ApiKey:
    """Dependency: authenticate, then apply the per-key rate limit.

    Returns the authenticated ``ApiKey`` so routes can depend on this in place
    of ``require_api_key`` directly.
    """
    limiter: RateLimiter = request.app.state.rate_limiter
    limit = effective_limit(api_key, get_settings().rate_limit_per_minute)
    allowed, retry_after = limiter.check(api_key.id, limit)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded ({limit}/min).",
            headers={"Retry-After": str(retry_after)},
        )
    return api_key
