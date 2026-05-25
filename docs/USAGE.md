# Crypto Trader v4 — E2E Usage Guide

Binance = market data source. CoinDCX = execution venue. Redis Streams = event bus.
PostgreSQL = event store + read-model projection. SolidJS UI over a FastAPI read API.

## Architecture (tick → signal → order → UI)

```
Binance WS/REST ─┐
                 ├─► engine (regime/structure/playbooks + optional LLM) ─► Signal
CoinDCX MD ──────┘                                                          │
                                                                           ▼
                                          Redis stream  execution:signals (XADD)
                                                                           │
                                          run_consumer (group execution_engine)
                                            • idempotency lock (SET NX)
                                            • risk gate (kill switch + daily cap)
                                            • poison-pill → DLQ, XACK after success
                                                                           ▼
                                          WalletSignalAdapter → EnhancedFuturesWallet
                                            • paper: synthetic fill + slippage/collar
                                            • live : CoinDCXExecutionEngine (gated)
                                                                           │
                          domain events ──┬─► Postgres event store (events JSONB)
                                          ├─► Postgres projection (active_positions/orders/fills)
                                          └─► Redis stream events:broadcast
                                                                           │
                                          api_service (8088): /positions /orders /fills /pnl /events/stream(SSE)
                                                                           │
                                                                     SolidJS UI (3030)
```

Note: there are two API/event stacks. `bin/dev` uses **`api_service.py` + `run_consumer` →
`events:broadcast` stream**. The engine path (`multi_engine`/`engine_live`) publishes UI events
to the **`events:ui` pub/sub** consumed by `crypto_trader/api/app.py`. The UI (`ui/src/App.tsx`)
targets `api_service.py` on `http://localhost:8088`.

## Quick start (paper mode)

```bash
# 1. infra
docker compose up -d postgres redis      # pg :5435, redis :6382

# 2. backend (consumer + read API). Loads .env, defaults MODE=paper.
./bin/dev                                 # API → http://localhost:8088

# 3. UI (separate shell)
cd ui && npm install && npm run dev -- --port 3030
```

Open http://localhost:3030 — toggle PAPER/LIVE, watch positions/orders/PnL + live event feed.

## Configuration (.env)

| Var | Purpose |
|-----|---------|
| `MODE` | `paper` \| `testnet` \| `live` |
| `TRADE_SYMBOL` | primary symbol (default SOLUSDT) |
| `WATCHLIST` | comma list, e.g. `BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT` (multi-symbol) |
| `DATA_SOURCE` | `binance` \| `coindcx` \| `auto` (Binance primary, CoinDCX fallback) |
| `FEED_STALE_MS` | mark price staleness ceiling (default 15000) |
| `EXECUTION_BUS` | `redis` (decoupled) \| `inproc` |
| `DATABASE_URL` / `REDIS_URL` | persistence + bus |
| `PROJECTION_ENABLED` | write the SQL read-model the UI queries |
| `MAX_LEVERAGE` / `MAX_DAILY_TRADES` / `MAX_ORDERS_PER_MINUTE` | risk caps |
| `COINDCX_API_KEY` / `COINDCX_API_SECRET` | venue auth (live + account sync) |

### Going live — the triple gate (all required)

1. `CoinDCXExecutionEngine(i_understand_real_money=True)` (constructor flag)
2. `LIVE_TRADING_ENABLED=true`
3. `LIVE_TRADING_ACK=I_UNDERSTAND_REAL_MONEY_WILL_BE_LOST`
4. no HALT file at `~/.crypto_trader/HALT`

Any missing condition → `LiveTradingBlocked`, no order sent. Order placement is enabled
**only** when all four hold; flip any off to disable live placement instantly.
`MODE=paper` ignores the gate and uses simulated fills.

## Multi-symbol

`multi_engine` spawns one `WebSocketTradingEngine` thread per `WATCHLIST` symbol (staggered
~1.5s to respect Binance connection limits). One shared `RiskManager` enforces caps across all
symbols; the wallet keys positions by symbol. Signals route by `Signal.symbol`.

## Reconciliation & sync

- `AccountSync.snapshot(symbol)` pulls venue truth from CoinDCX (balances/positions/open orders/fills).
- `Reconciler.reconcile(symbol)` compares wallet vs venue: detects ghost/missing positions and qty
  drift, re-places vanished protective SL orders, cancels orphans. Unresolved drift trips the
  RiskManager kill switch. Runs at boot (blocks live start on failure) and periodically.

## Manual E2E signal injection (testing)

```python
from crypto_trader.infra.redis_streams import RedisStreamBus
from crypto_trader.execution.signal_bus import SignalPublisher, Signal
bus = RedisStreamBus.from_url("redis://localhost:6382/0")
SignalPublisher(bus).emit(Signal(
    strategy_id="manual", symbol="DOGEUSDT", side="LONG",
    quantity=100.0, mode="paper", price=0.15, metadata={"entry_price": 0.15}))
```

Then `curl localhost:8088/positions?mode=paper` and `.../pnl?mode=paper`.

## Troubleshooting

- **`[OPEN BLOCKED] X: Already have open position`** — the paper wallet rehydrates prior state
  from `wallet_<ns>_<symbol>.json` / its sqlite snapshot. To start clean, remove the wallet state
  files (and `risk_state.json` for daily counters) or close the position first.
- **`/positions` empty but consumer ran** — confirm `PROJECTION_ENABLED=true` and `DATABASE_URL` set
  in the consumer's env.
- **Duplicate consumers** — only run ONE `run_consumer` per group (`execution_engine`); multiple
  compete for messages and split delivery.
- **Port 8088 in use** — a previous `api_service` is still running; kill it before `bin/dev`.
- **UI shows nothing live** — `api_service` SSE reads the `events:broadcast` stream produced by
  `run_consumer`; make sure the consumer (not just the engine) is running.

## Verified components (2026-05-25, paper)

Binance REST klines + WS LTP (mark/bid/ask), CoinDCX market data + signed auth (balance), signal
bus → consumer → paper fill (slippage applied) → projection → API/UI, risk caps + kill switch,
live triple-gate (blocks correctly), account sync against live CoinDCX, multi-symbol, full pytest
suite (119 passed).
