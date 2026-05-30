# Antigravity Trading System - System Requirements Document (SRD)

## 1. Executive Summary
Antigravity is a production-grade, multi-symbol automated futures trading system. It is designed to operate on Binance (for Market Data) and CoinDCX (for Execution), utilizing a highly decoupled, event-sourced architecture to ensure maximum safety, capital preservation, and deterministic execution.

## 2. Multi-Symbol Architecture
Unlike single-instrument bots, Antigravity operates on a dynamic `WATCHLIST` of symbols (e.g., BTCUSDT, ETHUSDT, SOLUSDT). 
- **Portfolio Engine:** Risk is calculated on a per-symbol basis but constrained by global portfolio limits.
- **Concurrent Processing:** The engine evaluates all watchlist symbols on every candle boundary simultaneously.
- **Single Position Rule:** Only one active position allowed *per symbol* in the watchlist.

## 3. Institutional Risk Architecture
The system employs multiple discrete safety engines rather than a monolithic logic block.

### 3.1 Margin Engine (G6)
- **Responsibility:** Validates margin availability and liquidation distance.
- **Rule:** Rejects entries if the margin utilization exceeds the threshold or if the Stop-Loss price is too close to the exchange Liquidation Price.

### 3.2 Leverage Engine & Dynamic Leverage Manager (G7)
- **Responsibility:** Enforces dynamic leverage tier limits and scales leverage based on real-time factors.
- **Rules:**
  - Scales leverage dynamically between `DYNAMIC_LEVERAGE_MIN` (default 5x) and `DYNAMIC_LEVERAGE_MAX` (default 20x) using the `DynamicLeverageManager`.
  - Multiplicative Risk Scaling: Scales leverage down based on volatility (ATR%), account drawdown, and margin ratio.
  - Volatility Halt: Halts trading (0x leverage) if ATR% exceeds extreme threshold.
  - Drawdown Floor: Forces leverage to minimum floor under severe drawdown.
  - Regime adjustment: Boosts leverage in favorable regimes (e.g., Trend Expansion) and cuts it in unfavorable ones (e.g., Mean Reversion, Chop).

### 3.3 Portfolio Risk Engine
- **Responsibility:** Caps total system exposure.
- **Rule:** `max_daily_trades`, `max_margin_ratio` (G4 - halt if > 80%), and global `max_drawdown_pct`.

## 4. Hard Safety Mandates (G1-G10 & F1-F5)
The system MUST adhere to the following strict operational guards:

*   **G1 (Clock Skew):** Reject entries if local↔venue clock drift > 2000ms.
*   **G2 (Velocity):** Max 6 orders per minute per symbol.
*   **G3 (Supervisor):** Halt engine if WebSocket or Position threads die.
*   **G4 (Authoritative Health):** Halt instantly if CoinDCX `margin_ratio_cross` > 80%.
*   **G5 & G9 (Exchange Consistency):** Mandatory sync of internal wallet vs. exchange state on startup and continuously during runtime. Orphaned orders or phantom positions trigger an automatic kill switch.
*   **G6 (Margin Engine):** (See section 3.1)
*   **G7 (Leverage Engine & Dynamic Leverage Manager):** (See section 3.2)
*   **G8 (Order State Machine):** Enforce deterministic order lifecycle; terminal states (FILLED, CANCELLED, REJECTED) are immutable.
*   **G10 (Close Flip-Guard):** Clamps order exit quantity to the actual venue-reported quantity, or skips order execution if the venue is already flat, preventing unintended opposite positions from opening on CoinDCX due to lack of a native reduce-only flag.

### 4.1 Feature-Specific Guards
*   **F1 (Venue-Resident SL):** A resting STOP_MARKET order MUST be placed on the execution venue immediately after entry.
*   **F2 (TDS Accounting):** System models 1% Indian TDS on sell legs.
*   **F3 (Basis Guard):** Entry price MUST be within a specified % (e.g., 0.5%) of the Binance mark price to prevent arb traps.
*   **F4 (Paper Degradation):** Simulated execution must incur spread penalties for realistic testing.

## 5. Execution & State Consistency

### 5.1 Deterministic Order State Machine
Orders follow a strict lifecycle (`NEW` -> `PENDING` -> `FILLED`/`PARTIALLY_FILLED` -> `CANCELLED`/`REJECTED`). State transitions are handled linearly, preventing race conditions where fills and cancellations cross over WebSocket streams.

### 5.2 Event-Sourced Persistence
All domain events (signals, order creations, fills, position changes) are persisted to a PostgreSQL Event Journal. The internal Wallet state is rebuilt sequentially from this journal, allowing "time-travel" debugging and perfect recovery.

### 5.3 Restart Recovery (PRD §18)
On startup, the system MUST:
1. Load internal persisted state (e.g., `mr_state`).
2. Fetch live wallet position from Exchange (via EnhancedFuturesWallet).
3. If a position is open but NO stop_order_id exists on the exchange, IMMEDIATELY recreate the stop-loss order.
4. Synchronize projection databases.
5. Resume strategy processing.
