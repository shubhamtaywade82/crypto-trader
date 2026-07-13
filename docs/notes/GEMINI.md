# Antigravity — Agent Context

## 🎯 Strategic Intent
Antigravity is a production-grade algorithmic trading system for Binance Futures (Market Data) and CoinDCX (Execution). It prioritizes **Capital Preservation** through a multi-layered safety architecture. The primary strategy is a **single-leg mean-reversion futures bot** operating with low leverage (2-3x) and isolated margin mode.

## 🏗️ Technical Architecture (v4)
The system is decoupled using Redis Streams to separate strategy generation from order execution.

1.  **Market Data:** High-fidelity Binance WebSocket (`@bookTicker`, `@markPrice`).
2.  **Execution Engine:** CoinDCX REST execution with 1% Indian TDS accounting (F2).
3.  **Persistence:** Event-sourced `EnhancedFuturesWallet` with Postgres Read-Model Projections.
4.  **Messaging:** Redis Streams (`execution:signals`) for decoupled execution
    with idempotency / DLQ / PEL recovery. **Opt-in** via `EXECUTION_BUS=redis`
    + `REDIS_URL`. When enabled, `engine_ws` PUBLISHES entries and an
    **in-process consumer thread shares the same wallet** (so `ws_pm` keeps
    managing exits). When disabled (default), the engine uses the hardened
    direct path (`wallet.open_position`). DO NOT run the standalone
    `run_consumer` on the same stream+group while the in-process consumer is
    active — a consumer group load-balances and would split entries across two
    wallets.
5.  **Dashboard:** FastAPI + SSE bridge powering a real-time SolidJS dashboard (Port 3030).
6.  **Risk Infrastructure:**
    - `MarginEngine` — margin utilization caps + liquidation distance validation.
    - `LeverageEngine` — dynamic leverage tier enforcement with ATR-based scaling.
    - `OrderStateMachine` — deterministic order lifecycle transitions (prevents async race conditions).
    - `ExchangeStateReconciler` — continuous exchange state consistency checks (phantom positions, orphaned orders, missing stops).
    - `MRStateManager` — per-symbol mean-reversion restart recovery with atomic persistence.

## 🛡️ Hard Safety Mandates
AI Agents MUST respect these guards when modifying code:

- **G1 (Clock Skew):** Reject entries if local↔venue clock drift > 2000ms.
- **G2 (Velocity):** Max 6 orders per minute per symbol.
- **G3 (Supervisor):** Halt engine if WebSocket or Position threads die.
- **G4 (Authoritative Health):** Halt instantly if CoinDCX `margin_ratio_cross` > 80%.
- **G5 (Reconciliation):** Mandatory sync of internal state vs Exchange state on startup/reconnect.
- **G6 (Margin Engine):** Block entries when margin utilization exceeds threshold or SL is too close to liquidation price.
- **G7 (Leverage Engine):** Validate notional exposure against leverage tier limits; scale down in high-volatility regimes.
- **G8 (Order State Machine):** Enforce deterministic order lifecycle; terminal states are immutable.
- **G9 (Exchange Consistency):** Continuous reconciliation of wallet vs exchange; kill switch on critical desyncs.

- **F3 (Basis Guard):** Entry price MUST be within 0.5% of Binance mark price.

## 📈 Development Workflow
- `MODE=paper` is the default. **NEVER** set `MODE=live` in test environments.
- Infrastructure ports: **Postgres: 5435**, **Redis: 6382**.
- Unified Orchestration:
    - `./bin/dev`: Starts backend (Infra, Consumer, API). Pre-emptively cleans up orphaned processes.
    - `./bin/bot`: Starts only the Trading Engine. Pre-emptively cleans up orphaned processes.
    - `./bin/start`: Unified launcher for full stack (Infra, API, Bot). Pre-emptively cleans up orphaned processes.
    - `./bin/test_ui`: Simulates signal burst for UI validation.

## 📂 Key Files
- `crypto_trader/engine_live.py`: Gated entrypoint and preflight checks.
- `crypto_trader/engine_ws.py`: WebSocket-enhanced trading engine with integrated risk checks.
- `crypto_trader/execution/run_consumer.py`: The decoupled execution worker.
- `crypto_trader/risk.py`: Master safety gate — RiskManager, MarginEngine, LeverageEngine.
- `crypto_trader/wallet.py`: Event-sourced wallet with OrderStateMachine.
- `crypto_trader/reconciliation.py`: Exchange State Consistency Layer.
- `crypto_trader/strategies/mr_state.py`: Mean-Reversion per-symbol restart recovery.
- `crypto_trader/strategies/mean_reversion.py`: Mean-Reversion strategy evaluation logic.
- `crypto_trader/api_service.py`: Dashboard backend.
- `ui/src/App.tsx`: Dashboard frontend logic.
