"""OpenAI-compatible chat completions endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import get_settings
from app.models import ApiKey
from app.ratelimit import enforce_rate_limit
from app.routing import UpstreamError, open_stream, run_complete, stream_body
from app.schemas import ChatCompletionRequest

router = APIRouter(tags=["chat"])


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


@router.post("/v1/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    api_key: ApiKey = Depends(enforce_rate_limit),
):
    settings = get_settings()
    aliases = settings.model_alias_map

    if request.stream:
        try:
            handle = await open_stream(request, settings, aliases, api_key.id)
        except UpstreamError as exc:
            return _error_response(exc)
        return StreamingResponse(
            stream_body(handle, request, api_key.id),
            media_type="text/event-stream",
        )

    try:
        result = await run_complete(request, settings, aliases, api_key.id)
    except UpstreamError as exc:
        return _error_response(exc)
    return JSONResponse(content=result)
