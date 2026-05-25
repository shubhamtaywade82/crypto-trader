# Antigravity — Agent Context

## 🎯 Strategic Intent
Antigravity is a production-grade algorithmic trading system for Binance Futures (Market Data) and CoinDCX (Execution). It prioritizes **Capital Preservation** through a multi-layered safety architecture.

## 🏗️ Technical Architecture (v4)
The system is decoupled using Redis Streams to separate strategy generation from order execution.

1.  **Market Data:** High-fidelity Binance WebSocket (`@bookTicker`, `@markPrice`).
2.  **Execution Engine:** CoinDCX REST execution with 1% Indian TDS accounting (F2).
3.  **Persistence:** Event-sourced `EnhancedFuturesWallet` with Postgres Read-Model Projections.
4.  **Messaging:** Redis Streams (`execution:signals`) for decoupling and idempotency.
5.  **Dashboard:** FastAPI + SSE bridge powering a real-time SolidJS dashboard (Port 3030).

## 🛡️ Hard Safety Mandates
AI Agents MUST respect these guards when modifying code:

- **G1 (Clock Skew):** Reject entries if local↔venue clock drift > 2000ms.
- **G2 (Velocity):** Max 6 orders per minute per symbol.
- **G3 (Supervisor):** Halt engine if WebSocket or Position threads die.
- **G4 (Authoritative Health):** Halt instantly if CoinDCX `margin_ratio_cross` > 80%.
- **G5 (Reconciliation):** Mandatory sync of internal state vs Exchange state on startup/reconnect.

- **F3 (Basis Guard):** Entry price MUST be within 0.5% of Binance mark price.

## 📈 Development Workflow
- `MODE=paper` is the default. **NEVER** set `MODE=live` in test environments.
- Infrastructure ports: **Postgres: 5435**, **Redis: 6382**.
- Unified Orchestration:
    - `./bin/dev`: Starts backend (Infra, Consumer, API).
    - `./bin/bot`: Starts only the Trading Engine.
    - `./bin/test_ui`: Simulates signal burst for UI validation.

## 📂 Key Files
- `crypto_trader/engine_live.py`: Gated entrypoint and preflight checks.
- `crypto_trader/execution/run_consumer.py`: The decoupled execution worker.
- `crypto_trader/risk.py`: Master safety gate implementation.
- `crypto_trader/api_service.py`: Dashboard backend.
- `ui/src/App.tsx`: Dashboard frontend logic.
