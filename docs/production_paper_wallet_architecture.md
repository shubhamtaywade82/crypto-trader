# Production-Grade Internal Paper Wallet Architecture

## Scope Clarification
This design is for an **internal simulated paper wallet** used by the bot for strategy testing and shadow execution.
It is not a real exchange custody wallet and does not move real funds.

## Current State Summary
The existing `EnhancedFuturesWallet` in `crypto_trader/wallet.py` is a useful simulation ledger with:

- Position lifecycle management (`open_position`, `partial_close`, `close_position`, `update_positions`)
- Realized and unrealized PnL tracking
- Trailing and time-stop logic
- JSON persistence
- Intra-process locking

This makes it suitable for development and paper testing, but not production-grade simulation.

## Why It Is Not Yet Production-Grade
1. **Weak persistence durability**: single JSON snapshot writes are not atomic and lack robust recovery.
2. **No exchange reconciliation model**: no fills, fees, funding, order lifecycle, or exchange parity controls.
3. **Simplified margin accounting**: exposure simulation is useful but not broker-equivalent.
4. **Global state file**: shared storage across runs/symbols increases collision risk.
5. **No append-only event journal**: limited auditability and replay.
6. **Concurrency scope is local**: lock protects only one process.

## Target Architecture
Promote the wallet from a mutable ledger to a deterministic event-sourced simulator.

### Principles
- **Event-sourced core**: all state changes are immutable events.
- **Deterministic replay**: same events + market data => same state.
- **Exchange-fidelity simulation**: realistic fills, slippage, spread, fees, funding, and liquidation.
- **Operational durability**: atomic, crash-safe persistence with schema versioning.

### Suggested Package Layout
```text
crypto_trader/wallet/
├── engine.py
├── portfolio.py
├── positions.py
├── orders.py
├── execution.py
├── liquidation.py
├── funding.py
├── fees.py
├── slippage.py
├── persistence.py
├── replay.py
├── snapshots.py
├── schemas.py
├── events.py
└── models.py
```

### Core Components
- **Portfolio engine**: account-level balances, margin, equity, and exposure metrics.
- **Position engine**: per-symbol position state (entry, qty, mark, liquidation metrics).
- **Order engine**: lifecycle of market/limit/stop/reduce-only/trailing orders.
- **Execution simulator**: spread/slippage/latency/partial-fill model.
- **Fee engine**: maker/taker charge accrual.
- **Funding engine**: periodic funding credits/debits.
- **Liquidation engine**: maintenance margin checks and liquidation events.
- **Event store + snapshots + replay**: append-only journal with periodic snapshots and deterministic recovery.

## Data & Persistence Recommendations
- Use **`Decimal`** for all money and quantity math.
- Prefer **SQLite (WAL mode)** over raw JSON files for atomic writes and recovery.
- Persist append-only events with schema versioning.

### Event Examples
- `ORDER_CREATED`, `ORDER_FILLED`, `ORDER_CANCELLED`
- `POSITION_OPENED`, `POSITION_UPDATED`, `POSITION_CLOSED`
- `FEE_CHARGED`, `FUNDING_APPLIED`
- `LIQUIDATION`, `RISK_HALT`

### Storage Layout
```text
storage/
├── events/
├── snapshots/
├── reports/
├── metrics/
└── journals/
```

## Minimum Upgrades to Reach Production-Grade Paper Trading
1. Atomic persistence + backup + schema versioning.
2. Append-only event journal and replay pipeline.
3. Fee, funding, spread, and slippage simulation.
4. Namespace state by strategy/account/symbol.
5. Multi-process-safe storage semantics.
6. Liquidation and partial-fill modeling.

## Implementation Roadmap

### Phase 1: Accounting Foundation
- Introduce `Decimal`-based models.
- Add explicit portfolio/position/order schemas.
- Move persistence to SQLite + WAL.

### Phase 2: Realism Layer
- Add spread/slippage/fill latency models.
- Add maker/taker fees and funding accrual.

### Phase 3: Event-Sourcing Layer
- Append-only events.
- Snapshotting and deterministic replay.
- Crash-safe startup recovery.

### Phase 4: Exchange Fidelity
- Partial fill queues and order state transitions.
- Liquidation/maintenance margin simulation.
- Exchange-like execution behavior under volatility.

## Final Classification
Current wallet: **decent simulated wallet** for R&D and paper forward testing.
Target wallet: **production-grade paper trading engine** suitable for reliable strategy validation and future live-execution migration.
