# ETH Futures Mean-Reversion Trading Bot

## Complete PRD + SRD

### Version 1.0

---

# 1. Executive Summary

## Product Name

Crypto Futures Mean-Reversion Trading Bot

## Product Type

Fully automated cryptocurrency perpetual futures trading system.

## Primary Objective

Build a production-grade, self-hosted, automated futures trading system capable of:

* Trading Binance USDⓈ-M perpetual futures
* Executing deterministic mean-reversion strategies
* Managing risk automatically
* Recovering safely from crashes/restarts
* Operating continuously with minimal manual intervention
* Maintaining complete auditability of all trading actions

---

# 2. Product Goals

## Primary Goals

### G1. Safe Autonomous Execution

The system must autonomously execute trades with strict risk controls.

### G2. Exchange Reliability

The system must remain operational despite:

* temporary API failures
* websocket disconnects
* network instability
* process crashes
* server restarts

### G3. Deterministic Strategy Execution

Trading decisions must be deterministic and reproducible.

### G4. Full Auditability

Every:

* signal
* order
* fill
* cancellation
* stop-loss
* error
* position change

must be persisted and traceable.

### G5. Production Readiness

The bot must support:

* cloud deployment
* restart recovery
* observability
* logging
* monitoring
* emergency shutdown

---

# 3. Non-Goals

The following are explicitly excluded from V1:

* machine learning prediction
* reinforcement learning
* discretionary/manual trading UI
* copy trading
* portfolio optimization
* options trading
* arbitrage
* high-frequency trading
* latency-sensitive market making
* multi-exchange routing
* on-chain trading

---

# 4. Trading Strategy Definition

## Strategy Type

Mean Reversion

## Market

Binance USDⓈ-M Futures

## Instrument

ETH/USDT perpetual futures

## Timeframe

15-minute candles

## Core Assumption

Price statistically reverts toward its short-term mean after temporary deviation.

---

# 5. Trading Logic

## Indicator

### SMA

* Period: 20
* Source: close price
* Candle source: last fully closed candle only

---

## Entry Conditions

### Long Entry

Open long when:

```text
close <= SMA20 * (1 - entry_band)
```

Default:

```text
entry_band = 1.5%
```

---

### Short Entry

Open short when:

```text
close >= SMA20 * (1 + entry_band)
```

---

# 6. Exit Logic

## Mean Reversion Exit

### Long Exit

Close long when:

```text
close >= SMA20
```

### Short Exit

Close short when:

```text
close <= SMA20
```

---

## Hard Stop Loss

### Long Stop

```text
entry_price * (1 - stop_loss_pct)
```

### Short Stop

```text
entry_price * (1 + stop_loss_pct)
```

Default:

```text
stop_loss_pct = 0.8%
```

Stop-loss orders must:

* be exchange-hosted
* be reduce-only
* be attached immediately after fill

---

# 7. Funding Rate Protection

## Rule

No new entries allowed if:

```text
abs(funding_rate) > max_funding_rate
```

Default:

```text
0.05%
```

---

# 8. Position Management

## Constraints

### Single Position Only

Only one active position allowed per symbol.

### One-Way Mode Only

System assumes Binance Futures one-way mode.

### No Averaging Down

No pyramiding or martingale logic allowed.

### No Hedging

Simultaneous long and short positions prohibited.

---

# 9. Risk Management

## Risk Per Trade

Default:

```text
0.5% account equity
```

---

## Position Sizing Formula

```text
risk_capital = equity * risk_per_trade

qty = risk_capital / stop_distance
```

---

## Maximum Leverage

Default:

```text
3x
```

Hard cap:

```text
5x
```

---

## Daily Risk Limits

### Daily Max Loss

Default:

```text
3% account equity
```

### Consecutive Loss Lockout

Default:

```text
3 consecutive stop-losses
```

### Trading Halt Duration

Default:

```text
24 hours
```

---

# 10. System Architecture

## High-Level Components

```text
+----------------------+
| Strategy Engine      |
+----------------------+
           |
           v
+----------------------+
| Risk Manager         |
+----------------------+
           |
           v
+----------------------+
| Execution Engine     |
+----------------------+
           |
           v
+----------------------+
| Exchange Adapter     |
| (CCXT/Binance)       |
+----------------------+

+----------------------+
| Persistence Layer    |
+----------------------+

+----------------------+
| Monitoring & Alerts  |
+----------------------+
```

---

# 11. Core Modules

# 11.1 Exchange Adapter

## Responsibilities

* abstract Binance APIs
* normalize CCXT responses
* handle retries
* handle precision formatting
* validate exchange constraints

## Required Features

* market orders
* stop-market orders
* fetch positions
* fetch balances
* fetch open orders
* cancel orders
* leverage management
* funding rate retrieval

---

# 11.2 Strategy Engine

## Responsibilities

* candle ingestion
* indicator computation
* signal generation
* signal deduplication

## Rules

* process only closed candles
* no intra-candle decisions
* deterministic outputs

---

# 11.3 Risk Manager

## Responsibilities

* position sizing
* leverage validation
* daily loss enforcement
* stop-loss validation
* funding rate checks
* exposure constraints

---

# 11.4 Execution Engine

## Responsibilities

* place entries
* attach stop-loss
* cancel stale orders
* flatten positions
* reconcile exchange state

## Requirements

* idempotent execution
* retry-safe
* no duplicate entries

---

# 11.5 State Manager

## Responsibilities

* persist runtime state
* restore state after restart
* reconcile exchange state

## Persisted Fields

* active position
* stop order ID
* last processed candle
* last trade
* current drawdown
* consecutive losses

---

# 11.6 Monitoring Service

## Responsibilities

* health checks
* heartbeat
* latency tracking
* exchange connectivity monitoring

---

# 11.7 Alert Service

## Channels

* Telegram
* Discord (optional)
* Email (optional)

## Alerts

* entry opened
* exit completed
* stop-loss triggered
* API failure
* websocket disconnect
* daily lockout activated
* restart recovery triggered

---

# 12. Persistence Layer

## Database

PostgreSQL

---

# 13. Database Schema

# 13.1 trades

| column      | type      |
| ----------- | --------- |
| id          | uuid      |
| symbol      | string    |
| side        | string    |
| entry_price | decimal   |
| exit_price  | decimal   |
| quantity    | decimal   |
| pnl         | decimal   |
| opened_at   | timestamp |
| closed_at   | timestamp |
| exit_reason | string    |

---

# 13.2 orders

| column            | type      |
| ----------------- | --------- |
| id                | uuid      |
| exchange_order_id | string    |
| symbol            | string    |
| side              | string    |
| type              | string    |
| status            | string    |
| price             | decimal   |
| quantity          | decimal   |
| raw_payload       | jsonb     |
| created_at        | timestamp |

---

# 13.3 bot_state

| column | type   |
| ------ | ------ |
| key    | string |
| value  | jsonb  |

---

# 13.4 daily_metrics

| column             | type    |
| ------------------ | ------- |
| id                 | uuid    |
| date               | date    |
| realized_pnl       | decimal |
| drawdown           | decimal |
| consecutive_losses | integer |

---

# 14. Exchange Constraints

## Precision Handling

Must use:

* amount_to_precision
* price_to_precision

---

## Reduce-Only Orders

All closing orders must use:

```text
reduceOnly=true
```

---

## Exchange Time Sync

System must auto-adjust timestamp drift.

---

# 15. WebSocket Requirements

## Required Streams

* mark price
* user data stream
* order updates
* account updates

---

## Reconnect Rules

* exponential backoff
* automatic session recovery
* stale stream detection

---

# 16. Candle Processing Rules

## Strict Requirements

### Use Closed Candles Only

Never trade on active candle.

### Duplicate Protection

Never process same candle twice.

### Missing Candle Detection

Detect gaps in OHLCV sequence.

---

# 17. Error Handling

## Exchange Errors

* retry transient failures
* abort fatal errors

---

## Fatal Errors

Examples:

* insufficient margin
* invalid leverage
* invalid symbol
* reduce-only rejection

Bot must:

* alert operator
* halt trading safely

---

# 18. Restart Recovery

## On Startup

### Step 1

Fetch live positions.

### Step 2

Fetch open orders.

### Step 3

Rebuild internal state.

### Step 4

Recreate missing stop-loss orders.

### Step 5

Resume processing.

---

# 19. Deployment Requirements

## Environment

Ubuntu Linux

---

## Recommended Infrastructure

### Minimum

* 2 vCPU
* 2GB RAM
* SSD storage

### Recommended

* AWS EC2
* DigitalOcean Droplet
* Hetzner VPS

---

# 20. Process Management

## Required

Systemd or Docker restart policy.

---

# 21. Logging Requirements

## Log Categories

* strategy
* execution
* exchange
* websocket
* risk
* alerts
* database

---

## Log Format

Structured JSON logs preferred.

---

# 22. Metrics

## Required Metrics

* uptime
* trade count
* win rate
* realized pnl
* unrealized pnl
* max drawdown
* average holding time
* API latency
* websocket reconnect count

---

# 23. Security Requirements

## API Key Security

* environment variables only
* never hardcoded
* encrypted secrets preferred

---

## IP Whitelisting

Mandatory in production.

---

## Principle of Least Privilege

Disable:

* withdrawals
* spot trading
* unnecessary permissions

---

# 24. Testing Requirements

# 24.1 Unit Tests

Coverage:

* indicator calculations
* position sizing
* signal generation
* stop-loss logic

Minimum:

```text
90% coverage
```

---

# 24.2 Integration Tests

Mock:

* Binance REST
* WebSocket feeds

Validate:

* order flow
* restart recovery
* stop-loss recreation

---

# 24.3 Paper Trading

Mandatory before live deployment.

Minimum:

```text
30 days
```

---

# 25. Backtesting Requirements

## Required Features

* realistic fees
* slippage
* funding rates
* latency assumptions
* partial fills

---

# 26. Operational Runbook

## Daily Checks

* exchange connectivity
* websocket health
* funding rates
* open positions
* available margin

---

## Emergency Shutdown Procedure

### Trigger

* abnormal losses
* repeated API failures
* exchange instability

### Actions

1. cancel all orders
2. flatten all positions
3. disable trading
4. send alerts

---

# 27. Monitoring Dashboard

## Required Panels

* account equity
* open positions
* active orders
* current PnL
* strategy state
* funding rate
* recent logs

---

# 28. Telegram Alert Format

## Entry Alert

```text
[ENTRY OPENED]

Symbol: ETH/USDT
Side: LONG
Entry: 2512.5
Qty: 0.43
Stop: 2492.4
Leverage: 3x
```

---

# 29. Configuration Management

## Configurable Parameters

| parameter      | default  |
| -------------- | -------- |
| symbol         | ETH/USDT |
| timeframe      | 15m      |
| sma_period     | 20       |
| entry_band     | 1.5%     |
| stop_loss_pct  | 0.8%     |
| leverage       | 3        |
| risk_per_trade | 0.5%     |

---

# 30. Future Expansion Points

## Planned Extensions

* multi-symbol support
* portfolio risk engine
* adaptive volatility bands
* ATR stop-loss
* regime filters
* trend filters
* AI-assisted analytics
* distributed workers

---

# 31. Success Criteria

## Functional Success

* executes trades correctly
* stop-loss always attached
* restart recovery functional

---

## Operational Success

* 99% uptime
* zero orphaned positions
* deterministic execution

---

# 32. Final Production Constraints

The system MUST:

* never trade without stop-loss
* never process live candles
* never exceed configured leverage
* never duplicate orders
* never ignore restart reconciliation
* never trade if exchange state is inconsistent

---

# END OF DOCUMENT
