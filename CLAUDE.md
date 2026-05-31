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
- `app/engine.py` — LiteLLM wrapper: `normalize_model`, `provider_of`, `cost_of`, `acomplete`, `astream`, `aembed`.
- `app/routing.py` — retries + fallback + cost calc + `RequestLog` writes (opens its own session via `SessionLocal`, so streaming logs after the response). `run_complete`/`open_stream`/`run_embed` share the candidate/retry/fallback policy.
- `app/routes/` — `chat`, `embeddings`, `models`, `keys` (admin), `stats` (admin), `health`. `/v1/embeddings` reuses the full `enforce_budget` gate chain; embedding logs (completion_tokens=0) flow into `/api/stats` unchanged.
- `app/auth.py` — gateway keys (SHA-256 hashed) + admin key.

## Conventions / gotchas
- **Model naming:** bare `claude-*`/`gemini-*` are normalized to `anthropic/…`/`gemini/…` for LiteLLM; `gpt-*`/`o*` pass through. Explicit `provider/model` and `MODEL_ALIASES` honored.
- **Streaming fallback:** `open_stream()` resolves the provider (incl. fallback) and pulls the first chunk *before* the `StreamingResponse` is built, so total failures raise cleanly instead of mid-stream. Don't move that logic into the SSE generator.
- **Rate limiting** (`app/ratelimit.py`): sliding-window per gateway key on `/v1/chat/completions`, enforced via the `enforce_rate_limit` dependency. State is **in-memory per-process** (`app.state.rate_limiter`), so multi-worker deploys get ≈ limit × workers — back with Redis for a strict global limit. Limit = key's `rate_limit_per_min` if set, else `RATE_LIMIT_PER_MINUTE`; `<= 0` means unlimited. 429s carry a `Retry-After` header.
- **Budgets** (`app/budget.py`): per-key monthly USD cap on `/v1/chat/completions`, enforced via `enforce_budget`, which **depends on `enforce_rate_limit`** — so the chat route's single dependency runs the whole chain `require_api_key → enforce_rate_limit → enforce_budget`. Spend = `SUM(request_logs.cost_usd)` for the key since the start of the current **UTC calendar month** (`_month_start()` is a tz-aware boundary that compares correctly on both SQLite and Postgres). Cap = key's `monthly_budget_usd` if set, else `MONTHLY_BUDGET_USD`; `<= 0` means unlimited. Checked **before** the upstream call (cost of the crossing request isn't known yet → a key can overshoot by one request). Over budget → **HTTP 402**.
- **Post-release columns** (e.g. `api_keys.rate_limit_per_min`): `create_all` only makes missing *tables*, so `init_db` runs a best-effort `ALTER TABLE … ADD COLUMN` from `_ADDED_COLUMNS` in `app/db.py`. Add new columns there too until the schema moves to Alembic.
- **`app/db.py` binds the engine to `DATABASE_URL` at import time** — tests must set the env var before importing the app (see `tests/conftest.py`).
- **Tests:** run with `PYTHONPATH= uv run pytest` — a system ROS pytest plugin leaks in via `PYTHONPATH` otherwise. litellm calls are stubbed; no network.
- Provider keys live only in `.env` server-side; never returned to clients.
