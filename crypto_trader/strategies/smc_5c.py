"""
crypto_trader.strategies.smc_5c — ML-report 5-Condition SMC strategy (both sides).
==================================================================================
Ported from python_strategy_builder's "5-condition" blueprint so the champion
SELECTOR can score it head-to-head against the other strategies on equal,
out-of-sample, fee-aware footing — rather than trusting the report's prose.

Five conditions (LONG; SHORT mirrors):
  1. 4H trend     : EMA(fast) > EMA(slow)          (default 50/200)
  2. 1H structure : close breaks the prior-N high  (break of structure up)
  3. 15m FVG      : a bullish FVG in the last K bars (low[j] > high[j-2])
  4. Volatility   : entry-TF ATR(14) > vol_atr_pctile (rolling window)
  5. Volume       : entry-TF volume  > vol_pctile      (rolling window)
Exit: fixed R:R — TP = +tp_pct, SL = -sl_pct (default 2:1 = +1.0% / -0.5%).

NO external `ta` dependency — reuses the repo's own compute_ema / compute_atr /
MarketStructureAnalyzer. Same shape as the other playbooks: evaluate(df_entry,
htf_frames) → setup dict. Shared rule helpers are reused by
``backtest/smc5c_engine.py`` so live and backtest cannot drift.

VALIDATION: the research ML run for this blueprint FAILED out-of-sample (0
confident trades, negative cost ladder, NOT-VALIDATED). It is wired here ONLY so
the selector can confirm/deny it on the bot's own backtest. Do NOT trade for
capital unless the selector crowns it champion on a real sample. Percentiles use
a rolling window over CLOSED bars (no whole-history lookahead).
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

import numpy as np
import pandas as pd

from ..regime import compute_atr, compute_ema
from ..structure import MarketStructureAnalyzer

logger = logging.getLogger("crypto_trader.strategies.smc_5c")


# ── shared rule helpers (used by live evaluate + the backtest engine) ──

def trend_dir(htf_close: pd.Series, fast: int, slow: int) -> int:
    """+1 if EMA(fast) > EMA(slow) on the HTF, -1 if below, 0 if undecided/NaN."""
    if htf_close is None or len(htf_close) < slow + 2:
        return 0
    ef = compute_ema(htf_close, fast).iloc[-1]
    es = compute_ema(htf_close, slow).iloc[-1]
    if pd.isna(ef) or pd.isna(es) or ef == es:
        return 0
    return 1 if ef > es else -1


def bos_dir(df_1h: pd.DataFrame, lookback: int) -> int:
    """+1 if last close breaks the prior-N high, -1 if breaks the prior-N low, else 0."""
    if df_1h is None or len(df_1h) < lookback + 2:
        return 0
    hi = float(df_1h["high"].iloc[-lookback - 1:-1].max())
    lo = float(df_1h["low"].iloc[-lookback - 1:-1].min())
    c = float(df_1h["close"].iloc[-1])
    if c > hi:
        return 1
    if c < lo:
        return -1
    return 0


def fvg_recent(df_15m: pd.DataFrame, window: int) -> int:
    """Bitmask: +1 bit if a bullish FVG in last ``window`` bars, +2 bit if bearish.
    (low[j] > high[j-2] = bullish gap; high[j] < low[j-2] = bearish gap.)"""
    if df_15m is None or len(df_15m) < 3:
        return 0
    lows = df_15m["low"].values
    highs = df_15m["high"].values
    start = max(2, len(df_15m) - window)
    bull = bear = False
    for j in range(start, len(df_15m)):
        if lows[j] > highs[j - 2]:
            bull = True
        elif highs[j] < lows[j - 2]:
            bear = True
    return (1 if bull else 0) | (2 if bear else 0)


class PlaybookSMC5C:
    name = "smc_5c"

    def __init__(
        self,
        *,
        entry_timeframe: str = "5m",       # report says 1m; 5m default for tractability
        ema_fast: int = 50,
        ema_slow: int = 200,
        bos_lookback: int = 5,
        fvg_window: int = 10,
        atr_period: int = 14,
        vol_window: int = 200,             # rolling window for the percentiles
        atr_pctile: float = 60.0,
        vol_pctile: float = 70.0,
        tp_pct: float = 0.010,             # +1.0%
        sl_pct: float = 0.005,             # -0.5%  → 2:1
        max_hold_hours: int = 4,
    ) -> None:
        self.entry_timeframe = entry_timeframe
        self.ema_fast = max(2, ema_fast)
        self.ema_slow = max(self.ema_fast + 1, ema_slow)
        self.bos_lookback = max(1, bos_lookback)
        self.fvg_window = max(1, fvg_window)
        self.atr_period = max(2, atr_period)
        self.vol_window = max(20, vol_window)
        self.atr_pctile = min(max(atr_pctile, 0.0), 100.0)
        self.vol_pctile = min(max(vol_pctile, 0.0), 100.0)
        self.tp_pct = max(1e-4, tp_pct)
        self.sl_pct = max(1e-4, sl_pct)
        self.max_hold_hours = max(1, max_hold_hours)
        self.last_eval: Optional[dict] = None

    def apply_overrides(self, ov: dict) -> None:
        for key, val in ov.items():
            if not key.startswith("smc5c_"):
                continue
            attr = key[len("smc5c_"):]
            if not hasattr(self, attr):
                continue
            existing = getattr(self, attr)
            try:
                typed = type(existing)(val) if existing is not None else val
            except (TypeError, ValueError):
                typed = val
            setattr(self, attr, typed)

    def _min_bars(self) -> int:
        return max(self.atr_period + 2, self.vol_window + 2, 30)

    def evaluate(self, df_entry: pd.DataFrame,
                 htf_frames: Optional[Dict[str, pd.DataFrame]] = None) -> Optional[Dict]:
        from ..wallet import PositionSide, Playbook

        self.last_eval = None
        if df_entry is None or len(df_entry) < self._min_bars():
            return None
        htf = htf_frames or {}
        df_15m, df_1h, df_4h = htf.get("15m"), htf.get("1h"), htf.get("4h")

        # 1) 4H trend
        t = trend_dir(df_4h["close"] if df_4h is not None else None, self.ema_fast, self.ema_slow)
        if t == 0:
            self.last_eval = {"stage": "trend"}
            return None
        is_long = t == 1

        # 2) 1H break of structure (in trend direction)
        b = bos_dir(df_1h, self.bos_lookback)
        if (is_long and b != 1) or (not is_long and b != -1):
            self.last_eval = {"stage": "bos", "bos": b, "long": is_long}
            return None

        # 3) 15m FVG present in direction
        f = fvg_recent(df_15m, self.fvg_window)
        if (is_long and not (f & 1)) or (not is_long and not (f & 2)):
            self.last_eval = {"stage": "fvg", "mask": f, "long": is_long}
            return None

        # 4) volatility percentile (rolling window over closed bars)
        atr_series = compute_atr(df_entry, self.atr_period).dropna()
        if len(atr_series) < 5:
            return None
        atr_win = atr_series.values[-self.vol_window:]
        if atr_series.iloc[-1] <= np.percentile(atr_win, self.atr_pctile):
            self.last_eval = {"stage": "atr_pctile"}
            return None

        # 5) volume percentile (rolling window)
        vol = df_entry["volume"].dropna().values
        if len(vol) < 5:
            return None
        vol_win = vol[-self.vol_window:]
        if vol[-1] <= np.percentile(vol_win, self.vol_pctile):
            self.last_eval = {"stage": "vol_pctile"}
            return None

        entry = float(df_entry["close"].iloc[-1])
        if is_long:
            sl = entry * (1 - self.sl_pct); tp = entry * (1 + self.tp_pct); side = PositionSide.LONG
        else:
            sl = entry * (1 + self.sl_pct); tp = entry * (1 - self.tp_pct); side = PositionSide.SHORT

        setup = {
            "side": side, "entry_price": entry, "sl_price": float(sl), "tp_price": float(tp),
            "score": 0.75, "name": self.name, "strategy": "smc_5c",
            "playbook": Playbook.INTRADAY, "time_stop_hours": self.max_hold_hours,
            "reason": (f"SMC5C {side.value} | 4H trend + 1H BOS + 15m FVG + ATR>{self.atr_pctile:.0f}p "
                       f"+ Vol>{self.vol_pctile:.0f}p | {self.tp_pct*100:.1f}%/{self.sl_pct*100:.1f}%"),
        }
        self.last_eval = {"side": side.value, "entry": entry, "sl": sl, "tp": tp}
        logger.info("[SMC5C SIGNAL] %s | entry=%.4f sl=%.4f tp=%.4f", side.value, entry, sl, tp)
        return setup
