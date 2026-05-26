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

from .regime import MarketRegime, compute_ema, compute_rsi, compute_atr
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
        min_score: float = 0.75,
    ):
        self.sl_pct = sl_pct
        self.tp_pct = tp_pct
        self.time_h = time_h
        self.vol_mult = vol_mult
        self.rsi_lo = rsi_lo
        self.rsi_hi = rsi_hi
        self.ema_period = ema_period
        self.min_score = min_score

    def evaluate(self, df_1h: pd.DataFrame, regime: MarketRegime, structure: Optional[Dict] = None) -> Optional[Dict]:
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

        # SMC confirmation gates (from MarketStructureAnalyzer output)
        smc_bonus = 0.0
        if structure:
            current_price = float(price)
            recent_bos = structure.get("recent_bos")
            recent_sweep = structure.get("recent_sweep")
            unmitigated_obs = structure.get("unmitigated_order_blocks", [])
            unmitigated_fvgs = structure.get("unmitigated_fvgs", [])

            if direction == PositionSide.LONG:
                if recent_bos and recent_bos["type"] == "BULLISH" and recent_bos.get("candles_ago", 999) <= 10:
                    smc_bonus += 0.15
                if recent_sweep and recent_sweep["type"] == "BULLISH" and recent_sweep.get("candles_ago", 999) <= 5:
                    smc_bonus += 0.15
                for ob in unmitigated_obs:
                    if ob["type"] == "BULLISH" and float(ob["low"]) <= current_price <= float(ob["high"]):
                        smc_bonus += 0.15
                        break
                for fvg in unmitigated_fvgs:
                    if fvg["type"] == "BULLISH" and float(fvg["bottom"]) > current_price:
                        smc_bonus += 0.10
                        break
            else:
                if recent_bos and recent_bos["type"] == "BEARISH" and recent_bos.get("candles_ago", 999) <= 10:
                    smc_bonus += 0.15
                if recent_sweep and recent_sweep["type"] == "BEARISH" and recent_sweep.get("candles_ago", 999) <= 5:
                    smc_bonus += 0.15
                for ob in unmitigated_obs:
                    if ob["type"] == "BEARISH" and float(ob["low"]) <= current_price <= float(ob["high"]):
                        smc_bonus += 0.15
                        break
                for fvg in unmitigated_fvgs:
                    if fvg["type"] == "BEARISH" and float(fvg["top"]) < current_price:
                        smc_bonus += 0.10
                        break

        score += smc_bonus

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
            "reason": f"PlaybookA: EMA21 pullback + vol + RSI({rsi:.1f}) + rejection | score={score:.2f} (smc={smc_bonus:.2f})",
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
        min_score: float = 0.75,
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
        structure: Optional[Dict] = None,
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

        # SMC confirmation gates (from MarketStructureAnalyzer output)
        smc_bonus = 0.0
        if structure:
            current_price = float(price)
            recent_bos = structure.get("recent_bos")
            unmitigated_obs = structure.get("unmitigated_order_blocks", [])
            pa_signals = structure.get("price_action_signals", [])

            if direction == PositionSide.LONG:
                if recent_bos and recent_bos["type"] == "BULLISH" and recent_bos.get("candles_ago", 999) <= 10:
                    smc_bonus += 0.20
                for ob in unmitigated_obs:
                    if (ob["type"] == "BULLISH"
                            and breakout_level > 0
                            and abs(float(ob["high"]) - breakout_level) / breakout_level < 0.005):
                        smc_bonus += 0.15
                        break
                for sig in pa_signals:
                    if sig["candle"] == "completed" and sig["pattern"] == "BULLISH_ENGULFING":
                        smc_bonus += 0.10
                        break
            else:
                if recent_bos and recent_bos["type"] == "BEARISH" and recent_bos.get("candles_ago", 999) <= 10:
                    smc_bonus += 0.20
                for ob in unmitigated_obs:
                    if (ob["type"] == "BEARISH"
                            and breakout_level > 0
                            and abs(float(ob["low"]) - breakout_level) / breakout_level < 0.005):
                        smc_bonus += 0.15
                        break
                for sig in pa_signals:
                    if sig["candle"] == "completed" and sig["pattern"] == "BEARISH_ENGULFING":
                        smc_bonus += 0.10
                        break

        score += smc_bonus

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
            "reason": f"PlaybookB: 4H breakout + 1H retest | RSI(4H)={rsi_4h:.1f} | score={score:.2f} (smc={smc_bonus:.2f})",
        }


# ── Playbook Ares: Mean Reversion ──

class PlaybookAres:
    """
    Mean Reversion strategy using Bollinger Bands and RSI.
    Timeframe: 15m.
    Entry: Price touches/crosses band + RSI exhaustion.
    R:R: 1:2.3.
    """

    def __init__(
        self,
        bb_period: int = 20,
        bb_std: float = 2.0,
        rsi_period: int = 14,
        rsi_overbought: float = 70,
        rsi_oversold: float = 30,
        sl_pct: float = 0.015,
        tp_pct: float = 0.0345,  # 1.5% * 2.3 = 3.45%
        min_score: float = 0.60,
    ):
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.rsi_period = rsi_period
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        self.sl_pct = sl_pct
        self.tp_pct = tp_pct
        self.min_score = min_score

    def evaluate(self, df_15m: pd.DataFrame, adx_1h: Optional[float] = None) -> Optional[Dict]:
        if len(df_15m) < self.bb_period + 5:
            return None

        # Trend filter: Mean reversion works best when ADX is low (< 25)
        if adx_1h is not None and adx_1h > 25:
            return None

        df = df_15m.copy()
        df["sma"] = df["close"].rolling(self.bb_period).mean()
        df["std"] = df["close"].rolling(self.bb_period).std()
        df["upper_band"] = df["sma"] + (df["std"] * self.bb_std)
        df["lower_band"] = df["sma"] - (df["std"] * self.bb_std)
        df["rsi"] = compute_rsi(df["close"], self.rsi_period)

        latest = df.iloc[-1]
        prev = df.iloc[-2]
        price = latest["close"]

        score = 0.0
        direction = None

        # LONG: Price touches/crosses lower band + RSI oversold
        if latest["low"] <= latest["lower_band"] and latest["rsi"] < self.rsi_oversold:
            if prev["close"] > prev["lower_band"]:  # Confirm crossing into/at band
                direction = PositionSide.LONG
                score = (self.rsi_oversold - latest["rsi"]) / self.rsi_oversold + 0.5
        
        # SHORT: Price touches/crosses upper band + RSI overbought
        elif latest["high"] >= latest["upper_band"] and latest["rsi"] > self.rsi_overbought:
            if prev["close"] < prev["upper_band"]:
                direction = PositionSide.SHORT
                score = (latest["rsi"] - self.rsi_overbought) / (100 - self.rsi_overbought) + 0.5

        if not direction or score < self.min_score:
            return None

        # Stop and target calculations
        if direction == PositionSide.LONG:
            sl = price * (1 - self.sl_pct)
            tp = price * (1 + self.tp_pct)
        else:
            sl = price * (1 + self.sl_pct)
            tp = price * (1 - self.tp_pct)

        return {
            "playbook": Playbook.INTRADAY,  # Treating Ares as Intraday for wallet logic
            "side": direction,
            "entry_price": price,
            "sl_price": sl,
            "tp_price": tp,
            "time_stop_hours": 24,
            "score": round(min(score, 1.0), 3),
            "reason": f"PlaybookAres: BB touch + RSI({latest['rsi']:.1f}) | score={score:.2f}",
        }


# ── Reversal-candle helpers (shared by reversal-style playbooks) ──

def _is_bullish_reversal(prev: pd.Series, curr: pd.Series) -> bool:
    """Bullish engulfing OR bullish pinbar (long lower wick) on `curr`."""
    body = abs(curr["close"] - curr["open"])
    rng = curr["high"] - curr["low"]
    lower_wick = min(curr["open"], curr["close"]) - curr["low"]
    upper_wick = curr["high"] - max(curr["open"], curr["close"])

    prev_bearish = prev["close"] < prev["open"]
    bullish_engulfing = (
        prev_bearish
        and curr["close"] > curr["open"]
        and curr["open"] <= prev["close"]
        and curr["close"] > prev["open"]
    )
    bullish_pinbar = (
        rng > 0
        and lower_wick >= 1.5 * body
        and upper_wick <= 0.5 * lower_wick
        and lower_wick >= 0.4 * rng
    )
    return bool(bullish_engulfing or bullish_pinbar)


def _is_bearish_reversal(prev: pd.Series, curr: pd.Series) -> bool:
    """Bearish engulfing OR bearish pinbar (long upper wick) on `curr`."""
    body = abs(curr["close"] - curr["open"])
    rng = curr["high"] - curr["low"]
    lower_wick = min(curr["open"], curr["close"]) - curr["low"]
    upper_wick = curr["high"] - max(curr["open"], curr["close"])

    prev_bullish = prev["close"] > prev["open"]
    bearish_engulfing = (
        prev_bullish
        and curr["close"] < curr["open"]
        and curr["open"] >= prev["close"]
        and curr["close"] < prev["open"]
    )
    bearish_pinbar = (
        rng > 0
        and upper_wick >= 1.5 * body
        and lower_wick <= 0.5 * upper_wick
        and upper_wick >= 0.4 * rng
    )
    return bool(bearish_engulfing or bearish_pinbar)


# ── Playbook VolExhaust: Volatility Exhaustion (strict mean reversion) ──

class PlaybookVolExhaust:
    """
    Volatility-exhaustion fade (Strategy A). Stricter sibling of PlaybookAres.
    Timeframe: 15m.
    Entry: close outside a 3-SD Bollinger Band + volume anomaly (>2x avg) +
           RSI extreme (<20 / >80) + a reversal candle (engulfing/pinbar).
    Exit: target the band mean (20-SMA); SL = 1.5*ATR beyond the extreme wick.
    """

    def __init__(
        self,
        bb_period: int = 20,
        bb_std: float = 3.0,
        rsi_period: int = 14,
        rsi_oversold: float = 20.0,
        rsi_overbought: float = 80.0,
        vol_mult: float = 2.0,
        atr_period: int = 14,
        atr_sl_mult: float = 1.5,
        time_h: int = 18,
        adx_max: float = 35.0,
        min_score: float = 0.60,
    ):
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.vol_mult = vol_mult
        self.atr_period = atr_period
        self.atr_sl_mult = atr_sl_mult
        self.time_h = time_h
        self.adx_max = adx_max
        self.min_score = min_score

    def evaluate(self, df_15m: pd.DataFrame, adx_1h: Optional[float] = None) -> Optional[Dict]:
        if len(df_15m) < self.bb_period + self.atr_period + 5:
            return None

        # Strong trends invalidate a fade — skip when ADX is very high.
        if adx_1h is not None and adx_1h > self.adx_max:
            return None

        df = df_15m.copy()
        df["sma"] = df["close"].rolling(self.bb_period).mean()
        df["std"] = df["close"].rolling(self.bb_period).std()
        df["upper_band"] = df["sma"] + df["std"] * self.bb_std
        df["lower_band"] = df["sma"] - df["std"] * self.bb_std
        df["rsi"] = compute_rsi(df["close"], self.rsi_period)
        df["atr"] = compute_atr(df, self.atr_period)
        df["vol_avg20"] = df["quote_volume"].rolling(self.bb_period).mean()

        curr = df.iloc[-1]
        prev = df.iloc[-2]
        price = float(curr["close"])
        mean = float(curr["sma"])
        atr = float(curr["atr"])
        rsi = float(curr["rsi"]) if pd.notna(curr["rsi"]) else 50.0

        if pd.isna(mean) or pd.isna(atr) or atr <= 0 or pd.isna(curr["std"]) or curr["std"] <= 0:
            return None

        # Volume anomaly
        vol_avg = float(curr["vol_avg20"]) if pd.notna(curr["vol_avg20"]) else 0.0
        vol_ratio = (float(curr["quote_volume"]) / vol_avg) if vol_avg > 0 else 0.0
        if vol_ratio < self.vol_mult:
            return None

        direction = None
        # LONG: close below lower 3-SD band + RSI oversold + bullish reversal
        if curr["close"] < curr["lower_band"] and rsi < self.rsi_oversold and _is_bullish_reversal(prev, curr):
            direction = PositionSide.LONG
            rsi_extremity = (self.rsi_oversold - rsi) / self.rsi_oversold
        # SHORT: close above upper 3-SD band + RSI overbought + bearish reversal
        elif curr["close"] > curr["upper_band"] and rsi > self.rsi_overbought and _is_bearish_reversal(prev, curr):
            direction = PositionSide.SHORT
            rsi_extremity = (rsi - self.rsi_overbought) / (100 - self.rsi_overbought)
        else:
            return None

        # Score: RSI extremity (0-1) + volume multiple (capped) + SD distance
        sd_distance = abs(price - mean) / float(curr["std"])  # in std units
        sd_score = min(max((sd_distance - self.bb_std) / self.bb_std, 0.0), 1.0)
        vol_score = min((vol_ratio - self.vol_mult) / self.vol_mult, 1.0)
        score = 0.5 + 0.25 * max(min(rsi_extremity, 1.0), 0.0) + 0.15 * vol_score + 0.10 * sd_score
        score = min(score, 1.0)

        if score < self.min_score:
            return None

        # SL = 1.5*ATR beyond the extreme wick; TP = the band mean.
        if direction == PositionSide.LONG:
            sl = float(curr["low"]) - self.atr_sl_mult * atr
            tp = mean
        else:
            sl = float(curr["high"]) + self.atr_sl_mult * atr
            tp = mean

        # Guard against an inverted target (mean already passed) or zero distance.
        if direction == PositionSide.LONG and not (sl < price < tp):
            return None
        if direction == PositionSide.SHORT and not (tp < price < sl):
            return None

        return {
            "playbook": Playbook.INTRADAY,
            "side": direction,
            "entry_price": price,
            "sl_price": sl,
            "tp_price": tp,
            "time_stop_hours": self.time_h,
            "score": round(score, 3),
            "reason": (
                f"PlaybookVolExhaust: {self.bb_std:.0f}SD band + vol x{vol_ratio:.1f} "
                f"+ RSI({rsi:.1f}) + reversal | target=mean | score={score:.2f}"
            ),
        }


# ── Playbook SweepMSS: Liquidity Sweep + Market-Structure-Shift ──

class PlaybookSweepMSS:
    """
    Liquidity sweep + market-structure-shift (Strategy B / SMC).
    Consumes the precomputed MarketStructureAnalyzer output (do not recompute):
      - structure_1h: sweep of a prior swing high/low (HTF context)
      - structure_ltf: an immediate opposing BOS (MSS) on the lower timeframe
    Entry on pullback into an unmitigated order block / FVG.
    SL beyond the sweeping wick; TP = opposing liquidity pool (active swing).
    """

    def __init__(
        self,
        sweep_max_age: int = 6,
        mss_max_age: int = 12,
        sl_buffer_pct: float = 0.001,
        min_rr: float = 1.5,
        min_score: float = 0.60,
    ):
        self.sweep_max_age = sweep_max_age
        self.mss_max_age = mss_max_age
        self.sl_buffer_pct = sl_buffer_pct
        self.min_rr = min_rr
        self.min_score = min_score

    def evaluate(
        self,
        df_1h: pd.DataFrame,
        df_ltf: pd.DataFrame,
        structure_1h: Optional[Dict],
        structure_ltf: Optional[Dict],
        regime: Optional[MarketRegime] = None,
    ) -> Optional[Dict]:
        if not structure_1h or not structure_ltf or len(df_1h) < 10:
            return None

        sweep = structure_1h.get("recent_sweep")
        if not sweep or sweep.get("candles_ago", 999) > self.sweep_max_age:
            return None

        direction = PositionSide.LONG if sweep["type"] == "BULLISH" else PositionSide.SHORT

        # MSS: an opposing BOS on the lower timeframe in the sweep's direction.
        bos = structure_ltf.get("recent_bos")
        mss_ok = bool(
            bos
            and bos["type"] == sweep["type"]
            and bos.get("candles_ago", 999) <= self.mss_max_age
        )
        if not mss_ok:
            return None

        price = float(df_1h.iloc[-1]["close"])
        swept_price = float(sweep["swept_price"])

        # SL just beyond the sweeping wick.
        if direction == PositionSide.LONG:
            sl = swept_price * (1 - self.sl_buffer_pct)
        else:
            sl = swept_price * (1 + self.sl_buffer_pct)

        # OB / FVG confluence: price currently inside an unmitigated OB of the
        # right type, or an unmitigated FVG sits ahead in the trade direction.
        ob_conf = False
        for src in (structure_ltf, structure_1h):
            for ob in src.get("unmitigated_order_blocks", []):
                if ob["type"] == sweep["type"] and float(ob["low"]) <= price <= float(ob["high"]):
                    ob_conf = True
                    break
            if ob_conf:
                break
        fvg_conf = False
        for src in (structure_ltf, structure_1h):
            for fvg in src.get("unmitigated_fvgs", []):
                if fvg["type"] == sweep["type"]:
                    fvg_conf = True
                    break
            if fvg_conf:
                break

        # TP = opposing liquidity pool (nearest active swing on the other side).
        tp1 = tp2 = None
        if direction == PositionSide.LONG:
            highs = sorted(
                [float(s["price"]) for s in structure_1h.get("active_swing_highs", []) if float(s["price"]) > price]
            )
            if highs:
                tp1 = highs[0]
                tp2 = highs[1] if len(highs) > 1 else None
        else:
            lows = sorted(
                [float(s["price"]) for s in structure_1h.get("active_swing_lows", []) if float(s["price"]) < price],
                reverse=True,
            )
            if lows:
                tp1 = lows[0]
                tp2 = lows[1] if len(lows) > 1 else None

        risk = abs(price - sl)
        if risk <= 0:
            return None

        # Fallback to an R-multiple target if no opposing liquidity pool found.
        if tp1 is None:
            tp1 = price + 2.0 * risk if direction == PositionSide.LONG else price - 2.0 * risk
        if tp2 is None:
            tp2 = price + 3.0 * risk if direction == PositionSide.LONG else price - 3.0 * risk

        # Enforce minimum reward:risk on the first target.
        if abs(tp1 - price) / risk < self.min_rr:
            return None

        # Score: sweep freshness + MSS + OB/FVG confluence.
        freshness = max(0.0, 1.0 - sweep.get("candles_ago", 0) / max(self.sweep_max_age, 1))
        score = 0.45 + 0.20 * freshness + 0.20 * (1.0 if ob_conf else 0.0) + 0.15 * (1.0 if fvg_conf else 0.0)
        score = min(score, 1.0)
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
            "time_stop_hours": 36,
            "score": round(score, 3),
            "reason": (
                f"PlaybookSweepMSS: {sweep['type']} sweep ({sweep.get('candles_ago')}c ago) "
                f"+ MSS + {'OB' if ob_conf else ''}{'/FVG' if fvg_conf else ''} | score={score:.2f}"
            ),
        }
