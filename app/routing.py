"""Request orchestration: retries, fallback chain, cost calc, and usage logging.

Policy: for each candidate model we retry on transient errors up to
``max_retries``; on persistent failure we fall back to the next candidate.
Candidates = the primary model plus the request's ``fallback_models``, or — when
the client sends none — the server-side ``FALLBACK_CHAINS`` default for that
model. Streaming commits to a provider only once its first chunk arrives, so
fallback still works pre-stream.
"""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

from app.config import Settings
from app.db import SessionLocal
from app.engine import (
    acomplete,
    aembed,
    astream,
    cost_of,
    is_retryable,
    normalize_model,
    provider_of,
)
from app.models import RequestLog
from app.schemas import ChatCompletionRequest, EmbeddingRequest


class UpstreamError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _status_of(exc: Exception) -> int:
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else 502


def _candidates(
    request: ChatCompletionRequest | EmbeddingRequest,
    settings: Settings,
    aliases: dict[str, str],
    *,
    wildcard: bool = True,
) -> list[str]:
    """Primary model + fallbacks, deduped on their normalized form.

    Client-supplied ``fallback_models`` win outright; otherwise the server-side
    ``FALLBACK_CHAINS`` entry for the normalized primary applies — exact model
    key first, then the ``provider/*`` wildcard. Embeddings opt out of the
    wildcard (``wildcard=False``) so a provider-wide chat chain never routes an
    embedding request to a chat model.
    """
    primary = normalize_model(request.model, aliases)
    fallbacks = list(request.fallback_models or [])
    if not fallbacks:
        chains = settings.fallback_chain_map
        fallbacks = list(chains.get(primary) or [])
        if not fallbacks and wildcard:
            fallbacks = list(chains.get(f"{provider_of(primary)}/*") or [])
    models = [request.model]
    seen = {primary}
    for m in fallbacks:
        normalized = normalize_model(m, aliases)
        if normalized not in seen:
            seen.add(normalized)
            models.append(m)
    return models


async def _log(**fields) -> None:
    async with SessionLocal() as session:
        session.add(RequestLog(**fields))
        await session.commit()


def _usage_fields(usage: dict | None) -> tuple[int, int, int]:
    usage = usage or {}
    pt = usage.get("prompt_tokens") or 0
    ct = usage.get("completion_tokens") or 0
    tt = usage.get("total_tokens") or (pt + ct)
    return pt, ct, tt


# --- Non-streaming -------------------------------------------------------
async def run_complete(
    request: ChatCompletionRequest,
    settings: Settings,
    aliases: dict[str, str],
    api_key_id: int,
) -> dict:
    attempts = 0
    last_exc: Exception | None = None

    for raw_model in _candidates(request, settings, aliases):
        model = normalize_model(raw_model, aliases)
        for attempt in range(settings.max_retries + 1):
            attempts += 1
            start = time.perf_counter()
            try:
                resp = await acomplete(request, model, settings.request_timeout_seconds)
            except Exception as exc:  # noqa: BLE001 — classified below
                last_exc = exc
                if is_retryable(exc) and attempt < settings.max_retries:
                    continue
                break  # try next candidate model

            latency_ms = int((time.perf_counter() - start) * 1000)
            pt, ct, tt = _usage_fields(resp.get("usage"))
            provider = provider_of(model)
            cost = cost_of(model, pt, ct)
            await _log(
                api_key_id=api_key_id,
                provider=provider,
                model=model,
                requested_model=request.model,
                prompt_tokens=pt,
                completion_tokens=ct,
                total_tokens=tt,
                cost_usd=cost,
                latency_ms=latency_ms,
                streamed=False,
                status="ok",
                attempts=attempts,
            )
            resp["gateway"] = {
                "provider": provider,
                "resolved_model": model,
                "cost_usd": cost,
                "latency_ms": latency_ms,
                "attempts": attempts,
            }
            return resp

    message = str(last_exc) if last_exc else "all candidates failed"
    await _log(
        api_key_id=api_key_id,
        provider=provider_of(normalize_model(request.model, aliases)),
        model=normalize_model(request.model, aliases),
        requested_model=request.model,
        status="error",
        attempts=attempts,
        error=message[:2000],
    )
    raise UpstreamError(message, _status_of(last_exc) if last_exc else 502)


# --- Streaming -----------------------------------------------------------
class _StreamHandle:
    def __init__(self, gen, first, model, attempts, start):
        self.gen = gen
        self.first = first
        self.model = model
        self.attempts = attempts
        self.start = start


async def open_stream(
    request: ChatCompletionRequest,
    settings: Settings,
    aliases: dict[str, str],
    api_key_id: int,
) -> _StreamHandle:
    attempts = 0
    last_exc: Exception | None = None

    for raw_model in _candidates(request, settings, aliases):
        model = normalize_model(raw_model, aliases)
        for attempt in range(settings.max_retries + 1):
            attempts += 1
            start = time.perf_counter()
            gen = astream(request, model, settings.request_timeout_seconds)
            try:
                first = await gen.__anext__()
            except StopAsyncIteration:
                return _StreamHandle(None, None, model, attempts, start)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                await gen.aclose()
                if is_retryable(exc) and attempt < settings.max_retries:
                    continue
                break
            return _StreamHandle(gen, first, model, attempts, start)

    message = str(last_exc) if last_exc else "all candidates failed"
    await _log(
        api_key_id=api_key_id,
        provider=provider_of(normalize_model(request.model, aliases)),
        model=normalize_model(request.model, aliases),
        requested_model=request.model,
        status="error",
        attempts=attempts,
        error=message[:2000],
    )
    raise UpstreamError(message, _status_of(last_exc) if last_exc else 502)


async def stream_body(
    handle: _StreamHandle,
    request: ChatCompletionRequest,
    api_key_id: int,
) -> AsyncIterator[str]:
    """Yields SSE lines for an already-resolved stream and logs usage at the end.
    Call ``open_stream`` first so total failures raise before the response starts."""
    pt = ct = tt = 0

    def capture(chunk: dict) -> None:
        nonlocal pt, ct, tt
        if chunk.get("usage"):
            pt, ct, tt = _usage_fields(chunk["usage"])

    if handle.first is not None:
        capture(handle.first)
        yield f"data: {json.dumps(handle.first)}\n\n"
    if handle.gen is not None:
        async for chunk in handle.gen:
            capture(chunk)
            yield f"data: {json.dumps(chunk)}\n\n"
    yield "data: [DONE]\n\n"

    latency_ms = int((time.perf_counter() - handle.start) * 1000)
    await _log(
        api_key_id=api_key_id,
        provider=provider_of(handle.model),
        model=handle.model,
        requested_model=request.model,
        prompt_tokens=pt,
        completion_tokens=ct,
        total_tokens=tt or (pt + ct),
        cost_usd=cost_of(handle.model, pt, ct),
        latency_ms=latency_ms,
        streamed=True,
        status="ok",
        attempts=handle.attempts,
    )


# --- Embeddings ----------------------------------------------------------
async def run_embed(
    request: EmbeddingRequest,
    settings: Settings,
    aliases: dict[str, str],
    api_key_id: int,
) -> dict:
    """Embeddings with the same retry + fallback + logging policy as completions.

    Embedding usage reports only prompt tokens (no completion), so completion
    cost is zero; ``cost_of`` handles that. Logged to ``RequestLog`` like any
    other request, so it shows up in /api/stats unchanged.
    """
    attempts = 0
    last_exc: Exception | None = None

    for raw_model in _candidates(request, settings, aliases, wildcard=False):
        model = normalize_model(raw_model, aliases)
        for attempt in range(settings.max_retries + 1):
            attempts += 1
            start = time.perf_counter()
            try:
                resp = await aembed(request, model, settings.request_timeout_seconds)
            except Exception as exc:  # noqa: BLE001 — classified below
                last_exc = exc
                if is_retryable(exc) and attempt < settings.max_retries:
                    continue
                break  # try next candidate model

            latency_ms = int((time.perf_counter() - start) * 1000)
            pt, ct, tt = _usage_fields(resp.get("usage"))
            provider = provider_of(model)
            cost = cost_of(model, pt, ct)
            await _log(
                api_key_id=api_key_id,
                provider=provider,
                model=model,
                requested_model=request.model,
                prompt_tokens=pt,
                completion_tokens=ct,
                total_tokens=tt,
                cost_usd=cost,
                latency_ms=latency_ms,
                streamed=False,
                status="ok",
                attempts=attempts,
            )
            resp["gateway"] = {
                "provider": provider,
                "resolved_model": model,
                "cost_usd": cost,
                "latency_ms": latency_ms,
                "attempts": attempts,
            }
            return resp

    message = str(last_exc) if last_exc else "all candidates failed"
    await _log(
        api_key_id=api_key_id,
        provider=provider_of(normalize_model(request.model, aliases)),
        model=normalize_model(request.model, aliases),
        requested_model=request.model,
        status="error",
        attempts=attempts,
        error=message[:2000],
    )
    raise UpstreamError(message, _status_of(last_exc) if last_exc else 502)
