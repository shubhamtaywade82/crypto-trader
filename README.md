# Antigravity — Production-Grade Crypto Trading System (v4)

A modular, high-fidelity algorithmic trading suite designed for the Indian market. Antigravity features a hybrid architecture using **Binance** for high-frequency market data and **CoinDCX** for execution. It includes an event-sourced wallet, decoupled Redis/Postgres pipeline, and a real-time SolidJS dashboard.

---

## 🏗️ High-Level Architecture

```text
[ Binance WS ] ──▶ [ WebSocketTradingEngine ]
                        │
                        ├──▶ [ Regime Classifier ] ──▶ [ Playbook Registry ]
                        │                                    │
                        ├──▶ [ AI Advisory Router ] ◀───────┘
                        │    (cloud: qwen3.5 / local fallback)
                        │
                        ▼
                   [ Risk Gate ]
                   MarginEngine · LeverageEngine · OrderStateMachine
                        │
                        ▼
                   [ Signal Bus ] ──▶ [ Redis Streams ]
                                           │
[ SolidJS UI ] ◀── [ FastAPI Dashboard ] ◀── [ Execution Consumer ]
                    (bearer token auth)         (CoinDCX Execution)
                                               │
                                   [ ExchangeStateReconciler ]
                                               │
                                        [ Postgres ]
                                   (events · wallet · arb)
```

### Core Components

- **Hybrid Data/Execution:** Low-latency Binance WebSocket data coupled with authoritative CoinDCX REST execution.
- **Decoupled Pipeline:** Redis Streams provide at-least-once delivery, crash recovery (PEL), and worker idempotency.
- **Event-Sourced Wallet:** Comprehensive position lifecycle tracking with 1% TDS accounting (F2), venue-resident stops (F1), and partial close support.
- **Relational Read-Model:** Postgres Projections transform JSONB events into queryable tables for the UI and analytics.
- **Risk Infrastructure:** MarginEngine (utilization + liquidation distance), LeverageEngine (dynamic tiers + ATR scaling), OrderStateMachine (deterministic lifecycle), and ExchangeStateReconciler (continuous exchange state consistency).
- **AI Advisory Subsystem:** Structured `crypto_trader/ai/` module with cloud/local adaptive routing
  (cloud-first for swing, local for intraday), SHA-256 disk cache, Pydantic-validated output schema,
  hard safety gates, and JSONL telemetry. Backed by Ollama's qwen3.5 family.

---

## 📂 Project Structure

| Directory / File     | Description                                              |
| :------------------- | :------------------------------------------------------- |
| `crypto_trader/`     | Core Python package (Engines, Exchanges, Risk, Storage). |
| `crypto_trader/ai/`  | AI advisory subsystem: router, providers (cloud/local), cache, telemetry, validators, prompts. |
| `crypto_trader/risk.py` | RiskManager, MarginEngine, LeverageEngine — all pre-execution safety gates. |
| `crypto_trader/wallet.py` | Event-sourced wallet with OrderStateMachine for deterministic order lifecycle. |
| `crypto_trader/reconciliation.py` | Exchange State Consistency Layer — detects desyncs, phantom positions, orphan orders. |
| `crypto_trader/strategies/` | Mean-Reversion strategy, MR state persistence, and playbook implementations. |
| `crypto_trader/execution/` | Redis consumer, reconciler, signal bus, account sync. |
| `crypto_trader/infra/` | Shared infrastructure: `event_routing.py` (wallet event sink factory). |
| `crypto_trader/storage/` | Persistence adapters: Postgres event store, wallet store, projection. |
| `ui/`                | SolidJS + Vite + TypeScript frontend dashboard.          |
| `bin/`               | Unified orchestrator scripts (`dev`, `bot`, `start`, `test_ui`). |
| `docker-compose.yml` | Infrastructure definition (Postgres 5435, Redis 6382).   |
| `docs/`              | Detailed architectural deep-dives.                       |

---

## 🚀 Getting Started

### 1. Prerequisites

- Python 3.8+
- Node.js 18+ (for UI)
- Docker & Docker Compose
- CoinDCX API Key/Secret (for Live mode)

### 2. Setup

```bash
cp .env.example .env
# Update .env with your credentials and configuration
pip install -r crypto_trader/requirements.txt
```

### 3. Run (one command, paper-safe)

```bash
./bin/start                 # infra + API + multi-symbol bot (MODE defaults to paper)
./bin/start --tick 60       # extra args forwarded to the bot
```

### 4. Launch Dashboard UI (separate terminal)

```bash
cd ui
npm install
npm run dev -- --port 3030
```

Access the dashboard at **<http://localhost:3030>**.

### Manual / split processes (alternative to `./bin/start`)

```bash
./bin/dev                   # backend only: infra + execution consumer + API (:8088)
./bin/bot                   # multi-symbol trading bot (uses WATCHLIST), e.g. ./bin/bot --tick 60
python3 bin/test_ui         # fire tiny paper signals to verify the UI/API pipeline
```

---

## ▶️ Going LIVE (real money)

Paper-soak first. Then set in `.env`:

```bash
MODE=live
LIVE_TRADING_ENABLED=true
LIVE_TRADING_ACK=I_UNDERSTAND_REAL_MONEY_WILL_BE_LOST
PLACE_ORDER=true
COINDCX_API_KEY=...   COINDCX_API_SECRET=...
COINDCX_MARGIN_CURRENCY=USDT     # or INR
```

Validate the gate before trading (single-symbol gated entrypoint):

```bash
python3 -m crypto_trader.engine_live --self-test-only
```

Orders are blocked unless ALL hold: `LIVE_TRADING_ENABLED=true` **and** the exact
`LIVE_TRADING_ACK` phrase **and** no `~/.crypto_trader/HALT` file **and** `PLACE_ORDER=true`.

### Emergency controls

| Action                                         | Command                                  |
| :--------------------------------------------- | :--------------------------------------- |
| Halt all live orders instantly                 | `touch ~/.crypto_trader/HALT`            |
| Resume                                         | `rm ~/.crypto_trader/HALT`               |
| Clear kill-switch + loss streak (after review) | `./bin/clear_risk`                       |
| Telegram (if configured)                       | `/kill` · `/resume` · `/status` · `/pnl` |

> **Dashboard Security:** Set `API_DASHBOARD_TOKEN=your_secret` in `.env` to enable
> bearer-token authentication on all `/api/*` routes. Leave unset for local dev (auth skipped).

---

## 🛡️ Hardened Safety (G1–G5 & F1–F5)

Antigravity is built for capital preservation through multiple layers of defense:

### Production Guards (G)

- **G1 Clock Skew:** Warnings/Halts if local clock drifts from venue (>2000ms).
- **G2 Velocity Breaker:** Limits order frequency (default: 6 orders/min).
- **G3 Thread Supervisor:** Halts the engine if WebSocket or Position Manager threads die.
- **G4 Authoritative Margin Guard:** Instant kill-switch if CoinDCX `margin_ratio_cross` exceeds config limit (default 80%).
- **G5 Strict Reconciliation:** Reconciles real exchange positions against event-sourced wallet; optional cancel-all-on-desync behavior.
- **G6 Margin Engine:** Dynamically validates margin utilization and strictly blocks entries if the stop-loss is placed too close to the liquidation threshold.
- **G7 Leverage Engine:** Enforces dynamic max-notional leverage tiers, actively scaling down exposure limits during high volatility (ATR-based).
- **G8 Deterministic Order State Machine:** Strictly enforces order lifecycle transitions (`NEW` → `PENDING` → `FILLED`/`CANCELLED`), eliminating async race conditions and phantom positions.
- **G9 Exchange State Consistency Layer:** Continuously syncs the bot's desired state with the actual exchange state. Identifies and safely cleans up orphaned orders or actively recreates missing protective stops.

### Hardened Features (F)

- **F1 Venue Stops:** Automatic placement of resting Stop-Loss orders on the exchange.
- **F2 TDS Accounting:** Models the 1% Indian TDS on sell-legs for accurate equity curves.
- **F3 Basis Guard:** Rejects entries if Binance/CoinDCX prices diverge beyond threshold.
- **F4 Execution Degradation:** Realistic paper-trading model with spread penalties.
- **F5 User Stream:** Real-time authoritative fill/balance reconciliation.

---

## 🔌 Adding New Strategies

Strategies implement `BasePlaybook` (a structural Protocol in `crypto_trader/playbooks.py`):

```python
class MyPlaybook:
    name = "playbook_my"  # registry key

    def evaluate(self, df_1h, regime, structure=None):
        # Return a setup dict or None
        ...
```

Register it in `WebSocketTradingEngine.__init__`:

```python
self.playbook_my = MyPlaybook()
# Add to _playbook_registry:
self._playbook_registry["playbook_my"] = self.playbook_my
```

Wire it to regimes in `_REGIME_STRATEGY_MAP`:

```python
ExtendedRegime.TREND_EXPANSION: ["playbook_a", "playbook_b", "playbook_my"],
```

No engine code changes required beyond these three lines.

---

## 📈 Developer Tools

| Script             | Purpose                                                                     |
| :----------------- | :-------------------------------------------------------------------------- |
| `./bin/start`      | One command: infra + Dashboard API + multi-symbol bot (paper-safe default). |
| `./bin/dev`        | Launches the backend stack (Infra + Consumer + API).                        |
| `./bin/bot`        | Launches **only** the multi-symbol trading bot.                             |
| `./bin/test_ui`    | Fires a burst of tiny test signals to verify the UI/API pipeline.           |
| `./bin/clear_risk` | Clears the kill-switch and resets the consecutive-loss counter.             |

### Sending Test Signals

While `./bin/dev` and the UI are running, run this in a new terminal:

```bash
python3 bin/test_ui
```

This will open positions in your paper wallet and you will see them appear instantly on the dashboard.

---

## ⚙️ Configuration (.env)

| Variable               | Default                                                   | Description                                                              |
| :--------------------- | :-------------------------------------------------------- | :----------------------------------------------------------------------- |
| `MODE`                 | `paper`                                                   | `paper` (simulated) or `live` (CoinDCX).                                 |
| `TRADE_SYMBOL`         | `SOLUSDT`                                                 | Active symbol for the single-engine runner.                              |
| `MAX_LEVERAGE`         | `2`                                                       | Hard leverage cap (Max 2x for safety).                                   |
| `MAX_DAILY_TRADES`     | `2`                                                       | Daily safety limit for trades.                                           |
| `MAX_MARGIN_RATIO`     | `0.80`                                                    | Exchange liquidation guard threshold.                                    |
| `DATABASE_URL`         | `postgresql://trader:trader@localhost:5435/crypto_trader` | **Required.** Postgres for event store, wallet, and arb persistence.    |
| `REDIS_URL`            | `redis://localhost:6382/0`                                | Redis connection string.                                                 |
| `API_DASHBOARD_TOKEN`  | _(empty)_                                                 | Bearer token for dashboard API auth. Empty = no auth (dev).             |
| `ALLOWED_ORIGINS`      | `*`                                                       | Comma-separated CORS origins for dashboard API.                          |
| `OLLAMA_CLOUD_API_KEY` | _(empty)_                                                 | Ollama Cloud API key — enables cloud routing for swing-mode analysis.   |
| `CLOUD_OLLAMA_MODEL`   | `qwen3.5:cloud`                                           | Cloud model for deep analysis.                                           |
| `OLLAMA_MODEL`         | `qwen3.5:4b`                                              | Local Ollama model for intraday reasoning.                               |
| `USE_CLOUD_LLM`        | `false`                                                   | Force all LLM calls to cloud (overrides adaptive routing).               |

---

## ⚠️ Disclaimer

This software is for **educational and paper-trading purposes only**.

- Cryptocurrency futures trading involves significant risk of loss.
- High leverage is extremely aggressive.
- Always test on testnet or in simulation before deploying real capital.
- The authors are not responsible for any financial losses.
