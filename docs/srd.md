## Direct decision

Treat this as a **single-leg mean-reversion futures bot**, not a grid bot.

Your draft has three conceptual problems:

1. **It is not a grid strategy.**
   A real grid bot places multiple resting orders at fixed price intervals. Your logic places one market entry and one exit condition.

2. **It mixes mean reversion and breakout language.**
   “Price drops below SMA → long” is mean reversion, not breakout.

3. **It is not production-safe yet.**
   It needs candle-close timing, exact position handling, protective orders, precision checks, and restart safety.

---

## Critical corrections

* **Do not hard-code API keys.** Use environment variables.
* **Do not trade on the forming candle.** Use the last closed candle.
* **Do not rely on `schedule.every(15).minutes`.** Run on candle boundaries.
* **Do not assume `fetch_positions()` returns the same structure across exchanges.** Parse signed position size carefully.
* **Do not use a fixed funding threshold blindly.** Compare funding cost against expected edge and holding time.
* **Do not open entries without a hard stop-loss.** Attach stop-loss immediately after fill.
* **Do not call this “breakout” logic.** It is mean reversion.

---

## Corrected architecture text

### 1. System setup

Use a dedicated server, not a laptop.

Install dependencies:

```bash
pip install ccxt pandas python-dotenv
```

Store credentials in environment variables:

```bash
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
```

Enable only the permissions you need:

* Futures trading
* Read-only account access
* IP restriction if your exchange supports it

---

### 2. Strategy definition

This bot is a **mean-reversion futures system** for `ETH/USDT`:

* Compute a 20-period SMA on the **last closed candle**
* If price is below SMA by a configured band, open a long
* If price is above SMA by a configured band, open a short
* Place a **hard stop-loss immediately after entry**
* Exit manually when price reverts back to SMA
* Block new entries when funding becomes expensive

---

### 3. Corrected Python template

```python
#!/usr/bin/env python3
import os
import time
import logging
from typing import Optional, Tuple

import ccxt
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# =====================================================================
# CONFIG
# =====================================================================
API_KEY = os.environ["BINANCE_API_KEY"]
API_SECRET = os.environ["BINANCE_API_SECRET"]

SYMBOL = "ETH/USDT"
TIMEFRAME = "15m"
SMA_PERIOD = 20

ENTRY_BAND = 0.015          # 1.5% away from SMA
STOP_LOSS_PCT = 0.008       # 0.8% stop-loss from entry
MAX_FUNDING_RATE = 0.0005   # 0.05% per interval
FIXED_QTY_ETH = 0.10        # replace with risk-based sizing in production

LEVERAGE = 3

# =====================================================================
# LOGGING
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("eth_mean_reversion_bot")

# =====================================================================
# EXCHANGE INIT
# =====================================================================
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {
        "defaultType": "future",
        "adjustForTimeDifference": True,
    }
})

exchange.load_markets()


# =====================================================================
# HELPERS
# =====================================================================
def set_leverage() -> None:
    market = exchange.market(SYMBOL)
    if not market:
        raise RuntimeError(f"Market not found: {SYMBOL}")

    try:
        exchange.set_leverage(LEVERAGE, SYMBOL)
        logger.info("Leverage set to %s on %s", LEVERAGE, SYMBOL)
    except Exception as exc:
        logger.warning("Could not set leverage: %s", exc)


def fetch_candles(limit: int = 200) -> pd.DataFrame:
    bars = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=limit)
    if not bars:
        raise RuntimeError("No OHLCV data returned")

    df = pd.DataFrame(
        bars,
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.astype(
        {
            "open": "float64",
            "high": "float64",
            "low": "float64",
            "close": "float64",
            "volume": "float64",
        }
    )
    return df


def last_closed_candle(df: pd.DataFrame) -> pd.Series:
    # Last row may still be forming; use the previous candle.
    if len(df) < SMA_PERIOD + 2:
        raise RuntimeError("Not enough candles to compute SMA safely")
    return df.iloc[-2]


def compute_sma(df: pd.DataFrame, period: int = SMA_PERIOD) -> float:
    sma_series = df["close"].rolling(period).mean()
    sma = float(sma_series.iloc[-2])  # closed candle only
    if pd.isna(sma):
        raise RuntimeError("SMA is NaN")
    return sma


def fetch_position_amt() -> float:
    """
    Returns signed position size:
    > 0 = long
    < 0 = short
    = 0 = flat
    """
    try:
        positions = exchange.fetch_positions([SYMBOL])
    except Exception as exc:
        raise RuntimeError(f"Unable to fetch positions: {exc}")

    for pos in positions:
        if pos.get("symbol") != SYMBOL:
            continue

        info = pos.get("info") or {}
        if "positionAmt" in info:
            return float(info["positionAmt"])

        contracts = pos.get("contracts")
        side = (pos.get("side") or "").lower()
        if contracts is not None:
            qty = float(contracts)
            if side == "short":
                return -qty
            return qty

    return 0.0


def fetch_funding_rate() -> Optional[float]:
    try:
        data = exchange.fetch_funding_rate(SYMBOL)
        rate = data.get("fundingRate")
        return float(rate) if rate is not None else None
    except Exception as exc:
        logger.warning("Funding rate unavailable: %s", exc)
        return None


def funding_allowed(rate: Optional[float]) -> bool:
    if rate is None:
        return True
    return abs(rate) <= MAX_FUNDING_RATE


def place_market_entry(side: str, amount: float) -> dict:
    amount = float(exchange.amount_to_precision(SYMBOL, amount))
    if amount <= 0:
        raise ValueError("Order amount must be > 0")

    order = exchange.create_order(
        symbol=SYMBOL,
        type="market",
        side=side,
        amount=amount,
    )
    return order


def get_fill_price(order: dict) -> float:
    for key in ("average", "price"):
        val = order.get(key)
        if val:
            return float(val)

    ticker = exchange.fetch_ticker(SYMBOL)
    last = ticker.get("last")
    if last is None:
        raise RuntimeError("Unable to resolve fill price")
    return float(last)


def place_stop_loss(exit_side: str, amount: float, stop_price: float) -> dict:
    """
    Binance Futures stop-market order.
    Reduce-only prevents accidental reversal.
    """
    amount = float(exchange.amount_to_precision(SYMBOL, amount))
    stop_price = float(exchange.price_to_precision(SYMBOL, stop_price))

    params = {
        "stopPrice": stop_price,
        "reduceOnly": True,
        "workingType": "MARK_PRICE",
    }

    order = exchange.create_order(
        symbol=SYMBOL,
        type="STOP_MARKET",
        side=exit_side,
        amount=amount,
        price=None,
        params=params,
    )
    return order


def cancel_open_orders() -> None:
    try:
        orders = exchange.fetch_open_orders(SYMBOL)
        for order in orders:
            try:
                exchange.cancel_order(order["id"], SYMBOL)
            except Exception as exc:
                logger.warning("Could not cancel order %s: %s", order.get("id"), exc)
    except Exception as exc:
        logger.warning("Could not fetch open orders for cancel: %s", exc)


def close_position(position_amt: float) -> None:
    if position_amt == 0:
        return

    side = "sell" if position_amt > 0 else "buy"
    amount = abs(position_amt)
    amount = float(exchange.amount_to_precision(SYMBOL, amount))

    exchange.create_order(
        symbol=SYMBOL,
        type="market",
        side=side,
        amount=amount,
        params={"reduceOnly": True},
    )
    logger.info("Closed position via market %s of %s", side, amount)


def sleep_until_next_candle() -> None:
    interval = 15 * 60  # 15m
    now = int(time.time())
    sleep_for = interval - (now % interval) + 2
    time.sleep(sleep_for)


# =====================================================================
# STRATEGY LOOP
# =====================================================================
def run_cycle() -> None:
    logger.info("Running strategy cycle")

    df = fetch_candles()
    candle = last_closed_candle(df)
    sma = compute_sma(df)
    price = float(candle["close"])
    position_amt = fetch_position_amt()
    fr = fetch_funding_rate()

    logger.info(
        "Price=%.4f | SMA=%.4f | Position=%.6f | Funding=%s",
        price,
        sma,
        position_amt,
        f"{fr:.6f}" if fr is not None else "N/A",
    )

    if position_amt == 0:
        if not funding_allowed(fr):
            logger.info("Funding too high. Skipping new entries.")
            return

        lower_trigger = sma * (1 - ENTRY_BAND)
        upper_trigger = sma * (1 + ENTRY_BAND)

        if price <= lower_trigger:
            logger.info("Oversold signal. Opening long.")
            order = place_market_entry("buy", FIXED_QTY_ETH)
            fill = get_fill_price(order)
            stop_price = fill * (1 - STOP_LOSS_PCT)
            place_stop_loss("sell", FIXED_QTY_ETH, stop_price)
            logger.info("Long opened at %.4f | SL=%.4f", fill, stop_price)

        elif price >= upper_trigger:
            logger.info("Overbought signal. Opening short.")
            order = place_market_entry("sell", FIXED_QTY_ETH)
            fill = get_fill_price(order)
            stop_price = fill * (1 + STOP_LOSS_PCT)
            place_stop_loss("buy", FIXED_QTY_ETH, stop_price)
            logger.info("Short opened at %.4f | SL=%.4f", fill, stop_price)

        else:
            logger.info("No entry signal.")
        return

    # Manage open position
    if position_amt > 0 and price >= sma:
        logger.info("Long reverted to SMA. Closing position.")
        cancel_open_orders()
        close_position(position_amt)

    elif position_amt < 0 and price <= sma:
        logger.info("Short reverted to SMA. Closing position.")
        cancel_open_orders()
        close_position(position_amt)

    else:
        logger.info("Position open. No exit yet.")


def main() -> None:
    set_leverage()
    logger.info("Bot initialised for %s on %s", SYMBOL, TIMEFRAME)

    while True:
        try:
            run_cycle()
        except Exception as exc:
            logger.exception("Cycle failed: %s", exc)

        sleep_until_next_candle()


if __name__ == "__main__":
    main()
```

---

## What this version fixes

* Uses **environment variables** instead of hard-coded secrets
* Uses the **last closed candle**, not a partially formed one
* Aligns execution to **candle boundaries**
* Handles **signed position size**
* Adds a **hard stop-loss immediately after entry**
* Blocks entries when **funding is too expensive**
* Cancels open orders before flattening
* Removes the false “breakout” framing

---

## Important design gap still remaining

This is still **not a true grid bot**.

If you want a real grid, the next layer is:

* multiple buy levels below price
* multiple sell levels above price
* inventory tracking
* per-level take profit
* re-centering logic when price drifts

This template is better classified as **mean-reversion futures automation**.

---

## Production checkpoints

Before live deployment, validate these:

1. **Precision and min-notional rules**

   * `amount_to_precision`
   * `price_to_precision`
   * exchange minimum order size

2. **Restart safety**

   * persist open state locally or in DB
   * reconcile open positions and open orders on startup

3. **Order failure handling**

   * retry logic
   * cancel-replace on protective order failure

4. **Risk sizing**

   * replace fixed quantity with risk-based sizing
   * cap max daily loss
   * cap max simultaneous exposure

5. **Exchange-specific behavior**

   * Binance hedge mode vs one-way mode
   * `STOP_MARKET` support and `reduceOnly` behavior
   * funding rate API availability

If needed, I can turn this into a **proper grid bot** or a **risk-based production version with state persistence and restart recovery**.

Binance’s USDⓈ-M futures order endpoint is `POST /fapi/v1/order`; `positionSide` is required in hedge mode, `reduceOnly` cannot be sent in hedge mode, and market orders require `quantity`. Conditional orders can use `stopPrice`, and `workingType` is supported. That is why the implementation below assumes **one-way mode** and attaches a **reduce-only stop** immediately after entry. ([Binance Developer Center][1])

## Production version: mean-reversion futures bot with restart recovery

```python
#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, Tuple

import ccxt
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# =============================================================================
# CONFIG
# =============================================================================
SYMBOL = "ETH/USDT"
TIMEFRAME = "15m"
SMA_PERIOD = 20

# Mean-reversion triggers
ENTRY_BAND = 0.015          # 1.5% away from SMA
EXIT_BAND = 0.000           # exit at SMA touch
STOP_LOSS_PCT = 0.008       # 0.8% hard stop

# Risk
RISK_PER_TRADE = 0.005      # 0.5% of futures equity
MAX_FUNDING_RATE = 0.0005   # 0.05% per funding interval
LEVERAGE = 3
ONE_WAY_MODE = True         # this code assumes one-way mode

# Persistence
STATE_FILE = Path(os.getenv("BOT_STATE_FILE", "bot_state.json"))

# Exchange credentials
API_KEY = os.environ["BINANCE_API_KEY"]
API_SECRET = os.environ["BINANCE_API_SECRET"]

# =============================================================================
# LOGGING
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("eth_mean_reversion_bot")

# =============================================================================
# EXCHANGE
# =============================================================================
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {
        "defaultType": "future",
        "adjustForTimeDifference": True,
    },
})

exchange.load_markets()


# =============================================================================
# STATE
# =============================================================================
@dataclass
class BotState:
    last_closed_candle_ts: Optional[int] = None
    last_entry_side: Optional[str] = None           # "long" / "short"
    last_entry_price: Optional[float] = None
    last_entry_qty: Optional[float] = None
    stop_order_id: Optional[str] = None


def load_state() -> BotState:
    if not STATE_FILE.exists():
        return BotState()
    try:
        data = json.loads(STATE_FILE.read_text())
        return BotState(**data)
    except Exception as exc:
        logger.warning("Could not load state file. Starting fresh: %s", exc)
        return BotState()


def save_state(state: BotState) -> None:
    STATE_FILE.write_text(json.dumps(asdict(state), indent=2, sort_keys=True))


# =============================================================================
# UTILITIES
# =============================================================================
def set_leverage() -> None:
    try:
        exchange.set_leverage(LEVERAGE, SYMBOL)
        logger.info("Leverage set to %s", LEVERAGE)
    except Exception as exc:
        logger.warning("Could not set leverage: %s", exc)


def fetch_candles(limit: int = 200) -> pd.DataFrame:
    bars = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=limit)
    if not bars:
        raise RuntimeError("No OHLCV returned")

    df = pd.DataFrame(
        bars,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.astype(
        {
            "open": "float64",
            "high": "float64",
            "low": "float64",
            "close": "float64",
            "volume": "float64",
        }
    )
    return df


def last_closed_candle(df: pd.DataFrame) -> pd.Series:
    if len(df) < SMA_PERIOD + 2:
        raise RuntimeError("Not enough candles for stable SMA")
    return df.iloc[-2]  # last row may still be forming


def compute_sma(df: pd.DataFrame, period: int = SMA_PERIOD) -> float:
    sma = df["close"].rolling(period).mean().iloc[-2]
    if pd.isna(sma):
        raise RuntimeError("SMA is NaN")
    return float(sma)


def fetch_position_amt() -> float:
    """
    Signed amount:
      > 0 long
      < 0 short
      = 0 flat
    """
    positions = exchange.fetch_positions([SYMBOL])
    for pos in positions:
        if pos.get("symbol") != SYMBOL:
            continue

        info = pos.get("info") or {}
        if "positionAmt" in info:
            return float(info["positionAmt"])

        contracts = pos.get("contracts")
        if contracts is not None:
            qty = float(contracts)
            side = (pos.get("side") or "").lower()
            return -qty if side == "short" else qty

    return 0.0


def fetch_entry_price_from_position() -> Optional[float]:
    positions = exchange.fetch_positions([SYMBOL])
    for pos in positions:
        if pos.get("symbol") != SYMBOL:
            continue
        if pos.get("entryPrice") is not None:
            try:
                return float(pos["entryPrice"])
            except Exception:
                pass
        info = pos.get("info") or {}
        if info.get("entryPrice") not in (None, "", "0"):
            try:
                return float(info["entryPrice"])
            except Exception:
                pass
    return None


def fetch_funding_rate() -> Optional[float]:
    try:
        data = exchange.fetch_funding_rate(SYMBOL)
        rate = data.get("fundingRate")
        return float(rate) if rate is not None else None
    except Exception as exc:
        logger.warning("Funding rate unavailable: %s", exc)
        return None


def funding_allowed(rate: Optional[float]) -> bool:
    if rate is None:
        return True
    return abs(rate) <= MAX_FUNDING_RATE


def fetch_usdt_equity() -> float:
    balance = exchange.fetch_balance()
    for key in ("USDT", "BUSD"):
        entry = balance.get(key) or {}
        for field in ("total", "free", "used"):
            val = entry.get(field)
            if val is not None:
                try:
                    return float(val)
                except Exception:
                    continue

    total = (balance.get("total") or {}).get("USDT")
    if total is not None:
        return float(total)

    raise RuntimeError("Could not resolve USDT equity from balance")


def calc_position_size_usdt_risk(entry_price: float, stop_price: float) -> float:
    """
    Risk-based sizing:
      risk_capital = equity * RISK_PER_TRADE
      qty = risk_capital / abs(entry - stop)
    Then cap notional to a reasonable size.
    """
    equity = fetch_usdt_equity()
    risk_capital = equity * RISK_PER_TRADE
    stop_distance = abs(entry_price - stop_price)

    if stop_distance <= 0:
        raise ValueError("Invalid stop distance")

    qty = risk_capital / stop_distance

    # Cap notional to leverage-adjusted equity ceiling
    max_notional = equity * LEVERAGE
    qty_cap = max_notional / entry_price
    qty = min(qty, qty_cap)

    qty = float(exchange.amount_to_precision(SYMBOL, qty))
    if qty <= 0:
        raise ValueError("Calculated quantity is invalid")

    market = exchange.market(SYMBOL)
    min_qty = ((market.get("limits") or {}).get("amount") or {}).get("min")
    if min_qty is not None and qty < float(min_qty):
        raise ValueError(f"Calculated quantity {qty} below exchange minimum {min_qty}")

    return qty


def place_market_order(side: str, amount: float) -> dict:
    amount = float(exchange.amount_to_precision(SYMBOL, amount))
    if amount <= 0:
        raise ValueError("Amount must be positive")

    # RESULT gives filled details for market orders on Binance Futures.
    return exchange.create_order(
        SYMBOL,
        "market",
        side,
        amount,
        None,
        params={"newOrderRespType": "RESULT"},
    )


def place_stop_market(exit_side: str, amount: float, stop_price: float) -> dict:
    amount = float(exchange.amount_to_precision(SYMBOL, amount))
    stop_price = float(exchange.price_to_precision(SYMBOL, stop_price))

    params = {
        "stopPrice": stop_price,
        "reduceOnly": True,
        "workingType": "MARK_PRICE",
        "newOrderRespType": "RESULT",
    }

    return exchange.create_order(
        SYMBOL,
        "STOP_MARKET",
        exit_side,
        amount,
        None,
        params=params,
    )


def cancel_open_orders() -> None:
    orders = exchange.fetch_open_orders(SYMBOL)
    for order in orders:
        try:
            exchange.cancel_order(order["id"], SYMBOL)
        except Exception as exc:
            logger.warning("Cancel failed for %s: %s", order.get("id"), exc)


def close_position(position_amt: float) -> None:
    if position_amt == 0:
        return
    side = "sell" if position_amt > 0 else "buy"
    amount = abs(position_amt)
    exchange.create_order(
        SYMBOL,
        "market",
        side,
        float(exchange.amount_to_precision(SYMBOL, amount)),
        None,
        params={"reduceOnly": True, "newOrderRespType": "RESULT"},
    )


def find_open_stop_order_id() -> Optional[str]:
    try:
        orders = exchange.fetch_open_orders(SYMBOL)
    except Exception:
        return None

    for order in orders:
        order_type = str(order.get("type", "")).upper()
        if "STOP" in order_type:
            return str(order.get("id"))
    return None


def reconcile_on_startup(state: BotState) -> BotState:
    """
    Rebuild protection if the process restarted while a position was open.
    """
    position_amt = fetch_position_amt()
    open_stop_id = find_open_stop_order_id()

    if position_amt == 0:
        state.stop_order_id = None
        state.last_entry_side = None
        state.last_entry_price = None
        state.last_entry_qty = None
        save_state(state)
        return state

    if open_stop_id:
        state.stop_order_id = open_stop_id
        save_state(state)
        return state

    entry_price = fetch_entry_price_from_position()
    if entry_price is None:
        raise RuntimeError("Open position exists but entry price could not be resolved")

    qty = abs(position_amt)
    if position_amt > 0:
        stop_price = entry_price * (1 - STOP_LOSS_PCT)
        exit_side = "sell"
        state.last_entry_side = "long"
    else:
        stop_price = entry_price * (1 + STOP_LOSS_PCT)
        exit_side = "buy"
        state.last_entry_side = "short"

    stop_order = place_stop_market(exit_side, qty, stop_price)
    state.stop_order_id = str(stop_order.get("id"))
    state.last_entry_price = entry_price
    state.last_entry_qty = qty
    save_state(state)

    logger.info(
        "Recreated missing stop-loss: side=%s qty=%.6f stop=%.4f",
        exit_side, qty, stop_price
    )
    return state


# =============================================================================
# STRATEGY LOGIC
# =============================================================================
def run_cycle(state: BotState) -> BotState:
    df = fetch_candles()
    candle = last_closed_candle(df)
    sma = compute_sma(df)
    close = float(candle["close"])
    candle_ts = int(pd.Timestamp(candle["timestamp"]).timestamp())

    position_amt = fetch_position_amt()
    funding = fetch_funding_rate()

    logger.info(
        "Candle=%s Close=%.4f SMA=%.4f Pos=%.6f Funding=%s",
        candle["timestamp"],
        close,
        sma,
        position_amt,
        f"{funding:.6f}" if funding is not None else "N/A",
    )

    # Avoid duplicate decisions on the same closed candle.
    if state.last_closed_candle_ts == candle_ts:
        logger.info("Candle already processed. Skipping.")
        return state

    # Always reconcile protection first.
    if position_amt != 0 and not state.stop_order_id:
        state = reconcile_on_startup(state)

    # Flat: look for entries
    if position_amt == 0:
        if not funding_allowed(funding):
            logger.info("Funding too high. Entry blocked.")
            state.last_closed_candle_ts = candle_ts
            save_state(state)
            return state

        lower_trigger = sma * (1 - ENTRY_BAND)
        upper_trigger = sma * (1 + ENTRY_BAND)

        if close <= lower_trigger:
            entry_side = "buy"
            intended_side = "long"
            entry_price_estimate = close
            stop_price = entry_price_estimate * (1 - STOP_LOSS_PCT)
            qty = calc_position_size_usdt_risk(entry_price_estimate, stop_price)

            logger.info("Long entry signal. qty=%.6f", qty)
            entry_order = place_market_order(entry_side, qty)

            fill_price = float(entry_order.get("average") or entry_order.get("price") or close)
            stop_price = fill_price * (1 - STOP_LOSS_PCT)
            stop_order = place_stop_market("sell", qty, stop_price)

            state.last_entry_side = intended_side
            state.last_entry_price = fill_price
            state.last_entry_qty = qty
            state.stop_order_id = str(stop_order.get("id"))
            state.last_closed_candle_ts = candle_ts
            save_state(state)

            logger.info("Long opened at %.4f | SL=%.4f", fill_price, stop_price)
            return state

        if close >= upper_trigger:
            entry_side = "sell"
            intended_side = "short"
            entry_price_estimate = close
            stop_price = entry_price_estimate * (1 + STOP_LOSS_PCT)
            qty = calc_position_size_usdt_risk(entry_price_estimate, stop_price)

            logger.info("Short entry signal. qty=%.6f", qty)
            entry_order = place_market_order(entry_side, qty)

            fill_price = float(entry_order.get("average") or entry_order.get("price") or close)
            stop_price = fill_price * (1 + STOP_LOSS_PCT)
            stop_order = place_stop_market("buy", qty, stop_price)

            state.last_entry_side = intended_side
            state.last_entry_price = fill_price
            state.last_entry_qty = qty
            state.stop_order_id = str(stop_order.get("id"))
            state.last_closed_candle_ts = candle_ts
            save_state(state)

            logger.info("Short opened at %.4f | SL=%.4f", fill_price, stop_price)
            return state

        logger.info("No entry signal.")
        state.last_closed_candle_ts = candle_ts
        save_state(state)
        return state

    # In position: exit on mean reversion
    if position_amt > 0 and close >= sma:
        logger.info("Long reverted to SMA. Flattening.")
        cancel_open_orders()
        close_position(position_amt)
        state = BotState(last_closed_candle_ts=candle_ts)
        save_state(state)
        return state

    if position_amt < 0 and close <= sma:
        logger.info("Short reverted to SMA. Flattening.")
        cancel_open_orders()
        close_position(position_amt)
        state = BotState(last_closed_candle_ts=candle_ts)
        save_state(state)
        return state

    logger.info("Position open. Holding.")
    state.last_closed_candle_ts = candle_ts
    save_state(state)
    return state


def sleep_until_next_candle() -> None:
    interval_seconds = 15 * 60
    now = int(time.time())
    sleep_for = interval_seconds - (now % interval_seconds) + 2
    time.sleep(sleep_for)


def main() -> None:
    set_leverage()

    state = load_state()
    try:
        state = reconcile_on_startup(state)
    except Exception as exc:
        logger.error("Startup reconcile failed: %s", exc)
        raise

    logger.info("Bot started for %s on %s", SYMBOL, TIMEFRAME)

    while True:
        try:
            state = run_cycle(state)
        except Exception as exc:
            logger.exception("Cycle failed: %s", exc)
        sleep_until_next_candle()


if __name__ == "__main__":
    main()
```

## What this version fixes

* Uses **last closed candle** only
* Uses **risk-based position sizing**
* Saves and reloads **state** across restarts
* Rebuilds a missing **stop-loss** after a crash/restart
* Blocks entries when **funding** is too expensive
* Keeps the bot aligned to **candle boundaries**, not an arbitrary timer
* Assumes **one-way mode**, which matches the `reduceOnly` stop-loss flow on Binance Futures ([Binance Developer Center][1])

## One important correction

This is still **not a grid bot**. It is a **mean-reversion futures bot** with production controls.

A true grid bot needs:

* multiple layered entry orders
* multiple take-profit layers
* inventory tracking
* re-centering logic
* per-grid risk caps

This code is the right foundation if your goal is **safe live automation** first.

## Next hardening step

Add a lightweight database table for:

* order ids
* fill prices
* stop ids
* daily realized PnL
* max daily loss lockout

That gives you proper auditability and prevents the bot from re-entering after a loss streak.

[1]: https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order "New Order | Binance Open Platform"

Yes, but only partially.

The PRD currently treats leverage as a **risk ceiling**, not as a **core portfolio/margin engine**. That is correct for a safe V1, but incomplete for a professional futures system.

Current leverage considerations already included:

* configurable leverage
* hard leverage cap
* leverage enforcement via API
* leverage-aware position sizing ceiling
* one-way mode assumptions
* liquidation avoidance intent
* futures-specific margin handling
* reduce-only exits
* funding-rate protection

But several critical leverage mechanics are still missing.

# Missing leverage requirements

## 1. Initial Margin vs Maintenance Margin

The PRD currently does not model:

```text
Initial Margin
Maintenance Margin
Liquidation Threshold
Margin Ratio
```

This is dangerous because futures risk is nonlinear under leverage.

The bot must continuously compute:

```text
margin_ratio =
maintenance_margin / wallet_balance
```

And halt new entries when unsafe.

---

# 2. Liquidation Distance Engine

Current stop-loss logic is insufficient.

With leverage, liquidation may occur BEFORE stop-loss execution during volatility spikes.

The system must enforce:

```text
minimum_liq_distance >= configured_threshold
```

Example:

```text
liquidation_price_distance >= 2x stop_distance
```

Otherwise reject trade.

---

# 3. Dynamic Leverage Tiers

Binance Futures uses leverage brackets.

Higher position sizes reduce max allowable leverage.

The bot must fetch:

* leverage brackets
* notional tiers
* maintenance margin rates

And validate:

```text
requested_position_size <= allowed_tier
```

---

# 4. Cross vs Isolated Margin

The PRD assumes isolated margin implicitly but never formalizes it.

This MUST be explicit.

Recommended:

```text
Margin Mode = ISOLATED
```

Reason:

* safer
* deterministic liquidation boundaries
* prevents portfolio contamination

Cross margin in autonomous systems is dangerous unless portfolio-level risk engine exists.

---

# 5. Real Leverage Monitoring

Configured leverage != actual leverage.

Actual leverage changes dynamically due to:

* unrealized pnl
* equity fluctuation
* partial closes
* averaging
* funding

System must compute:

```text
effective_leverage =
position_notional / account_equity
```

Continuously.

---

# 6. Max Portfolio Exposure

Current PRD only limits per-trade risk.

Need global exposure limits:

```text
max_total_notional
max_symbol_exposure
max_margin_utilization
```

Example:

```text
max_margin_utilization = 25%
```

This is mandatory in leveraged futures systems.

---

# 7. ADL Risk

Binance perpetuals include:

* liquidation engine
* ADL (Auto-Deleveraging)

The bot should monitor:

* ADL quantile estimation
* excessive leverage states

And avoid trading during elevated ADL risk.

---

# 8. Funding + Leverage Interaction

Funding under leverage compounds.

Actual funding cost:

```text
funding_cost =
position_notional * funding_rate
```

Not wallet balance.

At 10x leverage:

```text
0.05% funding ≈ 0.5% wallet impact
```

This must be modeled explicitly.

---

# 9. Volatility-Adaptive Leverage

Fixed leverage is suboptimal.

Professional systems reduce leverage during:

* high ATR
* high realized volatility
* news events
* funding spikes

Recommended:

| Regime             | Leverage   |
| ------------------ | ---------- |
| Low volatility     | 3x         |
| Medium volatility  | 2x         |
| High volatility    | 1x         |
| Extreme volatility | No trading |

---

# 10. Exchange Liquidation Buffer

Must enforce:

```text
stop_loss_price must be far from liquidation_price
```

Minimum recommended:

```text
stop_distance_to_liq >= 3%
```

Otherwise reject entry.

---

# Correct leverage architecture

The bot should evolve into:

```text
Signal Engine
    ↓
Risk Engine
    ↓
Margin Engine
    ↓
Leverage Engine
    ↓
Execution Engine
```

Currently the PRD merges:

* risk
* leverage
* margin

into one layer.

That is insufficient for professional perpetual futures trading.

# What should be added to the PRD

Add these modules:

## New Modules

### Margin Engine

Responsibilities:

* margin calculations
* liquidation distance
* maintenance margin monitoring
* isolated/cross validation
* margin utilization

### Leverage Engine

Responsibilities:

* leverage tier validation
* effective leverage tracking
* volatility-adjusted leverage
* ADL monitoring
* notional cap enforcement

---

# Recommended production leverage defaults

For a fully automated ETH futures mean-reversion bot:

| Parameter                | Recommended      |
| ------------------------ | ---------------- |
| Margin Mode              | Isolated         |
| Default Leverage         | 2x               |
| Hard Max                 | 5x               |
| Max Margin Usage         | 25%              |
| Risk Per Trade           | 0.25–0.5%        |
| Min Liquidation Distance | 3x stop distance |
| Funding Cutoff           | 0.03–0.05%       |
| Daily Loss Lockout       | 3%               |

---

# Critical observation

Your current strategy type matters heavily here.

Mean reversion + high leverage is structurally dangerous because:

* price can continue deviating
* liquidation risk increases exponentially
* volatility clusters against the position

Trend-following tolerates leverage better than mean reversion.

For autonomous mean-reversion futures systems:

```text
Low leverage + tight risk + isolated margin
```

is the correct architecture.

Not:

```text
10x–20x aggressive averaging
```

That eventually blows up regardless of win rate.

No.

Based on the architecture you described earlier for the repo and the typical structure of the current `crypto-trader` implementation, it does **not** yet contain the full production-grade leverage/margin/risk architecture from the PRD.

It has parts of it.

# What your current bot likely already has

From the earlier discussions and repo structure, your bot already appears to include:

* Binance/CCXT exchange integration
* Futures trading support
* Strategy execution
* Signal engine
* AI-assisted analysis
* Risk management basics
* SL/TP logic
* WebSocket feeds
* Technical indicators
* Position tracking
* Telegram alerts
* Multi-timeframe analysis
* Strategy orchestration
* Backtesting direction
* Config-driven architecture
* Some event-driven behavior

That already puts it ahead of most retail bots.

---

# What it DOES NOT fully have yet

These are the major missing production futures components.

---

# 1. Dedicated Margin Engine ❌

You currently do not appear to have a separate layer for:

* maintenance margin
* initial margin
* liquidation calculations
* isolated margin validation
* margin utilization
* liquidation buffers

This is critical for leveraged futures systems.

---

# 2. Dedicated Leverage Engine ❌

Missing:

* leverage bracket awareness
* dynamic leverage tiers
* effective leverage tracking
* volatility-adjusted leverage
* ADL monitoring
* leverage utilization limits

Most bots incorrectly assume:

```text id="0jv6g9"
configured leverage == actual leverage
```

That is false in perpetual futures.

---

# 3. Restart Reconciliation Engine ⚠️

You have partial architecture concepts around orchestration, but not a complete deterministic reconciliation system.

Missing guarantees:

* orphan stop recovery
* open order reconstruction
* exchange state rebuild
* duplicate execution prevention after restart

This is one of the hardest production problems.

---

# 4. Exchange State Consistency Layer ❌

Professional systems maintain:

```text id="3zcjq5"
desired_state
vs
actual_exchange_state
```

And continuously reconcile them.

Your current bot appears execution-oriented, not reconciliation-oriented.

---

# 5. Deterministic Order State Machine ❌

Missing explicit order lifecycle tracking:

```text id="qccpud"
NEW
PARTIALLY_FILLED
FILLED
CANCEL_PENDING
CANCELED
REJECTED
EXPIRED
```

Without this:

* race conditions happen
* duplicate orders happen
* phantom positions happen

---

# 6. Portfolio-Level Risk Engine ❌

Missing:

* total notional exposure
* margin utilization caps
* portfolio VaR
* correlated exposure control
* symbol concentration limits

---

# 7. Exchange Failure Recovery ❌

Likely missing:

* websocket sequence recovery
* stale stream detection
* order acknowledgement validation
* retry deduplication
* circuit breakers
* degraded-mode operation

---

# 8. True Persistent State Layer ⚠️

You probably have config/state caching, but not a true durable trading ledger.

Need:

* trades table
* orders table
* executions table
* reconciliation snapshots
* risk snapshots
* audit logs

---

# 9. Funding-Aware Position Engine ❌

Most retail bots ignore:

* funding drag
* carry cost
* leverage-amplified funding impact

Your current system likely does too.

---

# 10. Liquidation Distance Protection ❌

Critical missing safety:

```text id="8lzwo9"
stop loss distance < liquidation distance
```

must NEVER happen.

Most retail futures bots fail here.

---

# 11. Real Production Observability ❌

Missing:

* Prometheus metrics
* health endpoints
* structured telemetry
* execution latency tracking
* heartbeat monitoring
* system degradation alerts

---

# 12. Risk Lockout System ⚠️

Probably partially missing:

* daily drawdown halts
* consecutive loss lockout
* exchange anomaly shutdown
* volatility shutdown
* kill switch automation

---

# 13. True Grid Engine ❌

Your repo also does not appear to contain a true professional grid system.

A true grid requires:

* inventory management
* layered orders
* dynamic recentering
* hedge-aware fills
* realized inventory pnl accounting

Most bots marketed as “grid bots” are not real inventory engines.

---

# Current maturity assessment

## Your bot is approximately

| Layer                                  | Status    |
| -------------------------------------- | --------- |
| Retail bot                             | Surpassed |
| Advanced retail bot                    | Yes       |
| Semi-professional system               | Partially |
| Institutional-grade execution system   | No        |
| Fully resilient futures trading engine | No        |

---

# Biggest architectural weakness currently

Your current architecture appears strategy-centric.

Professional systems are:

```text id="x22s4x"
exchange-state-centric
```

That distinction is massive.

Retail bot mindset:

```text id="6o16bj"
signal -> order
```

Professional mindset:

```text id="a0hz4u"
exchange state
-> reconciliation
-> risk state
-> margin state
-> execution permissions
-> order
```

---

# Most important thing missing

This:

```text id="v4qkh8"
deterministic exchange reconciliation
```

Without it:

* crashes become dangerous
* websocket drops become dangerous
* partial fills become dangerous
* stop-loss disappearance becomes catastrophic

This is where most automated futures systems fail in live trading.

---

# Your actual next milestone

Not:

* more indicators
* more AI
* more strategies

But:

```text id="x9fc0s"
production execution infrastructure
```

Specifically:

1. Exchange state engine
2. Reconciliation engine
3. Margin/leverage engine
4. Durable persistence
5. Deterministic order state machine
6. Kill-switch framework
7. Portfolio risk engine

Those transform a “smart trading bot” into a real trading system.
