"""Application settings, loaded from environment / .env.

Provider API keys live here (server-side only) and are never exposed to gateway
clients. Clients authenticate with their own gateway key (see app.auth).
"""
from __future__ import annotations

import json
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Storage. SQLite by default (zero-infra); point at Postgres by setting
    # DATABASE_URL=postgresql+asyncpg://user:pass@host/db
    database_url: str = "sqlite+aiosqlite:///./gateway.db"

    # Upstream provider credentials (optional — a provider is simply unavailable
    # if its key is missing).
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None

    # Provider base URLs (override for Azure/proxies/self-hosted).
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"

    anthropic_version: str = "2023-06-01"

    # Admin key for managing gateway keys and viewing the dashboard/stats.
    admin_api_key: str | None = None

    # Reliability defaults.
    request_timeout_seconds: float = 120.0
    max_retries: int = Field(default=2, ge=0)

    # CORS allowlist for the dashboard / browser clients. Comma-separated.
    cors_origins: str = "*"

    # Optional JSON map of client-facing aliases -> litellm model names, e.g.
    # MODEL_ALIASES='{"fast":"gemini/gemini-1.5-flash","smart":"anthropic/claude-sonnet-4-20250514"}'
    model_aliases: str = "{}"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def model_alias_map(self) -> dict[str, str]:
        try:
            data = json.loads(self.model_aliases)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}


@lru_cache
def get_settings() -> Settings:
    return Settings()
