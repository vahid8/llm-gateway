"""OpenAI-compatible embeddings endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.budget import enforce_budget
from app.config import get_settings
from app.models import ApiKey
from app.routing import UpstreamError, run_embed
from app.schemas import EmbeddingRequest

router = APIRouter(tags=["embeddings"])


def _error_response(exc: UpstreamError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.message,
                "type": "upstream_error",
                "code": exc.status_code,
            }
        },
    )


@router.post("/v1/embeddings")
async def embeddings(
    request: EmbeddingRequest,
    api_key: ApiKey = Depends(enforce_budget),
):
    settings = get_settings()
    aliases = settings.model_alias_map
    try:
        result = await run_embed(request, settings, aliases, api_key.id)
    except UpstreamError as exc:
        return _error_response(exc)
    return JSONResponse(content=result)
