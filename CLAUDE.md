# llm-gateway

**Purpose:** Open-source, OpenAI-compatible LLM gateway across OpenAI, Anthropic,
and Gemini, with cost/latency/token tracking, gateway-issued API keys, streaming,
and retries + fallback. Built for OSS release and personal multi-provider use.

## Stack
- **Backend:** FastAPI (`uv`), async SQLAlchemy 2.0.
- **Provider engine:** LiteLLM SDK — the *only* module that imports `litellm` is `app/engine.py`. Everything else speaks the OpenAI-compatible schema in `app/schemas.py`.
- **Storage:** SQLite default; Postgres via `DATABASE_URL` (`postgresql+asyncpg://…`). Tables auto-create on startup (swap to Alembic when the schema evolves in prod).
- **Dashboard:** single static page (`app/static/`, Chart.js via CDN) served at `/dashboard`. No frontend build. React is the documented upgrade path, not built yet.

## Layout
- `app/engine.py` — LiteLLM wrapper: `normalize_model`, `provider_of`, `cost_of`, `acomplete`, `astream`.
- `app/routing.py` — retries + fallback + cost calc + `RequestLog` writes (opens its own session via `SessionLocal`, so streaming logs after the response).
- `app/routes/` — `chat`, `models`, `keys` (admin), `stats` (admin), `health`.
- `app/auth.py` — gateway keys (SHA-256 hashed) + admin key.

## Conventions / gotchas
- **Model naming:** bare `claude-*`/`gemini-*` are normalized to `anthropic/…`/`gemini/…` for LiteLLM; `gpt-*`/`o*` pass through. Explicit `provider/model` and `MODEL_ALIASES` honored.
- **Streaming fallback:** `open_stream()` resolves the provider (incl. fallback) and pulls the first chunk *before* the `StreamingResponse` is built, so total failures raise cleanly instead of mid-stream. Don't move that logic into the SSE generator.
- **`app/db.py` binds the engine to `DATABASE_URL` at import time** — tests must set the env var before importing the app (see `tests/conftest.py`).
- **Tests:** run with `PYTHONPATH= uv run pytest` — a system ROS pytest plugin leaks in via `PYTHONPATH` otherwise. litellm calls are stubbed; no network.
- Provider keys live only in `.env` server-side; never returned to clients.
