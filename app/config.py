"""Application settings, loaded from environment / .env.

Provider API keys live here (server-side only) and are never exposed to gateway
clients. Clients authenticate with their own gateway key (see app.auth).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
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
    # Doubleword: high-throughput, OpenAI-compatible inference cloud for open
    # models (Qwen, GLM, …). Not a litellm-native provider — routed via the
    # openai adapter against doubleword_base_url (see app.engine).
    doubleword_api_key: str | None = None

    # Docker-secrets / `*_FILE` convention: when one of these points at a file
    # (e.g. /run/secrets/doubleword_key, or a path under ~/.secrets for local
    # dev), the key is read from that file and OVERRIDES the inline `*_API_KEY`
    # env var — so secrets are mounted as files and never appear in the
    # container environment or `docker inspect`.
    openai_api_key_file: str | None = None
    anthropic_api_key_file: str | None = None
    gemini_api_key_file: str | None = None
    doubleword_api_key_file: str | None = None

    # Provider base URLs (override for Azure/proxies/self-hosted).
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    doubleword_base_url: str = "https://api.doubleword.ai/v1"

    anthropic_version: str = "2023-06-01"

    # Admin key for managing gateway keys and viewing the dashboard/stats.
    admin_api_key: str | None = None

    # Reliability defaults.
    request_timeout_seconds: float = 120.0
    max_retries: int = Field(default=2, ge=0)

    # Default per-key request rate limit (requests/minute on /v1/chat/completions).
    # 0 disables rate limiting globally; a key's own rate_limit_per_min overrides this.
    rate_limit_per_minute: int = Field(default=0, ge=0)

    # Default per-key monthly spend cap in USD (calendar month, UTC). 0 disables
    # budget enforcement globally; a key's own monthly_budget_usd overrides this.
    monthly_budget_usd: float = Field(default=0.0, ge=0)

    # CORS allowlist for the dashboard / browser clients. Comma-separated.
    cors_origins: str = "*"

    # Optional JSON map of client-facing aliases -> litellm model names, e.g.
    # MODEL_ALIASES='{"fast":"gemini/gemini-1.5-flash","smart":"anthropic/claude-sonnet-4-20250514"}'
    model_aliases: str = "{}"

    @model_validator(mode="after")
    def _load_key_files(self) -> Settings:
        """Read any `*_api_key_file` into the matching `*_api_key`.

        A file path wins over an inline key because the file (a Docker secret at
        /run/secrets/… in prod, or ~/.secrets/… in local dev) is the source of
        truth; the inline env var is only the fallback. Missing/empty files are
        ignored so a provider stays simply unavailable rather than erroring.
        """
        for name in ("openai", "anthropic", "gemini", "doubleword"):
            path = getattr(self, f"{name}_api_key_file")
            if not path:
                continue
            p = Path(path).expanduser()
            if p.is_file():
                value = p.read_text(encoding="utf-8").strip()
                if value:
                    setattr(self, f"{name}_api_key", value)
        return self

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
