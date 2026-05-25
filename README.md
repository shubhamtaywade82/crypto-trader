# Antigravity — Production-Grade Crypto Trading System (v4)

A modular, high-fidelity algorithmic trading suite designed for the Indian market. Antigravity features a hybrid architecture using **Binance** for high-frequency market data and **CoinDCX** for execution. It includes an event-sourced wallet, decoupled Redis/Postgres pipeline, and a real-time SolidJS dashboard.

---

## 🏗️ High-Level Architecture

```
[ Market Data ] ─────▶ [ Strategy Engine ] ─────▶ [ Redis Streams ]
(Binance WS)           (Regime/SMC Analysis)       (Signal Queue)
                                                          │
[ SolidJS UI ] ◀────── [ FastAPI Bridge ] ◀───── [ Execution Consumer ]
(Live Dashboard)       (SSE/Broadcast)             (CoinDCX Execution)
```

### Core Components
- **Hybrid Data/Execution:** Low-latency Binance WebSocket data coupled with authoritative CoinDCX REST execution.
- **Decoupled Pipeline:** Redis Streams provide at-least-once delivery, crash recovery (PEL), and worker idempotency.
- **Event-Sourced Wallet:** Comprehensive position lifecycle tracking with 1% TDS accounting (F2), venue-resident stops (F1), and partial close support.
- **Relational Read-Model:** Postgres Projections transform JSONB events into queryable tables for the UI and analytics.
- **AI Fusion (Optional):** Optional Ollama/LLM advisory layer for signal sentiment filtering.

---

## 📂 Project Structure

| Directory / File | Description |
|:--- | :--- |
| `crypto_trader/` | Core Python package (Engines, Exchanges, Risk, Storage). |
| `ui/` | SolidJS + Vite + TypeScript frontend dashboard. |
| `bin/` | Unified orchestrator scripts (`dev`, `bot`, `test_ui`). |
| `docker-compose.yml` | Infrastructure definition (Postgres 5435, Redis 6382). |
| `docs/` | Detailed architectural deep-dives. |

---

## 🚀 Getting Started (Fast Path)

The system is now unified into a single development orchestrator.

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

### 3. Launch Backend Stack
Starts Postgres, Redis, the Execution Consumer, and the Dashboard API.
```bash
./bin/dev
```

### 4. Launch Dashboard UI (Manual)
```bash
cd ui
npm install
npm run dev -- --port 3030
```
Access the dashboard at **http://localhost:3030**.

---

## 🛡️ Hardened Safety (G1–G5 & F1–F5)

Antigravity is built for capital preservation through multiple layers of defense:

### Production Guards (G)
- **G1 Clock Skew:** Warnings/Halts if local clock drifts from venue (>2000ms).
- **G2 Velocity Breaker:** Limits order frequency (default: 6 orders/min).
- **G3 Thread Supervisor:** Halts the engine if WebSocket or Position Manager threads die.
- **G4 Authoritative Margin Guard:** Instant kill-switch if CoinDCX `margin_ratio_cross` exceeds 80%.
- **G5 Strict Reconciliation:** Optional cancel-all-on-desync behavior.

### Hardened Features (F)
- **F1 Venue Stops:** Automatic placement of resting Stop-Loss orders on the exchange.
- **F2 TDS Accounting:** Models the 1% Indian TDS on sell-legs for accurate equity curves.
- **F3 Basis Guard:** Rejects entries if Binance/CoinDCX prices diverge beyond threshold.
- **F4 Execution Degradation:** Realistic paper-trading model with spread penalties.
- **F5 User Stream:** Real-time authoritative fill/balance reconciliation.

---

## 📈 Developer Tools

| Script | Purpose |
|:--- | :--- |
| `./bin/dev` | Launches the full backend stack (Infra + Consumer + API). |
| `./bin/bot` | Launches **only** the multi-symbol trading bot. |
| `./bin/test_ui` | Fires a burst of tiny test signals to verify the UI/API pipeline. |

### Sending Test Signals
While `./bin/dev` and the UI are running, run this in a new terminal:
```bash
python3 bin/test_ui
```
This will open positions in your paper wallet and you will see them appear instantly on the dashboard.

---

## ⚙️ Configuration (.env)

| Variable | Default | Description |
|:--- | :--- | :--- |
| `MODE` | `paper` | `paper` (simulated) or `live` (CoinDCX). |
| `TRADE_SYMBOL` | `SOLUSDT` | Active symbol for the single-engine runner. |
| `MAX_LEVERAGE` | `2` | Hard leverage cap (Max 2x for safety). |
| `MAX_DAILY_TRADES`| `2` | Daily safety limit for trades. |
| `MAX_MARGIN_RATIO`| `0.80` | Exchange liquidation guard threshold. |
| `DATABASE_URL` | `:5435` | Postgres connection string. |
| `REDIS_URL` | `:6382` | Redis connection string. |

---

## ⚠️ Disclaimer

This software is for **educational and paper-trading purposes only**.
- Cryptocurrency futures trading involves significant risk of loss.
- High leverage is extremely aggressive.
- Always test on testnet or in simulation before deploying real capital.
- The authors are not responsible for any financial losses.
