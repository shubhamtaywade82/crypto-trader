# Dashboard (FastAPI read-model API + SolidJS UI)

A **read-only, realtime** dashboard for the bot. It reads the Postgres projection
(`active_positions` / `orders` / `fills`) and relays bot events over Server-Sent
Events for live updates — **no polling**. It cannot place, cancel, or modify
trades; there are no write endpoints.

## Architecture

```
engine_live (writer)                       FastAPI service (reader)        SolidJS app
  ├─ wallet event_hook ─▶ Postgres projection ◀── psycopg2 reads ──────────┐
  └─ RedisEventPublisher ─▶ Redis pub/sub "events:ui" ─▶ /api/stream (SSE) ─┴─▶ EventSource
```

- **Realtime:** the bot publishes every domain event to a Redis pub/sub channel
  (`RedisEventPublisher`, hooked onto the wallet event hook when `UI_EVENTS_ENABLED`
  and `REDIS_URL` are set). The API's `/api/stream` SSE endpoint subscribes and
  pushes each event to the browser; the UI refetches the affected views on arrival
  (coalesced to ≤1 refetch / 300ms).
- **Read model:** REST endpoints read the projection tables directly (separate
  process, read-only). The trading core is untouched.

## Endpoints

All `/api/*` endpoints require a bearer token when `API_DASHBOARD_TOKEN` is set (see
**Config** below). Pass `Authorization: Bearer <token>` in the request header. If the
env var is empty, auth is skipped (local dev mode).

| Endpoint | Description |
|---|---|
| `GET /` | The built SolidJS app (or a JSON placeholder if not built yet) |
| `GET /api/health` | DB status + bot heartbeat age + mode |
| `GET /api/watchlist` | Active symbol watchlist loaded from config |
| `GET /api/positions?mode=` | Open positions (`mode` = `live`/`paper`, omit for all) |
| `GET /api/orders?mode=&limit=` | Recent orders (`limit` bounded 1–500, default 50) |
| `GET /api/fills?mode=&limit=` | Recent fills (`limit` bounded 1–500, default 50) |
| `GET /api/pnl?mode=` | Per-mode fills/fees/volume rollup |
| `GET /api/stream` | SSE: realtime bot events relayed from Redis |

## UI features

- **Single mode toggle:** `All / Live / Paper` (the projection's `mode` column).
- Open-positions table, PnL cards (per mode), recent-fills feed.
- Connection indicator (SSE state + bot heartbeat age).
- Dark, dependency-light (SolidJS + plain CSS; ~16 kB JS gzipped).

## Running it

### Local dev (hot reload)
```bash
# 1. API (needs Postgres + Redis running; see docker-compose)
DATABASE_URL=postgresql://trader:trader@localhost:5434/crypto_trader \
REDIS_URL=redis://localhost:6379/0 \
  uvicorn crypto_trader.api.app:app --reload --port 8000

# 2. UI dev server (proxies /api -> :8000)
cd ui && npm install && npm run dev    # http://localhost:5173
```

### Production / one process (FastAPI serves the built UI)
```bash
cd ui && npm install && npm run build  # emits ui/dist
uvicorn crypto_trader.api.app:app --host 0.0.0.0 --port 8000   # http://localhost:8000
```

### Docker
```bash
docker compose --profile ui up        # postgres + redis + dashboard at :8000
# run the bot with the UI relay on (separate profile/process):
docker compose --profile bot up        # bot publishes events:ui + projection
```
The `Dockerfile` builds the SolidJS bundle in a node stage and copies `ui/dist`
into the Python image, so the dashboard image serves the UI with no extra steps.

## Config

| Env var | Default | Meaning |
|---|---|---|
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | Bind address/port |
| `API_DASHBOARD_TOKEN` | `` (empty) | Bearer token for all `/api/*` routes. Empty = no auth (dev). |
| `ALLOWED_ORIGINS` | `*` | Comma-separated CORS origins (e.g. `http://localhost:3030,https://my.domain`). |
| `UI_EVENTS_ENABLED` | `true` | Bot publishes events to Redis for the UI |
| `UI_EVENTS_CHANNEL` | `events:ui` | Redis pub/sub channel |
| `PROJECTION_ENABLED` | `false` | Bot must populate the read-model the UI reads |
| `DATABASE_URL`, `REDIS_URL` | — | Required for the API to read + stream |

## Security

- **Read-only** — no trade-mutating endpoints exist.
- **Bearer token auth** — set `API_DASHBOARD_TOKEN=your_secret` in `.env` to protect all
  `/api/*` routes. When set, requests without a matching `Authorization: Bearer <token>`
  header receive `401 Unauthorized`. Leave unset for unauthenticated local dev access.
- **CORS** — configurable via `ALLOWED_ORIGINS` (comma-separated). Defaults to `*`
  (open) for local dev; restrict to specific origins for production deployments.
- Binds `0.0.0.0` for container access — always put behind TLS/reverse proxy when
  exposed beyond localhost.

## Files
- API: `crypto_trader/api/app.py`, `repo.py`, `events_bridge.py`
- Bot relay wiring: `crypto_trader/engine_live.py` (`RedisEventPublisher` on the event hook)
- UI: `ui/` (Vite + SolidJS + TS) → `ui/dist`
- Tests: `tests/test_api.py` (endpoints + mode filter + SSE, no DB/Redis needed)

## Caveats
- The realtime path (Redis pub/sub + SSE) and the projection were unit-tested with
  fakes/`TestClient`; validate end-to-end against a live Postgres + Redis + running
  bot (see the e2e validation prompt).
