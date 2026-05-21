"""
crypto_trader.playbooks — Entry Signal Generators
===================================================
Playbook A: Intraday Snap (6-16h, 0.7% SL, 1.0% TP)
Playbook B: Swing (24-48h, scaled exits, 1.2% SL, trail)
"""

import logging
from typing import Optional, Dict
from enum import Enum

import pandas as pd
import numpy as np

from .regime import MarketRegime, compute_ema, compute_rsi
from .wallet import PositionSide, Playbook

logger = logging.getLogger("crypto_trader.playbooks")


# ── Playbook A: Intraday Snap ──

class PlaybookA:
    """
    6-16 hour hold.
    Entry: 1H pullback to EMA21 within 4H trend + volume + RSI + rejection candle.
    """

    def __init__(
        self,
        sl_pct: float = 0.007,
        tp_pct: float = 0.010,
        time_h: int = 18,
        vol_mult: float = 1.20,
        rsi_lo: float = 40,
        rsi_hi: float = 60,
        ema_period: int = 21,
        min_score: float = 0.60,
    ):
        self.sl_pct = sl_pct
        self.tp_pct = tp_pct
        self.time_h = time_h
        self.vol_mult = vol_mult
        self.rsi_lo = rsi_lo
        self.rsi_hi = rsi_hi
        self.ema_period = ema_period
        self.min_score = min_score

    def evaluate(self, df_1h: pd.DataFrame, regime: MarketRegime) -> Optional[Dict]:
        if len(df_1h) < 30:
            return None

        # Direction filter
        if regime == MarketRegime.TRENDING_UP:
            direction = PositionSide.LONG
        elif regime == MarketRegime.TRENDING_DOWN:
            direction = PositionSide.SHORT
        elif regime == MarketRegime.RANGING:
            return None  # Skip ranging for intraday
        else:
            return None

        df = df_1h.copy()
        df["ema21"] = compute_ema(df["close"], self.ema_period)
        df["rsi"] = compute_rsi(df["close"], 14)
        df["quote_vol_avg20"] = df["quote_volume"].rolling(20).mean()

        curr = df.iloc[-1]
        prev = df.iloc[-2]
        price = curr["close"]

        # Score tracking
        score = 0.0
        checks_passed = 0
        total_checks = 5

        # 1. Volume check (quote volume, not base)
        if curr["quote_volume"] >= curr["quote_vol_avg20"] * self.vol_mult:
            score += 0.20
            checks_passed += 1

        # 2. RSI check
        rsi = curr["rsi"]
        if pd.notna(rsi) and self.rsi_lo <= rsi <= self.rsi_hi:
            score += 0.20
            checks_passed += 1

        # 3. Pullback to EMA21
        ema21 = curr["ema21"]
        if pd.isna(ema21):
            return None

        pullback = False
        if direction == PositionSide.LONG:
            pullback = (curr["low"] <= ema21 * 1.003) or (abs(price - ema21) / price < 0.003)
        else:
            pullback = (curr["high"] >= ema21 * 0.997) or (abs(price - ema21) / price < 0.003)

        if pullback:
            score += 0.20
            checks_passed += 1

        # 4. Rejection candle
        body = abs(curr["close"] - curr["open"])
        rejection = False
        if direction == PositionSide.LONG:
            lower_wick = min(curr["open"], curr["close"]) - curr["low"]
            # Proper engulfing: prev must be bearish
            prev_bearish = prev["close"] < prev["open"]
            bullish_engulfing = (
                prev_bearish and
                curr["open"] < prev["close"] and
                curr["close"] > prev["open"] and
                curr["close"] > curr["open"]
            )
            rejection = (lower_wick >= body * 1.5) or bullish_engulfing
        else:
            upper_wick = curr["high"] - max(curr["open"], curr["close"])
            prev_bullish = prev["close"] > prev["open"]
            bearish_engulfing = (
                prev_bullish and
                curr["open"] > prev["close"] and
                curr["close"] < prev["open"] and
                curr["close"] < curr["open"]
            )
            rejection = (upper_wick >= body * 1.5) or bearish_engulfing

        if rejection:
            score += 0.20
            checks_passed += 1

        # 5. Regime alignment bonus
        if regime in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN):
            score += 0.20
            checks_passed += 1

        if score < self.min_score:
            return None

        # Build setup
        if direction == PositionSide.LONG:
            sl = price * (1 - self.sl_pct)
            tp = price * (1 + self.tp_pct)
        else:
            sl = price * (1 + self.sl_pct)
            tp = price * (1 - self.tp_pct)

        return {
            "playbook": Playbook.INTRADAY,
            "side": direction,
            "entry_price": price,
            "sl_price": sl,
            "tp_price": tp,
            "time_stop_hours": self.time_h,
            "score": round(score, 3),
            "checks": f"{checks_passed}/{total_checks}",
            "reason": f"PlaybookA: EMA21 pullback + vol + RSI({rsi:.1f}) + rejection | score={score:.2f}",
        }


# ── Playbook B: Swing ──

class PlaybookB:
    """
    24-48 hour hold.
    Entry: 4H breakout + 1H retest.
    Scaled exits: 50% at +1%, 25% at +2%, 25% runner trails EMA9.
    """

    def __init__(
        self,
        sl_pct: float = 0.012,
        tp1_pct: float = 0.010,
        tp2_pct: float = 0.020,
        time_h: int = 48,
        vol_mult: float = 1.50,
        body_min: float = 0.012,
        rsi_long_lo: float = 45,
        rsi_long_hi: float = 65,
        rsi_short_lo: float = 35,
        rsi_short_hi: float = 55,
        min_score: float = 0.65,
    ):
        self.sl_pct = sl_pct
        self.tp1_pct = tp1_pct
        self.tp2_pct = tp2_pct
        self.time_h = time_h
        self.vol_mult = vol_mult
        self.body_min = body_min
        self.rsi_long_range = (rsi_long_lo, rsi_long_hi)
        self.rsi_short_range = (rsi_short_lo, rsi_short_hi)
        self.min_score = min_score

    def evaluate(
        self,
        df_1h: pd.DataFrame,
        df_4h: pd.DataFrame,
        regime: MarketRegime,
    ) -> Optional[Dict]:
        if len(df_4h) < 10 or len(df_1h) < 30:
            return None

        if regime not in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN):
            return None

        direction = PositionSide.LONG if regime == MarketRegime.TRENDING_UP else PositionSide.SHORT

        # 4H analysis
        df4 = df_4h.copy()
        df4["rsi"] = compute_rsi(df4["close"], 14)
        df4["quote_vol_avg20"] = df4["quote_volume"].rolling(20).mean()

        c4 = df4.iloc[-1]
        p4 = df4.iloc[-2] if len(df4) > 1 else c4
        body_pct = abs(c4["close"] - c4["open"]) / c4["open"]

        score = 0.0
        checks_passed = 0
        total_checks = 5

        # 1. Breakout body check
        if body_pct >= self.body_min:
            score += 0.20
            checks_passed += 1

        # 2. Volume check
        if c4["quote_volume"] >= c4["quote_vol_avg20"] * self.vol_mult:
            score += 0.20
            checks_passed += 1

        # 3. RSI check
        rsi_4h = c4["rsi"]
        if pd.notna(rsi_4h):
            if direction == PositionSide.LONG and self.rsi_long_range[0] <= rsi_4h <= self.rsi_long_range[1]:
                score += 0.20
                checks_passed += 1
            elif direction == PositionSide.SHORT and self.rsi_short_range[0] <= rsi_4h <= self.rsi_short_range[1]:
                score += 0.20
                checks_passed += 1

        # 4. Breakout direction
        if direction == PositionSide.LONG:
            if not (c4["close"] > c4["open"] and c4["close"] > p4["high"]):
                return None
            if len(df4) < 7:
                logger.warning("PlaybookB: insufficient 4H data for breakout level")
                return None
            breakout_level = df4.iloc[-7:-1]["high"].max()
        else:
            if not (c4["close"] < c4["open"] and c4["close"] < p4["low"]):
                return None
            if len(df4) < 7:
                logger.warning("PlaybookB: insufficient 4H data for breakout level")
                return None
            breakout_level = df4.iloc[-7:-1]["low"].min()

        score += 0.20
        checks_passed += 1

        # 5. 1H retest
        c1 = df_1h.iloc[-1]
        price = c1["close"]

        if direction == PositionSide.LONG:
            # Retest: low touches or dips slightly below breakout level
            near_level = c1["low"] <= breakout_level * 1.001
            if not (near_level and c1["close"] > c1["open"]):
                return None
            sl = price * (1 - self.sl_pct)
            tp1 = price * (1 + self.tp1_pct)
            tp2 = price * (1 + self.tp2_pct)
        else:
            near_level = c1["high"] >= breakout_level * 0.999
            if not (near_level and c1["close"] < c1["open"]):
                return None
            sl = price * (1 + self.sl_pct)
            tp1 = price * (1 - self.tp1_pct)
            tp2 = price * (1 - self.tp2_pct)

        score += 0.20
        checks_passed += 1

        if score < self.min_score:
            return None

        return {
            "playbook": Playbook.SWING,
            "side": direction,
            "entry_price": price,
            "sl_price": sl,
            "tp_levels": [
                {"price": tp1, "pct": 0.50, "hit": False, "label": "TP1"},
                {"price": tp2, "pct": 0.25, "hit": False, "label": "TP2"},
            ],
            "time_stop_hours": self.time_h,
            "score": round(score, 3),
            "checks": f"{checks_passed}/{total_checks}",
            "reason": f"PlaybookB: 4H breakout + 1H retest | RSI(4H)={rsi_4h:.1f} | score={score:.2f}",
        }
