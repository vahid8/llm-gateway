"""LiteLLM-backed provider engine.

The gateway delegates all provider translation, streaming, and pricing to the
`litellm` SDK. This module is the only place that imports litellm; the rest of
the app speaks our OpenAI-compatible schema.

Model naming: litellm wants provider-prefixed names for non-OpenAI providers
(``anthropic/claude-...``, ``gemini/gemini-...``). We normalize bare ``claude-*``
and ``gemini-*`` so OpenAI-style clients work unchanged, and honor any explicit
``provider/model`` string or configured alias as-is.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator

import litellm
from litellm.exceptions import (
    APIConnectionError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

from app.config import Settings
from app.schemas import ChatCompletionRequest, EmbeddingRequest

litellm.drop_params = True  # silently drop params a given provider doesn't support

_RETRYABLE = (Timeout, RateLimitError, ServiceUnavailableError, InternalServerError, APIConnectionError)

# Allowlist of OpenAI chat params we forward to litellm. Anything else (incl.
# client-injected credential/endpoint overrides like api_base, api_key,
# base_url, custom_llm_provider) is dropped — forwarding those would let an
# authenticated client redirect calls and exfiltrate our server-side provider
# keys, or bill against another account. model/messages/stream/timeout are set
# explicitly by the gateway.
_FORWARD_PARAMS = {
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "n",
    "presence_penalty",
    "frequency_penalty",
    "logit_bias",
    "logprobs",
    "top_logprobs",
    "seed",
    "response_format",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "user",
}

# Same idea for /v1/embeddings: only these client params reach litellm. ``input``
# and ``model`` are set explicitly; credential/endpoint overrides never forward.
_EMBED_FORWARD_PARAMS = {
    "encoding_format",
    "dimensions",
    "user",
}


# Doubleword is an OpenAI-compatible endpoint that litellm has no native
# provider for. We route ``doubleword/<id>`` through litellm's openai adapter
# with the endpoint + key injected per call (never via env, so it can't collide
# with a real OPENAI_API_KEY). Populated from Settings in ``configure_keys``.
DOUBLEWORD_PREFIX = "doubleword/"
_doubleword: dict[str, str] = {}


def configure_keys(settings: Settings) -> None:
    """Expose provider keys to litellm via the env vars it reads."""
    if settings.openai_api_key:
        os.environ.setdefault("OPENAI_API_KEY", settings.openai_api_key)
    if settings.anthropic_api_key:
        os.environ.setdefault("ANTHROPIC_API_KEY", settings.anthropic_api_key)
    if settings.gemini_api_key:
        os.environ.setdefault("GEMINI_API_KEY", settings.gemini_api_key)
    _doubleword.clear()
    if settings.doubleword_api_key:
        _doubleword["api_base"] = settings.doubleword_base_url
        _doubleword["api_key"] = settings.doubleword_api_key


def normalize_model(model: str, aliases: dict[str, str]) -> str:
    if model in aliases:
        return aliases[model]
    if "/" in model:
        return model
    low = model.lower()
    if low.startswith("claude"):
        return f"anthropic/{model}"
    if low.startswith("gemini"):
        return f"gemini/{model}"
    return model  # gpt-*, o*, and anything else default to openai/native


def _resolve_litellm_model(model: str) -> tuple[str, dict]:
    """Map a gateway model name to ``(litellm_model, extra_kwargs)``.

    ``doubleword/<id>`` is sent through litellm's openai adapter against
    Doubleword's OpenAI-compatible endpoint, with the server-side api_base/api_key
    injected here (never from the client). ``<id>`` may itself contain slashes
    (e.g. ``Qwen/Qwen3-VL-235B-A22B-Instruct-FP8``). Anything else is unchanged.
    """
    if model.startswith(DOUBLEWORD_PREFIX):
        rest = model[len(DOUBLEWORD_PREFIX):]
        return f"openai/{rest}", dict(_doubleword)
    return model, {}


def provider_of(normalized_model: str) -> str:
    if normalized_model.startswith(DOUBLEWORD_PREFIX):
        return "doubleword"
    try:
        return litellm.get_llm_provider(normalized_model)[1]
    except Exception:
        return "unknown"


def cost_of(normalized_model: str, prompt_tokens: int, completion_tokens: int) -> float:
    try:
        prompt_cost, completion_cost = litellm.cost_per_token(
            model=normalized_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return round(prompt_cost + completion_cost, 8)
    except Exception:
        return 0.0


def is_retryable(exc: Exception) -> bool:
    return isinstance(exc, _RETRYABLE)


def _litellm_kwargs(request: ChatCompletionRequest, model: str, timeout: float) -> dict:
    raw = request.model_dump(exclude_none=True)
    kwargs = {k: raw[k] for k in _FORWARD_PARAMS if k in raw}
    litellm_model, extra = _resolve_litellm_model(model)
    # Gateway-controlled fields — never taken from the client payload. ``extra``
    # carries server-side endpoint/credentials for OpenAI-compatible providers and
    # is applied after the client params so a client can't override it.
    kwargs.update(extra)
    kwargs["model"] = litellm_model
    kwargs["messages"] = raw["messages"]
    kwargs["timeout"] = timeout
    return kwargs


async def acomplete(
    request: ChatCompletionRequest, model: str, timeout: float
) -> dict:
    """Non-streaming completion. Returns an OpenAI-shaped response dict."""
    kwargs = _litellm_kwargs(request, model, timeout)
    kwargs["stream"] = False
    resp = await litellm.acompletion(**kwargs)
    return resp.model_dump()


async def astream(
    request: ChatCompletionRequest, model: str, timeout: float
) -> AsyncIterator[dict]:
    """Streaming completion. Yields OpenAI-shaped chunk dicts incl. a final
    usage chunk (litellm emits it via stream_options.include_usage)."""
    kwargs = _litellm_kwargs(request, model, timeout)
    kwargs["stream"] = True
    kwargs["stream_options"] = {"include_usage": True}
    response = await litellm.acompletion(**kwargs)
    async for chunk in response:
        yield chunk.model_dump()


def _embedding_kwargs(request: EmbeddingRequest, model: str, timeout: float) -> dict:
    raw = request.model_dump(exclude_none=True)
    kwargs = {k: raw[k] for k in _EMBED_FORWARD_PARAMS if k in raw}
    litellm_model, extra = _resolve_litellm_model(model)
    # Gateway-controlled fields — never taken from the client payload. ``extra``
    # carries server-side endpoint/credentials and is applied after client params.
    kwargs.update(extra)
    kwargs["model"] = litellm_model
    kwargs["input"] = raw["input"]
    kwargs["timeout"] = timeout
    return kwargs


async def aembed(request: EmbeddingRequest, model: str, timeout: float) -> dict:
    """Embeddings. Returns an OpenAI-shaped response dict."""
    kwargs = _embedding_kwargs(request, model, timeout)
    resp = await litellm.aembedding(**kwargs)
    return resp.model_dump()
