#!/usr/bin/env python3
"""
Binance & CoinDCX Futures Trading System — Standalone Unified Script
===================================================================
A standalone, self-contained automated trading system implementing the
multi-timeframe "SOL 10x Snap" strategy with optional LLM veto/gating:

    - Market Regime classification (trending/ranging/chop)
    - Playbook A: Intraday Snap (pullback to EMA21, volume surge, RSI filter)
    - Playbook B: Swing Breakout (4H breakout + 1H retest + trailing stop)
    - Risk Manager: daily trade/loss cooling limits
    - Ollama LLM Advisor: async AI veto and position sizing modifier
    - Dual WebSocket Feeds: supports Binance WS or CoinDCX WS for real-time price monitoring
    - Interactive Curses TUI dashboard for live monitoring

Usage:
    python3 binance_futures_trading_system.py --loop --tick 300
"""

import os
import ssl
import json
import time
import logging
import curses
import threading
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, List, Optional, Tuple, Literal
from enum import Enum
from pathlib import Path

import pandas as pd
import numpy as np
import requests
import websocket

# Import LLM advisor from package directory
try:
    from crypto_trader.llm_advisor import OllamaAdvisor, LLMAdvice
except ImportError:
    OllamaAdvisor, LLMAdvice = None, None

# ---------------------------------------------------------------------------
# Configuration Constants
# ---------------------------------------------------------------------------

BINANCE_FAPI_BASE = "https://fapi.binance.com"
BINANCE_FAPI_TESTNET = "https://demo-fapi.binance.com"

# Default trading parameters
LEVERAGE = 10
EQUITY_UTILIZATION = 0.50
CATASTROPHIC_SL_PCT = -0.50

# Playbook A - Intraday Snap
A_SL_PCT = 0.007          # 0.7% price stop
A_TP_PCT = 0.010          # 1.0% price target
A_TIME_H = 18             # 18-hour time stop
A_VOL_MULT = 1.20         # Volume surge threshold
A_RSI_LO = 40
A_RSI_HI = 60
A_EMA = 21                # pullback to EMA21

# Playbook B - Swing Breakout
B_SL_PCT = 0.012          # 1.2% price stop
B_TP1_PCT = 0.010         # TP1: Close 50% at +1.0%
B_TP2_PCT = 0.020         # TP2: Close 25% at +2.0%
B_TIME_H = 48             # 48-hour time stop
B_VOL_MULT = 1.50         # Volume surge threshold
B_BODY_MIN = 0.012        # Breakout candle body height
B_RSI_LONG_LO, B_RSI_LONG_HI = 45, 65
B_RSI_SHORT_LO, B_RSI_SHORT_HI = 35, 55
B_TRAIL_EMA = 9           # Trailing stop EMA reference

# Risk Limits
MAX_DAILY_TRADES = 2
MAX_CONSEC_LOSS = 2

# Settings defaults
DEFAULT_SYMBOL = "SOLUSDT"
TF_4H = "4h"
TF_1H = "1h"
LIMIT_4H = 100
LIMIT_1H = 150

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("UnifiedTradingSystem")

DATA_DIR = Path.home() / ".crypto_trader"
DATA_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Enums and Dataclasses
# ---------------------------------------------------------------------------

class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


class PositionSide(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class Signal(Enum):
    LONG = 1
    SHORT = -1
    NEUTRAL = 0


class MarketRegime(Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    CHOP = "CHOP"


class Playbook(Enum):
    INTRADAY = "INTRADAY"
    SWING = "SWING"


@dataclass
class EnhancedPosition:
    symbol: str
    side: PositionSide
    playbook: Playbook
    entry_price: float
    original_quantity: float
    remaining_quantity: float
    notional: float
    margin_used: float
    leverage: int
    open_time: int

    sl_price: float
    tp_levels: List[dict] = field(default_factory=list)  # [{"price": x, "pct": 0.5, "hit": False}, ...]
    trailing_active: bool = False
    trailing_stop_price: Optional[float] = None
    time_stop_hours: int = A_TIME_H

    unrealized_pnl: float = 0.0
    partial_realized_pnl: float = 0.0
    status: Literal["OPEN", "CLOSED"] = "OPEN"
    close_time: Optional[int] = None
    close_price: Optional[float] = None
    close_reason: Optional[str] = None

    def update_pnl(self, mark_price: float):
        if self.status != "OPEN":
            return
        self.notional = self.remaining_quantity * mark_price
        self.margin_used = self.notional / self.leverage
        if self.side == PositionSide.LONG:
            self.unrealized_pnl = (mark_price - self.entry_price) * self.remaining_quantity
        else:
            self.unrealized_pnl = (self.entry_price - mark_price) * self.remaining_quantity

    def to_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side.value
        d["playbook"] = self.playbook.value
        return d


# ---------------------------------------------------------------------------
# Technical Calculations
# ---------------------------------------------------------------------------

def compute_ema(df: pd.DataFrame, period: int) -> pd.Series:
    return df["close"].ewm(span=period, adjust=False).mean()


def compute_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / np.where(loss == 0, 1e-9, loss)
    return 100 - (100 / (1 + rs))


# ---------------------------------------------------------------------------
# Data Feed Client
# ---------------------------------------------------------------------------

class BinanceDataFeed:
    """REST Client for fetching historical market data."""

    def __init__(self, base_url: str = BINANCE_FAPI_BASE):
        self.base_url = base_url

    def get_klines(self, symbol: str, interval: str, limit: int = 100, start_time: int = None, end_time: int = None) -> pd.DataFrame:
        url = f"{self.base_url}/fapi/v1/klines"
        params = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        res = requests.get(url, params=params, timeout=10)
        res.raise_for_status()
        data = res.json()

        df = pd.DataFrame(data, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_base_volume",
            "taker_quote_volume", "ignore"
        ])
        for col in ["open", "high", "low", "close", "volume", "quote_volume"]:
            df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")
        return df

    def get_mark_price(self, symbol: str) -> float:
        url = f"{self.base_url}/fapi/v1/premiumIndex"
        res = requests.get(url, params={"symbol": symbol.upper()}, timeout=10)
        res.raise_for_status()
        return float(res.json().get("markPrice", 0.0))

    def get_funding_rate(self, symbol: str) -> float:
        try:
            url = f"{self.base_url}/fapi/v1/premiumIndex"
            res = requests.get(url, params={"symbol": symbol.upper()}, timeout=10)
            if res.status_code == 200:
                return float(res.json().get("lastFundingRate", 0.0))
        except Exception:
            pass
        return 0.0

    def get_open_interest(self, symbol: str) -> float:
        try:
            url = f"{self.base_url}/fapi/v1/openInterest"
            res = requests.get(url, params={"symbol": symbol.upper()}, timeout=10)
            if res.status_code == 200:
                return float(res.json().get("openInterest", 0.0))
        except Exception:
            pass
        return 0.0

    def get_taker_ratio(self, symbol: str) -> float:
        try:
            # Long/Short Taker Ratio proxy
            url = f"{self.base_url}/futures/data/takerlongshortRatio"
            res = requests.get(url, params={"symbol": symbol.upper(), "period": "5m", "limit": 1}, timeout=10)
            if res.status_code == 200 and len(res.json()) > 0:
                return float(res.json()[0].get("buySellRatio", 1.0))
        except Exception:
            pass
        return 1.0


# ---------------------------------------------------------------------------
# Market Regime Analyzer
# ---------------------------------------------------------------------------

class MarketRegimeAnalyzer:
    """Classifies market structure based on moving averages (EMA200, EMA50)."""

    def analyze(self, df_4h: pd.DataFrame) -> Tuple[MarketRegime, pd.DataFrame]:
        df = df_4h.copy()
        df["ema50"] = compute_ema(df, 50)
        df["ema200"] = compute_ema(df, 200)

        # ADX/ATR proxy for trend strength
        df["hl"] = df["high"] - df["low"]
        df["hc"] = (df["high"] - df["close"].shift(1)).abs()
        df["lc"] = (df["low"] - df["close"].shift(1)).abs()
        df["tr"] = df[["hl", "hc", "lc"]].max(axis=1)
        df["atr"] = df["tr"].rolling(14).mean()
        df["atr_pct"] = df["atr"] / df["close"] * 100

        curr = df.iloc[-1]
        prev = df.iloc[-5]  # Lookback to evaluate slope

        # Regime classification
        ema_gap = (curr["ema50"] - curr["ema200"]) / curr["ema200"] * 100
        atr_pct = curr["atr_pct"]

        if curr["close"] > curr["ema50"] > curr["ema200"] and curr["ema50"] > prev["ema50"]:
            if atr_pct > 1.2 or ema_gap > 1.5:
                regime = MarketRegime.TRENDING_UP
            else:
                regime = MarketRegime.RANGING
        elif curr["close"] < curr["ema50"] < curr["ema200"] and curr["ema50"] < prev["ema50"]:
            if atr_pct > 1.2 or ema_gap < -1.5:
                regime = MarketRegime.TRENDING_DOWN
            else:
                regime = MarketRegime.RANGING
        elif atr_pct < 0.6:
            regime = MarketRegime.CHOP
        else:
            regime = MarketRegime.RANGING

        return regime, df


# ---------------------------------------------------------------------------
# Signal Playbooks
# ---------------------------------------------------------------------------

class PlaybookA_IntradaySnap:
    """Playbook A: Pullback to EMA21 on 1H charts."""

    def evaluate(self, df_1h: pd.DataFrame, regime: MarketRegime) -> Optional[dict]:
        if regime not in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN):
            return None  # Trend-following only

        direction = PositionSide.LONG if regime == MarketRegime.TRENDING_UP else PositionSide.SHORT
        df = df_1h.copy()
        df["ema21"] = compute_ema(df, A_EMA)
        df["rsi"] = compute_rsi(df, 14)
        df["vol_avg"] = df["quote_volume"].rolling(20).mean()

        curr = df.iloc[-1]
        price = curr["close"]

        # Volume filter
        if curr["quote_volume"] < curr["vol_avg"] * A_VOL_MULT:
            return None

        # RSI filter
        if not (A_RSI_LO <= curr["rsi"] <= A_RSI_HI):
            return None

        # Pullback check
        ema = curr["ema21"]
        pullback = False
        if direction == PositionSide.LONG:
            pullback = (curr["low"] <= ema * 1.003) or (abs(price - ema) / price < 0.003)
        else:
            pullback = (curr["high"] >= ema * 0.997) or (abs(price - ema) / price < 0.003)

        if not pullback:
            return None

        # Stop and target calculations
        sl = price * (1 - A_SL_PCT) if direction == PositionSide.LONG else price * (1 + A_SL_PCT)
        tp = price * (1 + A_TP_PCT) if direction == PositionSide.LONG else price * (1 - A_TP_PCT)

        return {
            "playbook": Playbook.INTRADAY,
            "side": direction,
            "entry_price": price,
            "sl_price": sl,
            "tp_levels": [{"price": tp, "pct": 1.0, "hit": False, "label": "TP"}],
            "time_stop_hours": A_TIME_H,
            "reason": f"1H EMA pullback ({price:.2f} near EMA21 {ema:.2f}) | RSI={curr['rsi']:.1f}",
        }


class PlaybookB_Swing:
    """Playbook B: Breakout of 4H swing range + 1H retest confirmation."""

    def evaluate(self, df_1h: pd.DataFrame, df_4h: pd.DataFrame, regime: MarketRegime) -> Optional[dict]:
        if regime not in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN):
            return None

        direction = PositionSide.LONG if regime == MarketRegime.TRENDING_UP else PositionSide.SHORT
        df4 = df_4h.copy()
        df4["rsi"] = compute_rsi(df4, 14)
        df4["vol_avg"] = df4["quote_volume"].rolling(20).mean()

        c4 = df4.iloc[-1]
        p4 = df4.iloc[-2]
        body_pct = abs(c4["close"] - c4["open"]) / c4["open"]

        # Breakout criteria: big candle + high volume
        if body_pct < B_BODY_MIN or c4["quote_volume"] < c4["vol_avg"] * B_VOL_MULT:
            return None

        # Breakout levels lookup
        if direction == PositionSide.LONG:
            breakout_level = df4.iloc[-7:-1]["high"].max()
            if not (c4["close"] > c4["open"] and c4["close"] > p4["high"]):
                return None
        else:
            breakout_level = df4.iloc[-7:-1]["low"].min()
            if not (c4["close"] < c4["open"] and c4["close"] < p4["low"]):
                return None

        # 1H Retest check
        c1 = df_1h.iloc[-1]
        price = c1["close"]
        near_level = False

        if direction == PositionSide.LONG:
            near_level = c1["low"] <= breakout_level * 1.001
            if not (near_level and c1["close"] > c1["open"]):
                return None
            sl = price * (1 - B_SL_PCT)
            tp1 = price * (1 + B_TP1_PCT)
            tp2 = price * (1 + B_TP2_PCT)
        else:
            near_level = c1["high"] >= breakout_level * 0.999
            if not (near_level and c1["close"] < c1["open"]):
                return None
            sl = price * (1 + B_SL_PCT)
            tp1 = price * (1 - B_TP1_PCT)
            tp2 = price * (1 - B_TP2_PCT)

        return {
            "playbook": Playbook.SWING,
            "side": direction,
            "entry_price": price,
            "sl_price": sl,
            "tp_levels": [
                {"price": tp1, "pct": 0.50, "hit": False, "label": "TP1"},
                {"price": tp2, "pct": 0.25, "hit": False, "label": "TP2"},
            ],
            "time_stop_hours": B_TIME_H,
            "reason": f"4H breakout (level {breakout_level:.2f}) + 1H retest validation",
        }


# ---------------------------------------------------------------------------
# Risk Manager
# ---------------------------------------------------------------------------

class RiskManager:
    """Enforces daily drawdowns, consecutive losses, and cool-off thresholds."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.daily_count = 0
        self.consec_losses = 0
        self.last_trade_date = None
        self.cooldown_until = 0

    def record_open(self):
        self.daily_count += 1
        self.last_trade_date = datetime.now().date()

    def record_close(self, realized_pnl: float):
        if realized_pnl < 0:
            self.consec_losses += 1
            if self.consec_losses >= MAX_CONSEC_LOSS:
                self.cooldown_until = time.time() + (30 * 60)  # 30-min cooldown
                logger.warning(f"[RISK] {MAX_CONSEC_LOSS} consecutive losses! Cooling off for 30m.")
        else:
            self.consec_losses = 0

    def can_trade(self) -> Tuple[bool, str]:
        now = time.time()
        today = datetime.now().date()

        if self.last_trade_date != today:
            self.daily_count = 0
            self.consec_losses = 0

        if self.daily_count >= MAX_DAILY_TRADES:
            return False, f"Daily limit reached ({self.daily_count}/{MAX_DAILY_TRADES})"

        if now < self.cooldown_until:
            rem = int(self.cooldown_until - now)
            return False, f"Consecutive loss cooldown active ({rem}s remaining)"

        return True, "OK"


# ---------------------------------------------------------------------------
# Simulated Portfolio Wallet
# ---------------------------------------------------------------------------

class EnhancedFuturesWallet:
    """Event-sourced simulator for tracking balances, positions, fees, and updates."""

    def __init__(self, initial_balance: float = 1_000.0, leverage: int = 10, state_file: str = "wallet_state.json"):
        self.leverage = leverage
        self.equity_utilization = EQUITY_UTILIZATION
        self.state_file = DATA_DIR / state_file
        self.initial_balance = initial_balance
        self.wallet_balance = initial_balance
        self.unrealized_pnl_total = 0.0
        self.realized_pnl_total = 0.0
        self.positions: Dict[str, EnhancedPosition] = {}
        self.on_position_closed: Optional[Callable[[float], None]] = None
        self._load_state()

    @property
    def margin_balance(self) -> float:
        return self.wallet_balance + self.unrealized_pnl_total

    @property
    def available_balance(self) -> float:
        used_margin = sum(p.margin_used for p in self.positions.values() if p.status == "OPEN")
        return max(0.0, self.wallet_balance - used_margin)

    def get_open_position(self, symbol: str) -> Optional[EnhancedPosition]:
        pos = self.positions.get(symbol.upper())
        return pos if (pos and pos.status == "OPEN") else None

    def open_position(self, symbol: str, setup: dict, mark_price: float) -> Optional[EnhancedPosition]:
        symbol = symbol.upper()
        if self.get_open_position(symbol):
            return None

        # sizing calculations
        available = self.available_balance
        margin = available * self.equity_utilization
        if margin <= 0:
            return None

        notional = margin * self.leverage
        qty = notional / mark_price
        side = setup["side"]

        # calculate entry fee (0.04% taker fee standard)
        fee = notional * 0.0004
        self.wallet_balance -= fee
        self.realized_pnl_total -= fee

        pos = EnhancedPosition(
            symbol=symbol,
            side=side,
            playbook=setup["playbook"],
            entry_price=mark_price,
            original_quantity=qty,
            remaining_quantity=qty,
            notional=notional,
            margin_used=margin,
            leverage=self.leverage,
            open_time=int(time.time() * 1000),
            sl_price=setup["sl_price"],
            tp_levels=setup["tp_levels"],
            time_stop_hours=setup.get("time_stop_hours", A_TIME_H),
        )
        self.positions[symbol] = pos
        self._save_state()
        logger.info(f"[OPEN] {symbol} {side.value} @ {mark_price:.2f} | size={qty:.4f} | fee={fee:.4f}")
        return pos

    def partial_close(self, symbol: str, mark_price: float, pct: float, reason: str):
        symbol = symbol.upper()
        pos = self.get_open_position(symbol)
        if not pos:
            return

        qty_to_close = pos.remaining_quantity * pct
        if qty_to_close <= 0:
            return

        # PnL math
        if pos.side == PositionSide.LONG:
            pnl = (mark_price - pos.entry_price) * qty_to_close
        else:
            pnl = (pos.entry_price - mark_price) * qty_to_close

        fee = (qty_to_close * mark_price) * 0.0004
        net_pnl = pnl - fee

        pos.remaining_quantity -= qty_to_close
        pos.partial_realized_pnl += net_pnl
        self.wallet_balance += net_pnl
        self.realized_pnl_total += net_pnl

        logger.info(f"[PARTIAL CLOSE] {symbol} {pct*100:.0f}% @ {mark_price:.2f} | net={net_pnl:.2f} | reason={reason}")

        if pos.remaining_quantity <= 0.0001:
            self.close_position(symbol, mark_price, "FULL_VIA_PARTIAL")
        else:
            self._save_state()

    def close_position(self, symbol: str, mark_price: float, reason: str):
        symbol = symbol.upper()
        pos = self.get_open_position(symbol)
        if not pos:
            return

        qty = pos.remaining_quantity
        if pos.side == PositionSide.LONG:
            pnl = (mark_price - pos.entry_price) * qty
        else:
            pnl = (pos.entry_price - mark_price) * qty

        fee = (qty * mark_price) * 0.0004
        net_pnl = pnl - fee

        self.wallet_balance += net_pnl
        self.realized_pnl_total += net_pnl

        pos.remaining_quantity = 0.0
        pos.status = "CLOSED"
        pos.close_time = int(time.time() * 1000)
        pos.close_price = mark_price
        pos.close_reason = reason

        total_trade_pnl = pos.partial_realized_pnl + net_pnl
        logger.info(f"[CLOSE] {symbol} @ {mark_price:.2f} | total_pnl={total_trade_pnl:.2f} | reason={reason}")

        if self.on_position_closed:
            self.on_position_closed(total_trade_pnl)

        self._save_state()

    def update_positions(self, prices: Dict[str, float], indicators: dict):
        self.unrealized_pnl_total = 0.0
        now_ms = int(time.time() * 1000)

        for symbol, pos in list(self.positions.items()):
            if pos.status != "OPEN":
                continue

            price = prices.get(symbol)
            if not price or price <= 0:
                continue

            pos.update_pnl(price)
            self.unrealized_pnl_total += pos.unrealized_pnl

            # 1. Catastrophic SL
            if pos.margin_used + pos.unrealized_pnl < pos.margin_used * (1 + CATASTROPHIC_SL_PCT):
                self.close_position(symbol, price, "CATASTROPHIC_SL")
                continue

            # 2. Time Stop check
            duration_h = (now_ms - pos.open_time) / (1000 * 60 * 60)
            if duration_h >= pos.time_stop_hours:
                self.close_position(symbol, price, f"TIME_STOP ({duration_h:.1f}h)")
                continue

            # 3. Stop Loss
            if pos.side == PositionSide.LONG and price <= pos.sl_price:
                self.close_position(symbol, price, "STOP_LOSS")
                continue
            elif pos.side == PositionSide.SHORT and price >= pos.sl_price:
                self.close_position(symbol, price, "STOP_LOSS")
                continue

            # 4. Playbook exits
            if pos.playbook == Playbook.INTRADAY:
                # Playbook A uses simple single TP target
                for tp in pos.tp_levels:
                    if not tp["hit"]:
                        if pos.side == PositionSide.LONG and price >= tp["price"]:
                            self.close_position(symbol, price, "TAKE_PROFIT")
                        elif pos.side == PositionSide.SHORT and price <= tp["price"]:
                            self.close_position(symbol, price, "TAKE_PROFIT")
            else:
                # Playbook B Swing Exits (scaled exits + trailing stop)
                # TP1 (50%)
                tp1 = pos.tp_levels[0]
                if not tp1["hit"]:
                    hit = (pos.side == PositionSide.LONG and price >= tp1["price"]) or (pos.side == PositionSide.SHORT and price <= tp1["price"])
                    if hit:
                        tp1["hit"] = True
                        self.partial_close(symbol, price, 0.50, "TP1")
                        # adjust SL to breakeven
                        pos.sl_price = pos.entry_price
                        logger.info(f"[ADJUST SL] {symbol} moved to breakeven ({pos.sl_price:.2f})")
                        continue

                # TP2 (25%)
                tp2 = pos.tp_levels[1]
                if not tp2["hit"]:
                    hit = (pos.side == PositionSide.LONG and price >= tp2["price"]) or (pos.side == PositionSide.SHORT and price <= tp2["price"])
                    if hit:
                        tp2["hit"] = True
                        self.partial_close(symbol, price, 0.50, "TP2")  # 50% of remaining (which is 25% of total)
                        pos.trailing_active = True
                        logger.info(f"[TRAILING ACTIVATE] Trailing stop activated for {symbol}")
                        continue

                # Trailing Stop evaluation (runner 25%)
                if pos.trailing_active:
                    ind = indicators.get(symbol, {})
                    ema9 = ind.get("ema9_1h")
                    if ema9:
                        if pos.side == PositionSide.LONG and price < ema9:
                            self.close_position(symbol, price, f"TRAILING_STOP (crossed below 1H EMA9 {ema9:.2f})")
                        elif pos.side == PositionSide.SHORT and price > ema9:
                            self.close_position(symbol, price, f"TRAILING_STOP (crossed above 1H EMA9 {ema9:.2f})")

    def get_summary(self) -> dict:
        open_pos = [p.to_dict() for p in self.positions.values() if p.status == "OPEN"]
        return {
            "wallet_balance": self.wallet_balance,
            "realized_pnl": self.realized_pnl_total,
            "unrealized_pnl": self.unrealized_pnl_total,
            "margin_balance": self.margin_balance,
            "available": self.available_balance,
            "open_count": len(open_pos),
            "open_positions": open_pos,
        }

    def print_summary(self):
        s = self.get_summary()
        print("\n" + "=" * 65)
        print("  SOL 10× SNAP — WALLET SUMMARY")
        print("=" * 65)
        print(f"  Wallet Balance    : {s['wallet_balance']:.4f} USDT")
        print(f"  Unrealized PnL    : {s['unrealized_pnl']:.4f} USDT")
        print(f"  Realized PnL      : {s['realized_pnl']:.4f} USDT")
        print(f"  Margin Balance    : {s['margin_balance']:.4f} USDT")
        print(f"  Available         : {s['available']:.4f} USDT")
        print(f"  Open Positions    : {s['open_count']}")
        for p in s["open_positions"]:
            margin_pnl_pct = (p['unrealized_pnl'] / p['margin_used'] * 100) if p['margin_used'] > 0 else 0
            print(f"    → {p['symbol']} {p['side']} | Playbook={p['playbook']} | "
                  f"Entry={p['entry_price']:.2f} | RemQty={p['remaining_quantity']:.4f} | "
                  f"U-PnL={p['unrealized_pnl']:.4f} ({margin_pnl_pct:.2f}%)")
        print("=" * 65 + "\n")

    def reset(self):
        self.wallet_balance = self.initial_balance
        self.unrealized_pnl_total = 0.0
        self.realized_pnl_total = 0.0
        self.positions.clear()
        if self.state_file.exists():
            self.state_file.unlink()

    def _save_state(self):
        try:
            state = {
                "wallet_balance": float(self.wallet_balance),
                "realized_pnl": float(self.realized_pnl_total),
                "positions": {s: p.to_dict() for s, p in self.positions.items()},
            }
            self.state_file.write_text(json.dumps(state, indent=2))
        except Exception:
            pass

    def _load_state(self):
        if not self.state_file.exists():
            return
        try:
            state = json.loads(self.state_file.read_text())
            self.wallet_balance = state.get("wallet_balance", self.initial_balance)
            self.realized_pnl_total = state.get("realized_pnl", 0.0)
            for s, pos_dict in state.get("positions", {}).items():
                # rebuild EnhancedPosition dataclass
                pos = EnhancedPosition(
                    symbol=pos_dict["symbol"],
                    side=PositionSide(pos_dict["side"]),
                    playbook=Playbook(pos_dict["playbook"]),
                    entry_price=pos_dict["entry_price"],
                    original_quantity=pos_dict["original_quantity"],
                    remaining_quantity=pos_dict["remaining_quantity"],
                    notional=pos_dict["notional"],
                    margin_used=pos_dict["margin_used"],
                    leverage=pos_dict["leverage"],
                    open_time=pos_dict["open_time"],
                    sl_price=pos_dict["sl_price"],
                    tp_levels=pos_dict["tp_levels"],
                    trailing_active=pos_dict["trailing_active"],
                    trailing_stop_price=pos_dict.get("trailing_stop_price"),
                    time_stop_hours=pos_dict.get("time_stop_hours", A_TIME_H),
                    unrealized_pnl=pos_dict.get("unrealized_pnl", 0.0),
                    partial_realized_pnl=pos_dict.get("partial_realized_pnl", 0.0),
                    status=pos_dict["status"],
                    close_time=pos_dict.get("close_time"),
                    close_price=pos_dict.get("close_price"),
                    close_reason=pos_dict.get("close_reason"),
                )
                self.positions[s] = pos
        except Exception:
            pass


# ---------------------------------------------------------------------------
# WebSocket Pricing Feeds (Binance & CoinDCX alternatives)
# ---------------------------------------------------------------------------

class BinanceWSFeed:
    """Real-time market price parser from Binance futures streams."""

    def __init__(self, symbol: str, testnet: bool = False):
        self.symbol = symbol.lower()
        base_url = "wss://stream.binancefuture.com" if testnet else "wss://fstream.binance.com"
        self.ws_url = f"{base_url}/ws/{self.symbol}@markPrice"
        self.last_price = 0.0
        self.running = False
        self.status = "DISCONNECTED"
        self._thread = None

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            self.last_price = float(data.get("p", 0.0))
            self.status = "CONNECTED"
        except Exception:
            pass

    def _on_error(self, ws, error):
        self.status = f"ERROR: {error}"

    def _on_close(self, ws, close_status_code, close_msg):
        self.status = "CLOSED"

    def _on_open(self, ws):
        self.status = "OPENED"

    def _run(self):
        while self.running:
            try:
                self.status = "CONNECTING..."
                ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open
                )
                ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE}, ping_interval=20, ping_timeout=10)
            except Exception:
                pass
            time.sleep(5)

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        self.status = "STOPPED"


class CoinDCXWSFeed:
    """Real-time pricing feed parser from CoinDCX WebSocket streams (Socket.io v2)."""

    def __init__(self, symbol: str):
        # SOLUSDT -> B-SOL_USDT
        base = symbol.upper().replace("USDT", "")
        self.coindcx_symbol = f"B-{base}_USDT"
        self.last_price = 0.0
        self.running = False
        self.status = "DISCONNECTED"
        self.error_log = ""
        self._thread = None
        self.ws_url = "wss://stream.coindcx.com/socket.io/?EIO=3&transport=websocket"

    def _on_message(self, ws, message):
        try:
            if isinstance(message, str) and message.startswith("42"):
                payload = json.loads(message[2:])
                event_name = payload[0]
                data = payload[1]
                if event_name in ["new-trade", "price-change"] and "p" in data:
                    self.last_price = float(data["p"])
                    self.status = "CONNECTED"
            elif isinstance(message, str) and message.startswith("0"):
                ws.send("40")
            elif isinstance(message, str) and message.startswith("40"):
                join_msg = f'42["join", {{"channelName": "{self.coindcx_symbol}@trades-futures"}}]'
                ws.send(join_msg)
                self.status = "JOINING..."
        except Exception as e:
            self.error_log = f"ParseErr: {str(e)[:15]}"

    def _on_error(self, ws, error):
        self.error_log = f"DCXErr: {str(error)[:20]}"
        self.status = "ERROR"

    def _on_close(self, ws, close_status_code, close_msg):
        self.status = "CLOSED"

    def _on_open(self, ws):
        self.status = "OPENED"
        self.error_log = "Handshaking..."

    def _run(self):
        while self.running:
            try:
                self.status = "CONNECTING..."
                ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open
                )
                ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE}, ping_interval=25, ping_timeout=10)
            except Exception as e:
                self.error_log = f"RunErr: {str(e)[:15]}"
            time.sleep(5)

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        self.status = "STOPPED"


# ---------------------------------------------------------------------------
# Consolidated Trading Engine (Support V2, V3 features, Curses dashboard & TUI)
# ---------------------------------------------------------------------------

class TradingEngine:
    """
    Consolidated Trading Engine.
    Supports technical-only signals (v2) and async LLM advisory/vetoing (v3).
    """

    def __init__(
        self,
        symbol: str = DEFAULT_SYMBOL,
        initial_balance: float = 1_000.0,
        leverage: int = LEVERAGE,
        testnet: bool = False,
        use_coindcx_ws: bool = False,
        use_llm: bool = False,
        llm_host: str = "http://localhost:11434",
        llm_model: str = "qwen3.5:4b",
    ):
        self.symbol = symbol.upper()
        self.data_feed = BinanceDataFeed(
            base_url=BINANCE_FAPI_TESTNET if testnet else BINANCE_FAPI_BASE
        )
        if use_coindcx_ws:
            self.ws_feed = CoinDCXWSFeed(self.symbol)
            logger.info("[ENGINE] Configured for CoinDCX real-time pricing feed")
        else:
            self.ws_feed = BinanceWSFeed(self.symbol, testnet=testnet)
            logger.info("[ENGINE] Configured for Binance real-time pricing feed")

        self.regime_analyzer = MarketRegimeAnalyzer()
        self.playbook_a = PlaybookA_IntradaySnap()
        self.playbook_b = PlaybookB_Swing()
        self.risk_manager = RiskManager()
        self.wallet = EnhancedFuturesWallet(
            initial_balance=initial_balance,
            leverage=leverage,
            state_file=f"wallet_{self.symbol}_standalone.json",
        )
        self.wallet.on_position_closed = self.risk_manager.record_close

        # LLM Gating (v3 specific)
        self.use_llm = use_llm and (OllamaAdvisor is not None)
        self.advisor: Optional[OllamaAdvisor] = None
        self.latest_advice: Optional[LLMAdvice] = None
        self._llm_thread = None

        if self.use_llm:
            self.advisor = OllamaAdvisor(
                host=llm_host,
                model=llm_model,
                timeout=30,
                cache_ttl=1800,
                min_confidence=0.65,
                veto_threshold=-0.70,
            )
            if self.advisor.is_ready():
                logger.info(f"[LLM] Connected to Ollama ({llm_model})")
            else:
                logger.warning("[LLM] Ollama not available. Technical-only mode.")
                self.use_llm = False

    def run_once(self, mark_price: float = None) -> dict:
        try:
            df_4h = self.data_feed.get_klines(self.symbol, TF_4H, limit=LIMIT_4H)
            df_1h = self.data_feed.get_klines(self.symbol, TF_1H, limit=LIMIT_1H)
            if mark_price is None:
                mark_price = self.data_feed.get_mark_price(self.symbol)
            
            # Fetch additional context for LLM if active
            if self.use_llm:
                df_15m = self.data_feed.get_klines(self.symbol, "15m", limit=150)
                df_5m = self.data_feed.get_klines(self.symbol, "5m", limit=150)
                funding_rate = self.data_feed.get_funding_rate(self.symbol)
                oi_data = self.data_feed.get_open_interest(self.symbol)
                taker_ratio = self.data_feed.get_taker_ratio(self.symbol)
        except Exception as e:
            logger.error(f"Data fetch failed: {e}")
            return self.wallet.get_summary()

        # 2. Analyze Regime
        regime, df_4h = self.regime_analyzer.analyze(df_4h)
        logger.info(f"Market Regime: {regime.value}")

        # 3. Start LLM advice fetch (NON-BLOCKING) if enabled
        if self.use_llm and self.advisor:
            open_positions = [
                p.to_dict() for p in self.wallet.positions.values() if p.status == "OPEN"
            ]
            if self._llm_thread is None or not self._llm_thread.is_alive():
                self._llm_thread = self.advisor.get_advice_async(
                    symbol=self.symbol,
                    df_5m=df_5m,
                    df_15m=df_15m,
                    df_1h=df_1h,
                    df_4h=df_4h,
                    regime=regime.value,
                    regime_score=0.5,
                    mark_price=mark_price,
                    funding_rate=funding_rate,
                    oi_delta=0.0,
                    taker_ratio=taker_ratio,
                    open_positions=open_positions,
                )

        # 4. Risk check
        can_trade, risk_reason = self.risk_manager.can_trade()

        # 5. Prepare indicators for trailing stops
        df_1h["ema9"] = compute_ema(df_1h, B_TRAIL_EMA)
        latest_ema9_1h = float(df_1h["ema9"].iloc[-1]) if not pd.isna(df_1h["ema9"].iloc[-1]) else None
        indicators = {
            self.symbol: {
                "ema9_1h": latest_ema9_1h,
                "timestamp_ms": int(time.time() * 1000),
            }
        }

        # 6. Update existing positions
        self.wallet.update_positions({self.symbol: mark_price}, indicators)

        # 7. Evaluate new positions
        open_pos = self.wallet.get_open_position(self.symbol)
        if not open_pos and can_trade:
            setup = None
            setup = self.playbook_a.evaluate(df_1h, regime)
            if setup:
                logger.info(f"[PLAYBOOK A] Raw signal: {setup['side'].value} | {setup['reason']}")

            if not setup:
                setup = self.playbook_b.evaluate(df_1h, df_4h, regime)
                if setup:
                    logger.info(f"[PLAYBOOK B] Raw signal: {setup['side'].value} | {setup['reason']}")

            # 8. LLM FILTER Integration (from v3)
            if setup and self.use_llm and self.advisor:
                self.latest_advice = self.advisor.get_last_advice()
                if self.latest_advice:
                    tech_side = "LONG" if setup["side"] == PositionSide.LONG else "SHORT"
                    allow, reason = self.advisor.should_allow_trade(self.latest_advice, tech_side)
                    logger.info(f"[LLM FILTER] {reason}")

                    if not allow:
                        logger.warning(f"[LLM VETO] Trade BLOCKED: {reason}")
                        setup = None
                    else:
                        base_margin = self.wallet.available_balance * self.wallet.equity_utilization
                        adjusted_margin = self.advisor.adjust_position_size(self.latest_advice, base_margin)
                        if adjusted_margin <= 0:
                            logger.warning("[LLM] Position size reduced to zero by risk filter")
                            setup = None
                        elif adjusted_margin < base_margin:
                            original_util = self.wallet.equity_utilization
                            self.wallet.equity_utilization = adjusted_margin / self.wallet.available_balance
                            logger.info(f"[LLM] Size adjusted: {base_margin:.2f} -> {adjusted_margin:.2f} USDT")
                            pos = self.wallet.open_position(self.symbol, setup, mark_price)
                            if pos:
                                self.risk_manager.record_open()
                            self.wallet.equity_utilization = original_util
                            setup = None  # Already handled

            if setup:
                pos = self.wallet.open_position(self.symbol, setup, mark_price)
                if pos:
                    self.risk_manager.record_open()

        # 9. Summary
        summary = self.wallet.get_summary()
        if self.use_llm and self.advisor:
            self.latest_advice = self.advisor.get_last_advice()
            if self.latest_advice:
                summary["llm"] = self.latest_advice.to_dict()
            else:
                summary["llm"] = {"status": "pending or unavailable"}
        return summary

    def run_loop(self, interval_seconds: int = 300, max_iterations: int = None):
        self.ws_feed.start()

        def main_tui(stdscr):
            curses.curs_set(0)
            stdscr.nodelay(True)
            iteration = 0
            last_tick_time = 0

            while True:
                now = time.time()

                if now - last_tick_time >= interval_seconds:
                    mark_price = self.ws_feed.last_price if self.ws_feed.last_price > 0 else None
                    self.run_once(mark_price=mark_price)
                    last_tick_time = now
                    iteration += 1

                current_price = self.ws_feed.last_price
                if current_price > 0:
                    self.wallet.update_positions({self.symbol: current_price}, {})

                s = self.wallet.get_summary()

                # Curses drawing
                stdscr.clear()
                height, width = stdscr.getmaxyx()

                price_str = f"{current_price:.4f}" if current_price > 0 else self.ws_feed.status
                header = f"=== {self.symbol} LTP: {price_str} | {datetime.now().strftime('%H:%M:%S')} ==="
                stdscr.addstr(0, 0, header.ljust(width), curses.A_BOLD | curses.A_REVERSE)

                stdscr.addstr(2, 0, f"Wallet Balance    : {s['wallet_balance']:.4f} USDT", curses.A_BOLD)
                stdscr.addstr(3, 0, f"Realized PnL      : {s['realized_pnl']:.4f} USDT")
                stdscr.addstr(4, 0, f"Unrealized PnL    : {s['unrealized_pnl']:.4f} USDT", curses.A_GREEN if s['unrealized_pnl'] > 0 else curses.A_RED if s['unrealized_pnl'] < 0 else curses.A_NORMAL)
                stdscr.addstr(5, 0, f"Margin Balance    : {s['margin_balance']:.4f} USDT")
                stdscr.addstr(6, 0, f"Available         : {s['available']:.4f} USDT")

                can_trade, risk_reason = self.risk_manager.can_trade()
                stdscr.addstr(8, 0, f"Risk Status: {'ALLOW' if can_trade else 'HALTED'} | Reason: {risk_reason}")

                # AI advice panel (from v3)
                if self.use_llm and self.latest_advice:
                    stdscr.addstr(10, 0, "AI Advisor Details:", curses.A_UNDERLINE)
                    stdscr.addstr(11, 2, f"Sentiment: {self.latest_advice.sentiment_score:+.2f} | Confidence: {self.latest_advice.confidence:.2f}")
                    stdscr.addstr(12, 2, f"Veto Reason: {self.latest_advice.veto_reason if self.latest_advice.veto_reason else 'None'}")
                    if self.latest_advice.key_factors:
                        stdscr.addstr(13, 2, f"Key Factors: {', '.join(self.latest_advice.key_factors[:3])}")

                stdscr.addstr(15, 0, f"Open Positions: {s['open_count']}", curses.A_UNDERLINE)
                row = 16
                for p in s["open_positions"]:
                    margin_pnl_pct = (p['unrealized_pnl'] / p['margin_used'] * 100) if p['margin_used'] > 0 else 0
                    stdscr.addstr(row, 2, f"• {p['symbol']} {p['side']} | Entry: {p['entry_price']:.2f} | PnL: {p['unrealized_pnl']:.2f} ({margin_pnl_pct:.2f}%) | Playbook: {p['playbook']}")
                    row += 1

                time_to_next = max(0, int(interval_seconds - (now - last_tick_time)))
                stdscr.addstr(height-2, 0, f"Next strategy tick in {time_to_next}s | Iteration: {iteration} | Press 'q' to quit")
                stdscr.refresh()

                if max_iterations and iteration >= max_iterations:
                    break

                try:
                    key = stdscr.getch()
                    if key == ord('q'):
                        return
                except Exception:
                    pass
                time.sleep(0.2)

        try:
            curses.wrapper(main_tui)
        except KeyboardInterrupt:
            logger.info("Engine loop stopped by keyboard interrupt")
        except Exception as e:
            logger.error(f"TUI error: {e}")
            self.run_once()
            self.wallet.print_summary()
        finally:
            self.ws_feed.stop()

    def backtest(self, days: int = 14) -> pd.DataFrame:
        """Walk-forward technical backtest over historical candles."""
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - (days * 24 * 60 * 60 * 1000)

        logger.info(f"[BACKTEST] Fetching {days} days of data for {self.symbol}...")
        df_1h = self.data_feed.get_klines(self.symbol, TF_1H, start_time=start_ms, end_time=end_ms, limit=1000)
        df_4h = self.data_feed.get_klines(self.symbol, TF_4H, start_time=start_ms, end_time=end_ms, limit=1000)

        if len(df_1h) < 50:
            raise ValueError("Insufficient historical kline data for backtest.")

        self.wallet.reset()
        self.risk_manager.reset()

        records = []
        for i in range(50, len(df_1h)):
            window_1h = df_1h.iloc[:i].copy()
            window_4h = window_1h.iloc[::4].copy()
            if len(window_4h) < 10:
                continue

            price = float(window_1h["close"].iloc[-1])
            timestamp = window_1h["close_time"].iloc[-1]

            regime, _ = self.regime_analyzer.analyze(window_4h)

            window_1h["ema9"] = compute_ema(window_1h, B_TRAIL_EMA)
            ema9 = float(window_1h["ema9"].iloc[-1]) if not pd.isna(window_1h["ema9"].iloc[-1]) else None
            indicators = {self.symbol: {"ema9_1h": ema9, "timestamp_ms": int(timestamp.timestamp() * 1000)}}

            self.wallet.update_positions({self.symbol: price}, indicators)
            can_trade, _ = self.risk_manager.can_trade()

            if not self.wallet.get_open_position(self.symbol) and can_trade:
                setup = self.playbook_a.evaluate(window_1h, regime)
                if not setup:
                    setup = self.playbook_b.evaluate(window_1h, window_4h, regime)
                if setup:
                    pos = self.wallet.open_position(self.symbol, setup, price)
                    if pos:
                        self.risk_manager.record_open()

            records.append({
                "timestamp": timestamp,
                "price": price,
                "regime": regime.value,
                "wallet": self.wallet.wallet_balance,
                "margin": self.wallet.margin_balance,
                "open": len([p for p in self.wallet.positions.values() if p.status == "OPEN"]),
            })

        return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# CLI Command Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Standalone SOL 10x Snap Trading System (Consolidated v1/v2/v3)")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL, help="Trading pair")
    parser.add_argument("--balance", type=float, default=1_000.0, help="Initial balance")
    parser.add_argument("--leverage", type=int, default=LEVERAGE, help="Leverage multiplier")
    parser.add_argument("--testnet", action="store_true", help="Use testnet")
    parser.add_argument("--coindcx-ws", action="store_true", help="Use CoinDCX WS feed instead of Binance")
    parser.add_argument("--no-llm", action="store_true", help="Disable Ollama LLM advisor layer")
    parser.add_argument("--llm-host", default="http://localhost:11434", help="Ollama host URL")
    parser.add_argument("--llm-model", default="qwen3.5:4b", help="Ollama model name")
    parser.add_argument("--backtest-days", type=int, default=None, help="Backtest historical data for N days")
    parser.add_argument("--loop", action="store_true", help="Start TUI strategy execution loop")
    parser.add_argument("--tick", type=int, default=300, help="Strategy check interval in seconds")
    args = parser.parse_args()

    engine = TradingEngine(
        symbol=args.symbol,
        initial_balance=args.balance,
        leverage=args.leverage,
        testnet=args.testnet,
        use_coindcx_ws=args.coindcx_ws,
        use_llm=not args.no_llm,
        llm_host=args.llm_host,
        llm_model=args.llm_model,
    )

    if args.backtest_days:
        logger.info(f"Starting historical backtest for {args.backtest_days} days...")
        df = engine.backtest(days=args.backtest_days)
        print(df.tail(20).to_string(index=False))
        engine.wallet.print_summary()
    elif args.loop:
        logger.info("Starting consolidated TUI loop... Press Ctrl+C or 'q' inside TUI to quit.")
        engine.run_loop(interval_seconds=args.tick)
    else:
        summary = engine.run_once()
        engine.wallet.print_summary()
        print("\nSummary JSON:")
        print(json.dumps(summary, indent=2, default=str))
