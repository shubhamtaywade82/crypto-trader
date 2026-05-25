# Redis Streams + PostgreSQL Data Modeling

> Status: **additive and opt-in.** The default runtime is unchanged
> (single-process, `EXECUTION_BUS=inproc`, `PROJECTION_ENABLED=false`). Turn the
> pieces on with config. The Redis path's modules are unit-tested against an
> in-memory fake; validate end-to-end against a real Redis before live use
> (see *Caveats*).

This document describes the two data-modeling layers added for the decoupled
signal/execution pipeline:

1. **PostgreSQL** — the source of truth (append-only JSONB event journal) plus a
   derived relational **read-model** (projection) for UI/analytics.
2. **Redis Streams** — the transport that decouples strategy (producer) from
   execution (consumer group), with at-least-once delivery and crash recovery.

---

## 1. PostgreSQL data model

### 1a. Event journal (source of truth) — already existed

`crypto_trader/storage/postgres_store.py` (`PostgresEventStore`) is selected by
`get_event_store(DATABASE_URL)` whenever `DATABASE_URL` is set. It is an
**append-only JSONB event store** — the event-sourced model. We are *not* on a
flat relational schema.

```sql
events(id BIGSERIAL PK, ts BIGINT, type TEXT, symbol TEXT, payload JSONB)
snapshots(id BIGSERIAL PK, ts BIGINT, state JSONB)
```

Current state is a projection of `events`; the wallet's `PortfolioReducer`
replays them, and the reconciler injects correction events.

### 1b. Relational projection (read-model) — new

`crypto_trader/storage/projection.py` derives query-friendly tables from the
event stream so a UI can run plain SQL instead of replaying JSONB. It is a
**derived view** — rebuildable at any time; the journal stays the source of truth.

```sql
active_positions(symbol, mode, side, qty, avg_price, status, last_event_ts, updated_at)  -- PK (symbol, mode)
orders(exchange_order_id, mode, symbol, side, order_type, qty, filled_qty, avg_fill_price, status, created_at)  -- PK (exchange_order_id, mode)
fills(id BIGSERIAL, exchange_order_id, symbol, side, mode, price, qty, fee, ts)
```

- **`mode` column** (`paper`/`live`) is part of the PK on positions/orders, so the
  UI can split *or* aggregate live vs paper:
  - `SELECT * FROM active_positions WHERE mode='live';` (split)
  - `projection.pnl_summary()` returns per-mode aggregates; pass `mode=` to filter.
- **`ProjectionState`** is a pure reducer (`apply(event)`) — unit-tested, no DB.
- **`PostgresProjection`** persists it (lazy `psycopg2`): subscribe `.apply` to the
  wallet event hook for live updates, or `rebuild_from(events)` to rematerialize.
- **`RECON_ADJUST`** events let the reconciler inject truth corrections that the
  projection applies like any other event.

Wiring: when `PROJECTION_ENABLED=true` and `DATABASE_URL` is set, `engine_live`
chains `projection.apply` onto the wallet event hook (alongside the event store).
A projection failure is logged, never fatal — it's a derived view.

---

## 2. Redis Streams signal/execution pipeline

```
 strategy (producer)                         execution worker (consumer group)
 ───────────────────                         ─────────────────────────────────
 SignalPublisher.emit(Signal)                SignalConsumer.run_forever()
        │  XADD                                      │  XREADGROUP (PEL first, then ">")
        ▼                                            ▼
   execution:signals  ───────────────────────▶  idempotency? risk gate? mode adapter
        │                                            │  success → mark_processed + XACK
        │                                            │  failure → no XACK (stays in PEL)
        └── poison pill / unknown mode ──▶ execution:signals:dlq
```

### Components

| Module | Role |
|--------|------|
| `infra/redis_streams.py` `RedisStreamBus` | Streams façade: publish, ensure_group, read_new (`>`), read_pending (PEL `0`), ack, delivery_count (`XPENDING`), to_dlq, is_processed/mark_processed |
| `execution/signal_bus.py` `Signal` | Exchange-agnostic signal schema (strategy_id, symbol, side, qty, **mode**, price, metadata, timestamp, signal_id) |
| `execution/signal_bus.py` `SignalPublisher` | `XADD` a signal (strategies stay pure) |
| `execution/signal_bus.py` `SignalConsumer` | Consumer-group worker (idempotency, risk gate, poison-pill DLQ, PEL recovery, XACK-on-success) |
| `execution/signal_bus.py` `WalletSignalAdapter` | Executes a `Signal` against the wallet (paper or live) |
| `execution/signal_bus.py` `build_risk_gate` | Signal-level gate using `RiskManager` (kill switch + daily/velocity caps) |
| `execution/run_consumer.py` | The execution-worker entrypoint; reuses `LiveTradingSystem` for the gated build |

### Safety semantics (why this is crash-safe)

- **At-least-once:** a message is `XACK`-ed only after the adapter succeeds. A crash
  mid-execution leaves it in the Pending Entries List (PEL).
- **Boot + per-batch recovery:** the worker drains its PEL (`read_pending`, id `0`)
  before reading new messages, so orphaned in-flight signals are handled first.
- **Idempotency:** a `idem:{strategy_id}:{timestamp}` marker is set **after** success
  (not as a pre-claim), so a failed attempt retries while a true duplicate is skipped.
- **Poison-pill → DLQ:** when `XPENDING` delivery count exceeds
  `SIGNAL_MAX_DELIVERIES`, the message is moved to `execution:signals:dlq` and acked,
  so it can't crash-loop the worker. Malformed payloads and unknown modes go straight
  to the DLQ.
- **Risk gate before execution:** the kill switch + daily + per-minute velocity caps
  (G2) veto a signal before it reaches the adapter; a veto is terminal (acked).

---

## 3. Configuration

| Env var | Default | Meaning |
|---|---|---|
| `EXECUTION_BUS` | `inproc` | `redis` enables the decoupled consumer worker |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection (empty disables) |
| `SIGNAL_STREAM` | `execution:signals` | Signal stream name (DLQ is `<stream>:dlq`) |
| `CONSUMER_GROUP` | `execution_engine` | Consumer-group name |
| `SIGNAL_MAX_DELIVERIES` | `3` | Retries before a signal is DLQ'd |
| `IDEMPOTENCY_TTL_SECONDS` | `300` | Duplicate-suppression window |
| `PROJECTION_ENABLED` | `false` | Materialize the relational read-model (needs `DATABASE_URL`) |
| `DATABASE_URL` | _(unset → SQLite dev)_ | Postgres DSN for the event store + projection |

`config.redis_enabled` is True only when `EXECUTION_BUS=redis` **and** `REDIS_URL` is set.

---

## 4. Running it

```bash
# Infra (Postgres :5434, Redis :6379)
docker compose up --wait postgres redis

# Decoupled mode: run the trading/signal side + the execution worker separately.
EXECUTION_BUS=redis REDIS_URL=redis://localhost:6379/0 PROJECTION_ENABLED=true \
  DATABASE_URL=postgresql://trader:trader@localhost:5434/crypto_trader \
  python -m crypto_trader.execution.run_consumer

# Or via compose profiles (postgres + redis + bot + execution-worker):
docker compose --profile bot up
```

Inspect the read-model:

```sql
SELECT * FROM active_positions WHERE mode = 'live';
SELECT mode, COUNT(*), SUM(fee) FROM fills GROUP BY mode;   -- live vs paper split
```

DLQ inspection:

```bash
redis-cli XRANGE execution:signals:dlq - +
```

---

## 5. Caveats / follow-ups

- **Validate against a real Redis.** The bus/consumer/projection are unit-tested
  with an in-memory fake (`tests/test_redis_pipeline.py`) and a pure reducer; they
  have not been run against a live Redis/Postgres in CI here.
- **Retry uses PEL re-reads.** A production-grade worker should use `XAUTOCLAIM`
  with an idle threshold so a *different* worker can claim a dead consumer's
  backlog; today recovery is per-consumer. The delivery-count → DLQ guard is in place.
- **Producer wiring is opt-in.** `engine_ws` still executes inline by default; to go
  fully decoupled, have the strategy publish via `SignalPublisher` instead of calling
  `wallet.open_position` directly, and run `run_consumer` as the executor.
- **Projection is derived.** If it drifts, `rebuild_from(event_store.load_events())`
  rematerializes it from the journal.
