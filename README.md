# ⚡ LLM Gateway

An open-source, **OpenAI-compatible** gateway that puts OpenAI, Anthropic, and
Google Gemini behind one endpoint — with **cost, latency, and token tracking**,
gateway-issued API keys, streaming, and automatic retries + fallback.

Point any OpenAI SDK at it and switch models across providers by changing one
string. Provider translation is handled by [LiteLLM](https://github.com/BerriAI/litellm);
the gateway, storage, auth, routing policy, and dashboard are the project's own.

## Features

- **One OpenAI-compatible API** — `POST /v1/chat/completions`, `POST /v1/embeddings`, `GET /v1/models`. Existing OpenAI clients work unchanged.
- **Multi-provider** — OpenAI (`gpt-4o`…), Anthropic (`claude-…`), Gemini (`gemini-…`); bare model names are auto-routed.
- **Tools & vision** — function/tool calling and multimodal image inputs pass through to any provider that supports them.
- **Usage tracking** — every call logs provider, model, tokens, **USD cost**, latency, and status.
- **Dashboard** — `/dashboard`: totals, cost-over-time, cost-by-provider, per-model table, recent requests.
- **Streaming (SSE)** — full token streaming with usage captured for cost.
- **Gateway API keys** — issue/revoke client keys (stored as SHA-256 hashes); provider keys stay server-side.
- **Per-key budgets** — optional monthly USD spend cap per key (UTC calendar month); over budget returns 402.
- **Retries & fallback** — transient errors retried; persistent failures fall back to the next model.
- **Swappable storage** — SQLite by default, Postgres via one env var (SQLAlchemy async).

## Quickstart

```bash
git clone <your-repo-url> llm-gateway && cd llm-gateway
cp .env.example .env          # add ADMIN_API_KEY + at least one provider key
uv sync
uv run uvicorn app.main:app --reload
```

Create a gateway key, then call it like OpenAI:

```bash
# 1) Issue a client key (uses ADMIN_API_KEY)
curl -s -X POST localhost:8000/admin/keys \
  -H "Authorization: Bearer $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"my-app"}'
# -> {"api_key":"sk-...", ...}   (shown once)

# 2) Use it — note the model can be OpenAI, Anthropic, or Gemini
curl -s localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-..." \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-3-5-sonnet-20241022","messages":[{"role":"user","content":"hi"}]}'
```

Using the official OpenAI SDK:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="sk-...")  # your gateway key
client.chat.completions.create(
    model="gemini-1.5-flash",                       # routed to Gemini
    messages=[{"role": "user", "content": "hello"}],
)
```

Open the **dashboard** at <http://localhost:8000/dashboard> and paste your `ADMIN_API_KEY`.

## Model naming

| You send | Routed to |
|---|---|
| `gpt-4o`, `o4-mini`, … | OpenAI |
| `claude-3-5-sonnet-20241022`, … | Anthropic (`anthropic/…`) |
| `gemini-1.5-flash`, … | Gemini (`gemini/…`) |
| `anthropic/…`, `vertex_ai/…`, any explicit `provider/model` | passed through to LiteLLM |
| a configured alias (see `MODEL_ALIASES`) | its mapped target |

Per-request fallback: add `"fallback_models": ["gemini-1.5-flash"]` to a chat request;
on failure the gateway retries, then tries each fallback in order.

## Tools & vision

The gateway is a transparent passthrough for OpenAI-style **tool/function calling**
and **multimodal (image) inputs** — point a model that supports them at the
endpoint and they just work:

- Send `tools` / `tool_choice` (and optionally `parallel_tool_calls`); the
  assistant's `tool_calls` come back unchanged. On the next turn, send the
  assistant message with its `tool_calls` plus a `tool`-role message carrying the
  matching `tool_call_id` — both round-trip intact.
- Send `content` as a list of parts (`{"type":"text",...}`,
  `{"type":"image_url",...}`) for vision.

```python
client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": [
        {"type": "text", "text": "What's in this image?"},
        {"type": "image_url", "image_url": {"url": "https://…/cat.png"}},
    ]}],
)
```

Only an allowlist of OpenAI params is forwarded to the provider; client-supplied
credential/endpoint overrides (`api_base`, `api_key`, …) are always dropped.

## Rate limiting

Per-key, sliding-window over the last 60 seconds, enforced on
`/v1/chat/completions`. The limit is a key's own `rate_limit_per_min` (set at
creation) if present, otherwise the global `RATE_LIMIT_PER_MINUTE`; `0` (from
either) means unlimited. Over the limit returns **429** with a `Retry-After`
header.

```bash
# A key capped at 60 requests/minute, regardless of the global default:
curl -s -X POST localhost:8000/admin/keys \
  -H "Authorization: Bearer $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"name":"my-app","rate_limit_per_min":60}'
```

State is in-memory per process, so multi-worker deployments enforce roughly
`limit × workers`; back it with Redis for a strict global limit (upgrade path,
not built).

## Budgets

Per-key **monthly spend cap** in USD, enforced on `/v1/chat/completions`. The cap
is a key's own `monthly_budget_usd` (set at creation) if present, otherwise the
global `MONTHLY_BUDGET_USD`; `0` (from either) means unlimited. Month-to-date
spend is the sum of the key's logged `cost_usd` since the start of the current
**UTC calendar month**; once it reaches the cap the request is rejected with
**402 Payment Required**.

```bash
# A key capped at $25/month, regardless of the global default:
curl -s -X POST localhost:8000/admin/keys \
  -H "Authorization: Bearer $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"name":"my-app","monthly_budget_usd":25}'
```

The cap is checked *before* the upstream call (the cost of the crossing request
isn't known yet), so a key can overshoot by at most one request; for a strict
cap, back spend tracking with Redis (same upgrade path as rate limiting).

## Configuration

All via env / `.env` (see `.env.example`):

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./gateway.db` | SQLite or `postgresql+asyncpg://…` |
| `ADMIN_API_KEY` | — | Guards key management + dashboard |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` | — | Provider creds (only configured ones are enabled) |
| `REQUEST_TIMEOUT_SECONDS` | `120` | Upstream timeout |
| `MAX_RETRIES` | `2` | Retries per model on transient errors |
| `RATE_LIMIT_PER_MINUTE` | `0` | Default per-key req/min on `/v1/chat/completions` (0 = off) |
| `MONTHLY_BUDGET_USD` | `0` | Default per-key monthly USD spend cap (0 = unlimited) |
| `CORS_ORIGINS` | `*` | Comma-separated allowlist (lock down in prod) |
| `MODEL_ALIASES` | `{}` | JSON map of friendly name → litellm model |

## API

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/v1/chat/completions` | gateway key | Chat completion (stream or not) |
| `POST` | `/v1/embeddings` | gateway key | Embeddings (retries + fallback + cost) |
| `GET` | `/v1/models` | gateway key | Models for configured providers |
| `POST` | `/admin/keys` | admin | Create a gateway key (returned once) |
| `GET` | `/admin/keys` | admin | List keys (prefixes only) |
| `DELETE` | `/admin/keys/{id}` | admin | Revoke a key |
| `GET` | `/api/stats?days=N` | admin | Usage aggregates (dashboard data) |
| `GET` | `/health` | none | Liveness + configured providers |

## Docker

```bash
cp .env.example .env   # fill in keys
docker compose up --build           # SQLite (persisted volume)
# Postgres backend:
#   set DATABASE_URL=postgresql+asyncpg://gateway:gateway@db:5432/gateway in .env
docker compose --profile pg up --build
```

## Architecture

```
client ──OpenAI format──> /v1/chat/completions
                              │  auth (gateway key)
                              ▼
                          routing.py  ──retries + fallback──┐
                              │                              │
                              ▼                              ▼
                          engine.py (LiteLLM) ── OpenAI / Anthropic / Gemini
                              │
                              ▼
                       RequestLog (SQLite/Postgres) ──> /api/stats ──> /dashboard
```

## Development

```bash
uv sync
uv run pytest          # 39 tests; litellm calls are stubbed (no network)
uv run ruff check app tests
```

## Roadmap (post-v1)

Prometheus metrics · React dashboard · Redis-backed rate-limit & budget
counters for strict multi-worker limits.

## License

MIT — see [LICENSE](LICENSE).
