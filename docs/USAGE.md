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

### Running Detached (Survives Terminal Close)
For background operations immune to terminal/SSH session termination, a setsid-based launcher is available:
```bash
./bin/start-detached                    # starts infra, API, and bot in background, logs to logs/
./bin/stop                              # stops all detached bot and API processes
```
Logs are saved under `logs/api_YYYYMMDD_HHMMSS.log` and `logs/bot_YYYYMMDD_HHMMSS.log`. Symlinks `logs/api.latest.log` and `logs/bot.latest.log` point to the latest runs.

## Configuration (.env)

| Var | Purpose |
|-----|---------|
| `MODE` | `paper` \| `live` (both on CoinDCX mainnet) |
| `TRADE_SYMBOL` | primary symbol (default SOLUSDT) |
| `WATCHLIST` | comma list, e.g. `BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT` (multi-symbol) |
| `DATA_SOURCE` | `binance` \| `coindcx` \| `auto` (Binance primary, CoinDCX fallback) |
| `FEED_STALE_MS` | mark price staleness ceiling (default 15000) |
| `EXECUTION_BUS` | `redis` (decoupled) \| `inproc` |
| `DATABASE_URL` / `REDIS_URL` | persistence + bus |
| `PROJECTION_ENABLED` | write the SQL read-model the UI queries |
| `MAX_LEVERAGE` / `MAX_DAILY_TRADES` / `MAX_ORDERS_PER_MINUTE` | risk caps |
| `COINDCX_API_KEY` / `COINDCX_API_SECRET` | venue auth (live + account sync) |
| `USE_DYNAMIC_LEVERAGE` | Enable context-aware dynamic leverage scaling (clamped by profile and venue) |
| `DYNAMIC_LEVERAGE_MIN` | Minimum leverage floor (e.g., 5) |
| `DYNAMIC_LEVERAGE_MAX` | Maximum leverage ceiling (e.g., 20) |
| `DYNAMIC_LEVERAGE_VOL_ATR_PERIOD` | Period for ATR volatility calculation (default: 14) |
| `DYNAMIC_LEVERAGE_HIGH_VOL_THRESHOLD` | ATR% threshold above which leverage is halved (default: 0.05) |
| `DYNAMIC_LEVERAGE_EXTREME_VOL_THRESHOLD` | ATR% threshold above which trading is halted/0x leverage (default: 0.10) |
| `DYNAMIC_LEVERAGE_DRAWDOWN_MODERATE` | Drawdown threshold where leverage starts scaling down (default: 0.05) |
| `DYNAMIC_LEVERAGE_DRAWDOWN_SEVERE` | Drawdown threshold at which leverage drops to floor (default: 0.10) |
| `DYNAMIC_LEVERAGE_MARGIN_MODERATE` | Margin ratio where leverage starts scaling down (default: 0.25) |
| `DYNAMIC_LEVERAGE_MARGIN_HIGH` | Margin ratio at which leverage drops to floor (default: 0.50) |
| `DYNAMIC_LEVERAGE_REGIME_BOOST` | Enable regime adjustments (e.g. Trend Expansion boosts, mean reversion cuts) |

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
- `ExchangeStateReconciler.reconcile_symbol(symbol)` runs every loop tick in `engine_ws`, checking
  for phantom positions, missing positions, orphaned stop/TP orders, and missing venue stop-losses.
  Triggers the kill switch on critical desyncs.

## Risk infrastructure

| Component | Module | Purpose |
|-----------|--------|---------|
| `MarginEngine` | `risk.py` | Blocks entries when margin utilization exceeds safe threshold (80%) or SL is too close to liquidation price. |
| `LeverageEngine` | `risk.py` | Validates notional exposure against leverage tier limits; scales down max leverage in high-vol regimes via ATR. |
| `DynamicLeverageManager` | `margin_engine.py` | Context-aware per-symbol leverage scaling based on volatility (ATR%), drawdown, margin ratio, and regime. |
| `Close Flip-Guard` | `wallet.py` | Prevents opposite-side exits from opening opposite positions on CoinDCX due to lack of a native reduce-only flag. |
| `OrderStateMachine` | `wallet.py` | Deterministic order lifecycle enforcement (`NEW` → `PENDING` → `FILLED`). Terminal states are immutable. |
| `ExchangeStateReconciler` | `reconciliation.py` | Continuous exchange state consistency — detects phantom positions, orphaned orders, missing stops. |
| `MRStateManager` | `strategies/mr_state.py` | Per-symbol mean-reversion restart recovery with atomic disk persistence. |

## Deep Dive: Key Safety Enhancements

### 1. Bidirectional Dynamic Leverage Scaling (`DynamicLeverageManager`)
Dynamic leverage is scaled within `[DYNAMIC_LEVERAGE_MIN, DYNAMIC_LEVERAGE_MAX]` based on real-time factors:
- **Volatility (ATR%):** ATR% above the extreme threshold (e.g., 10%) triggers a halt (0x leverage); above the high threshold (e.g., 5%) cuts leverage in half.
- **Account Drawdown:** Moderate drawdown scales leverage down; severe drawdown drops leverage to the floor (`DYNAMIC_LEVERAGE_MIN`).
- **Margin Ratio:** Moderate margin ratio scales leverage down; high margin ratio drops leverage to the floor.
- **Regime Boosting:** Favorable market regimes (e.g., `TREND_EXPANSION`) boost leverage toward the ceiling, while adverse regimes (e.g., `MEAN_REVERSION`, `LOW_VOL_CHOP`) pull it toward the floor.
- **Conviction:** Optional signal conviction multiplier scales the dynamic portion of leverage.

### 2. Close Flip-Guard (CoinDCX Safety)
CoinDCX has no native `reduce-only` parameter on futures orders. If an exit order is placed for a quantity that exceeds the actual position on the exchange (e.g., if a protective stop-loss, take-profit, or liquidation was already executed on-venue), the excess quantity would open an unintended opposite-side position.
To protect capital, the wallet uses a **Close Flip-Guard**:
- **Venue Read:** Calls `_venue_position_qty()` before executing any closing order to verify the exact quantity active on CoinDCX.
- **Clamp Exit Quantity:** Clamps the exit order size to the actual venue quantity, preventing a crossover fill.
- **Skip flat venue orders:** If the venue position is already flat (`0`), the engine skips placing the exit order on the exchange and simply marks the position as closed internally.

### 3. User Stream Keepalive & Real-time Reconciliation
To keep the WebSocket feed active and avoid silent drops, the system implements:
- **Socket.IO Keepalive:** Sends a keepalive ping every 25 seconds on a daemon thread.
- **Immediate Position Reconciliation:** In `engine_live.py`, streaming position update events from CoinDCX trigger immediate reconciliation via `_on_stream_position` if the venue and internal quantities or open status diverge, instantly resolving any blind windows.

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
- **`[ENTRY BLOCKED] Margin Utilization`** — margin utilization exceeds the safe threshold (80%).
  Reduce existing exposure or increase account balance.
- **`[ENTRY BLOCKED] Liquidation Distance`** — stop-loss is too close to the estimated liquidation
  price. Widen the stop or reduce leverage.
- **`[STATE MACHINE] Invalid transition rejected`** — a late/out-of-order WebSocket event tried to
  mutate an order in a terminal state. This is safely blocked; no action needed.

## Verified components (2026-05-28, paper)

Binance REST klines + WS LTP (mark/bid/ask), CoinDCX market data + signed auth (balance), signal
bus → consumer → paper fill (slippage applied) → projection → API/UI, risk caps + kill switch,
live triple-gate (blocks correctly), account sync against live CoinDCX, multi-symbol, MarginEngine +
LeverageEngine pre-execution gates, OrderStateMachine deterministic lifecycle, ExchangeStateReconciler
continuous state consistency, MRStateManager restart recovery, full pytest suite.
