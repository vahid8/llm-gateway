"""OpenAI-compatible request/response schemas.

This is the gateway's canonical wire format. Provider adapters translate these
to/from each provider's native API, so existing OpenAI SDKs work unchanged.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    stop: str | list[str] | None = None
    n: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    user: str | None = None

    # Gateway extension: ordered fallback models tried on upstream failure.
    fallback_models: list[str] | None = Field(default=None)

    model_config = {"extra": "allow"}  # forward unknown params to the provider


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str | None = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: Usage

    # Gateway extension: visibility into routing/cost without a DB round-trip.
    gateway: dict[str, Any] | None = None


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    owned_by: str


class ModelList(BaseModel):
    object: str = "list"
    data: list[ModelInfo]
