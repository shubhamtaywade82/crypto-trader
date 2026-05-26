"""
crypto_trader.regime — Multi-Timeframe Market Regime Classification
======================================================================
Determines trend bias using EMA alignment, ADX, and swing structure.
Returns regime + confidence score for LLM gating.

Two regime enums are provided:
  MarketRegime      — 4-state (legacy, used by playbooks & wallet)
  ExtendedRegime    — 8-state (richer classification used by RegimeClassifier)
"""

import logging
from dataclasses import dataclass
from typing import Optional, Tuple, List
from enum import Enum

import numpy as np
import pandas as pd

logger = logging.getLogger("crypto_trader.regime")


class MarketRegime(Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    CHOP = "CHOP"


class ExtendedRegime(str, Enum):
    """8-state regime used by RegimeClassifier for strategy routing."""
    DEAD_MARKET          = "DEAD_MARKET"
    LOW_VOL_CHOP         = "LOW_VOL_CHOP"
    ACCUMULATION         = "ACCUMULATION"
    TREND_EXPANSION      = "TREND_EXPANSION"
    LIQUIDATION_EVENT    = "LIQUIDATION_EVENT"
    BREAKOUT_ENVIRONMENT = "BREAKOUT_ENVIRONMENT"
    MEAN_REVERSION       = "MEAN_REVERSION"
    HIGH_RISK_CHAOS      = "HIGH_RISK_CHAOS"


@dataclass
class RegimeContext:
    """Rich regime output from RegimeClassifier."""
    extended: ExtendedRegime
    base: MarketRegime          # underlying 4-state classification
    rvol: float                 # relative volume (current / 20-bar avg)
    oi_delta: float             # open interest delta (% change, or 0 if unavailable)
    liquidation_intensity: float  # 0.0–1.0 normalised liquidation volume
    session_name: str           # TradingSession.value from session context
    is_kill_zone: bool
    adx: float
    size_multiplier: float      # suggested position-size scalar (0.0–1.25)

    def allows_new_entries(self) -> bool:
        return self.extended not in (
            ExtendedRegime.DEAD_MARKET,
            ExtendedRegime.HIGH_RISK_CHAOS,
            ExtendedRegime.LIQUIDATION_EVENT,
        )


def calculate_rvol(df: pd.DataFrame, window: int = 20) -> float:
    """Relative volume: current bar's quote_volume vs rolling window average."""
    if "quote_volume" not in df.columns or len(df) < window + 1:
        return 1.0
    avg = df["quote_volume"].rolling(window).mean().iloc[-1]
    current = df["quote_volume"].iloc[-1]
    if avg <= 0:
        return 1.0
    return round(float(current / avg), 3)


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


class RegimeClassifier:
    """
    Maps raw market metrics to an ExtendedRegime and a position-size multiplier.

    Inputs come from MarketRegimeAnalyzer (base regime + ADX), RVOL, OI delta,
    liquidation data, and session context — all of which are already computed
    on every signal tick.

    Thresholds are tunable via config flags (RVOL_DEAD_THRESHOLD etc.).
    """

    def __init__(
        self,
        rvol_dead: float = 0.6,
        rvol_weak: float = 0.9,
        rvol_strong: float = 1.5,
        size_high: float = 1.25,
        size_low: float = 0.5,
        liq_intensity_threshold: float = 0.3,
        adx_ranging: float = 20.0,
    ):
        self.rvol_dead = rvol_dead
        self.rvol_weak = rvol_weak
        self.rvol_strong = rvol_strong
        self.size_high = size_high
        self.size_low = size_low
        self.liq_intensity_threshold = liq_intensity_threshold
        self.adx_ranging = adx_ranging

    def classify(
        self,
        base_regime: MarketRegime,
        adx: float,
        rvol: float,
        oi_delta: float,
        liquidation_intensity: float,
        is_kill_zone: bool,
        session_name: str,
        spread_pct: float = 0.0,
    ) -> tuple[ExtendedRegime, float]:
        """Return (ExtendedRegime, size_multiplier).

        size_multiplier: caller should scale risk_budget by this value.
        """
        # ── Priority gates (highest risk first) ──

        if rvol < self.rvol_dead:
            return ExtendedRegime.DEAD_MARKET, 0.0

        if spread_pct > 0.5:
            return ExtendedRegime.HIGH_RISK_CHAOS, 0.0

        is_trending = base_regime in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN)
        is_ranging = base_regime in (MarketRegime.RANGING, MarketRegime.CHOP)

        # Liquidation event: strong trend + OI expanding + high liq intensity
        if (
            is_trending
            and rvol >= self.rvol_strong
            and oi_delta > 0
            and liquidation_intensity >= self.liq_intensity_threshold
        ):
            return ExtendedRegime.LIQUIDATION_EVENT, 0.0

        # Trend expansion: trending + high vol + kill zone or strong OI
        if is_trending and rvol >= self.rvol_strong and (is_kill_zone or oi_delta > 0):
            return ExtendedRegime.TREND_EXPANSION, self.size_high

        # Breakout environment: trending + moderate vol + kill zone
        if is_trending and rvol >= self.rvol_weak and is_kill_zone:
            return ExtendedRegime.BREAKOUT_ENVIRONMENT, 1.0

        # Accumulation: weak trending or mild consolidation before a move
        if is_trending and rvol >= self.rvol_weak:
            return ExtendedRegime.ACCUMULATION, 0.75

        # Mean reversion: ranging + ADX low
        if is_ranging and adx < self.adx_ranging:
            return ExtendedRegime.MEAN_REVERSION, self.size_low

        # Low vol chop: dead-adjacent but not quite dead
        if rvol < self.rvol_weak:
            return ExtendedRegime.LOW_VOL_CHOP, self.size_low

        # Default fallback
        return ExtendedRegime.MEAN_REVERSION, self.size_low

    def build_context(
        self,
        base_regime: MarketRegime,
        adx: float,
        rvol: float,
        oi_delta: float,
        liquidation_intensity: float,
        session_name: str,
        is_kill_zone: bool,
        spread_pct: float = 0.0,
    ) -> RegimeContext:
        """Convenience wrapper that returns a full RegimeContext dataclass."""
        extended, size_mult = self.classify(
            base_regime=base_regime,
            adx=adx,
            rvol=rvol,
            oi_delta=oi_delta,
            liquidation_intensity=liquidation_intensity,
            is_kill_zone=is_kill_zone,
            session_name=session_name,
            spread_pct=spread_pct,
        )
        return RegimeContext(
            extended=extended,
            base=base_regime,
            rvol=rvol,
            oi_delta=oi_delta,
            liquidation_intensity=liquidation_intensity,
            session_name=session_name,
            is_kill_zone=is_kill_zone,
            adx=adx,
            size_multiplier=size_mult,
        )
