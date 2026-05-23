"""
crypto_trader.regime — Multi-Timeframe Market Regime Classification
======================================================================
Determines trend bias using EMA alignment, ADX, and swing structure.
Returns regime + confidence score for LLM gating.
"""

import logging
from typing import Tuple, List
from enum import Enum

import numpy as np
import pandas as pd

logger = logging.getLogger("crypto_trader.regime")


class MarketRegime(Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    CHOP = "CHOP"


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta.where(delta < 0, 0.0))
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - df["close"].shift(1)).abs()
    tr3 = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index. >25 = trending, <20 = ranging."""
    plus_dm = df["high"].diff()
    minus_dm = -df["low"].diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    atr = compute_atr(df, period)
    plus_di = 100 * (plus_dm.ewm(alpha=1/period).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period).mean() / atr)
    denominator = plus_di + minus_di
    dx = np.where(denominator == 0, 0.0, 100 * (plus_di - minus_di).abs() / denominator)
    dx = pd.Series(dx, index=plus_di.index)
    return dx.ewm(alpha=1/period).mean()

def find_pivots(df: pd.DataFrame, window: int = 3) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """Find swing highs and lows. Higher window = fewer, more significant pivots."""
    ph, pl = [], []
    highs = df["high"].values
    lows = df["low"].values
    for i in range(window, len(df) - window):
        if highs[i] == max(highs[i-window:i+window+1]):
            ph.append((i, highs[i]))
        if lows[i] == min(lows[i-window:i+window+1]):
            pl.append((i, lows[i]))
    return ph, pl


class MarketRegimeAnalyzer:
    """Classifies market regime and returns confidence score (0.0–1.0)."""

    def __init__(
        self,
        ema_fast: int = 9,
        ema_slow: int = 21,
        adx_period: int = 14,
        adx_trend_threshold: float = 25.0,
        pivot_window: int = 3,
    ):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.adx_period = adx_period
        self.adx_threshold = adx_trend_threshold
        self.pivot_window = pivot_window

    def analyze(self, df_4h: pd.DataFrame) -> Tuple[MarketRegime, float, pd.DataFrame]:
        """
        Returns (regime, score, enriched_df).
        Score: 0.0–1.0 confidence in the regime classification.
        """
        if len(df_4h) < 30:
            return MarketRegime.CHOP, 0.0, df_4h

        df = df_4h.copy()
        df["ema_fast"] = compute_ema(df["close"], self.ema_fast)
        df["ema_slow"] = compute_ema(df["close"], self.ema_slow)
        df["rsi"] = compute_rsi(df["close"], 14)
        df["adx"] = compute_adx(df, self.adx_period)

        latest = df.iloc[-1]
        price = latest["close"]
        ema_f, ema_s = latest["ema_fast"], latest["ema_slow"]
        adx = latest["adx"]
        ema_dist = abs(ema_f - ema_s) / price

        # Pivot structure with minimum distance filter (1%)
        ph, pl = find_pivots(df, window=self.pivot_window)

        # Filter pivots: must be >1% apart from adjacent pivots
        ph = self._filter_pivots(ph, min_dist_pct=0.01)
        pl = self._filter_pivots(pl, min_dist_pct=0.01)

        hh_hl = False
        lh_ll = False
        if len(ph) >= 3 and len(pl) >= 3:
            last_ph = [p[1] for p in ph[-3:]]
            last_pl = [p[1] for p in pl[-3:]]
            if last_ph[0] < last_ph[1] < last_ph[2] and last_pl[0] < last_pl[1] < last_pl[2]:
                hh_hl = True
            if last_ph[0] > last_ph[1] > last_ph[2] and last_pl[0] > last_pl[1] > last_pl[2]:
                lh_ll = True

        # Score components
        adx_score = min(adx / 50.0, 1.0)  # Normalize ADX to 0-1
        ema_align_score = 1.0 if ema_dist > 0.005 else 0.5
        structure_score = 1.0 if (hh_hl or lh_ll) else 0.3

        # Regime classification
        if adx >= self.adx_threshold:
            if ema_f > ema_s and price > ema_s and hh_hl:
                regime = MarketRegime.TRENDING_UP
                score = (adx_score + ema_align_score + structure_score) / 3
            elif ema_f < ema_s and price < ema_s and lh_ll:
                regime = MarketRegime.TRENDING_DOWN
                score = (adx_score + ema_align_score + structure_score) / 3
            else:
                regime = MarketRegime.RANGING
                score = adx_score * 0.5
        else:
            if ema_dist < 0.003:
                regime = MarketRegime.CHOP
                score = 0.3
            else:
                regime = MarketRegime.RANGING
                score = 0.5

        return regime, round(score, 3), df

    def _filter_pivots(self, pivots: List[Tuple[int, float]], min_dist_pct: float) -> List[Tuple[int, float]]:
        """Remove pivots that are too close to adjacent pivots."""
        if len(pivots) < 2:
            return pivots
        filtered = [pivots[0]]
        for i in range(1, len(pivots)):
            dist = abs(pivots[i][1] - filtered[-1][1]) / filtered[-1][1]
            if dist >= min_dist_pct:
                filtered.append(pivots[i])
        return filtered
