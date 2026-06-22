"""GET /v1/models — advertise models for providers that have a key configured."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import require_api_key
from app.config import get_settings
from app.models import ApiKey
from app.schemas import ModelInfo, ModelList

router = APIRouter(tags=["models"])

# Curated default catalog per provider. Clients may also pass any litellm model.
_CATALOG: dict[str, list[str]] = {
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o4-mini"],
    "anthropic": [
        "claude-sonnet-4-20250514",
        "claude-opus-4-20250514",
        "claude-3-5-sonnet-20241022",
        "claude-3-5-haiku-20241022",
    ],
    "gemini": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-1.5-pro", "gemini-1.5-flash"],
    # Doubleword model ids carry the provider prefix so clients call them as-is;
    # any other Doubleword-hosted id (e.g. a GLM build) also works via the prefix.
    "doubleword": ["doubleword/Qwen/Qwen3-VL-235B-A22B-Instruct-FP8"],
}


@router.get("/v1/models", response_model=ModelList)
async def list_models(_: ApiKey = Depends(require_api_key)) -> ModelList:
    settings = get_settings()
    configured = {
        "openai": bool(settings.openai_api_key),
        "anthropic": bool(settings.anthropic_api_key),
        "gemini": bool(settings.gemini_api_key),
        "doubleword": bool(settings.doubleword_api_key),
    }
    data: list[ModelInfo] = []
    for provider, models in _CATALOG.items():
        if not configured.get(provider):
            continue
        data.extend(ModelInfo(id=m, owned_by=provider) for m in models)
    for alias in settings.model_alias_map:
        data.append(ModelInfo(id=alias, owned_by="alias"))
    return ModelList(data=data)
