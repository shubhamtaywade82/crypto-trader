"""
crypto_trader.strategies.mtf_alignment — Multi-timeframe alignment breakout.
=============================================================================
Port of the validated builder strategy (python_strategy_builder/RESEARCH_FINDINGS.md):

    Macro bias  : EMA50 on 1h + 4h + 1d must ALL agree (price > EMA50 on every
                  macro frame → long; below every frame → short; else no trade).
    Structure   : breakout of the prior-N high/low on the execution TF (5m).
    Trigger     : engulfing candle on the execution TF in the macro direction.
    Stop        : recent swing low/high on the execution TF, FLOORED so the
                  tp_r target is a ≥1% NET move after fees.
    Target      : tp_r × risk (validated optimum 3.0R; ~40% win, big winners).

Out-of-sample frontier (90d, net of fees): ETH +0.44R @3R, BTC +0.25R @3R — both
on THIN samples (~19 trades). Edge is real but unproven at size — paper-test.
Win rate is intentionally LOW (~40%); the edge is the wide payoff, not hit-rate.

Same shared rule helpers are used by ``backtest/mta_engine.py`` so the live and
backtested logic cannot drift.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..regime import compute_atr, compute_ema

logger = logging.getLogger("crypto_trader.strategies.mtf_alignment")


# ── shared rule helpers (used by both live evaluate and the backtest engine) ──

def macro_bias(entry_close: float, htf_emas: List[Optional[float]],
               min_frames: int) -> int:
    """+1 if entry_close is above EVERY available macro EMA50, -1 if below every,
    else 0. Needs ≥ min_frames present."""
    present = [e for e in htf_emas if e is not None and np.isfinite(e)]
    if len(present) < min_frames:
        return 0
    if all(entry_close > e for e in present):
        return 1
    if all(entry_close < e for e in present):
        return -1
    return 0


def _bullish_engulf(o2, c2, o1, c1) -> bool:
    return c2 < o2 and c1 > o1 and c1 > o2 and o1 < c2


def _bearish_engulf(o2, c2, o1, c1) -> bool:
    return c2 > o2 and c1 < o1 and c1 < o2 and o1 > c2


class PlaybookMTFAlignment:
    name = "mtf_alignment"

    def __init__(
        self,
        *,
        entry_timeframe: str = "5m",
        macro_tfs: Optional[List[str]] = None,
        ema_len: int = 50,
        min_macro_frames: int = 2,
        break_lookback: int = 5,
        swing_lookback: int = 6,
        tp_r: float = 3.0,
        atr_period: int = 14,
        min_stop_frac: float = 0.0055,  # 2R≈1.1% gross ≈1% net; at 3R even wider
        max_hold_hours: int = 24,
    ) -> None:
        self.entry_timeframe = entry_timeframe
        self.macro_tfs = macro_tfs or ["1h", "4h", "1d"]
        self.ema_len = max(2, ema_len)
        self.min_macro_frames = max(1, min_macro_frames)
        self.break_lookback = max(1, break_lookback)
        self.swing_lookback = max(2, swing_lookback)
        self.tp_r = max(0.5, tp_r)
        self.atr_period = max(2, atr_period)
        self.min_stop_frac = max(0.0, min_stop_frac)
        self.max_hold_hours = max(1, max_hold_hours)
        self.last_eval: Optional[dict] = None

    def apply_overrides(self, ov: dict) -> None:
        for key, val in ov.items():
            if not key.startswith("mta_"):
                continue
            attr = key[len("mta_"):]
            if not hasattr(self, attr):
                continue
            existing = getattr(self, attr)
            try:
                typed = type(existing)(val) if existing is not None and not isinstance(existing, list) else val
            except (TypeError, ValueError):
                typed = val
            setattr(self, attr, typed)

    def _min_bars(self) -> int:
        return max(self.break_lookback + 2, self.swing_lookback + 2, self.atr_period + 2, 30)

    def evaluate(self, df_entry: pd.DataFrame,
                 htf_frames: Optional[Dict[str, pd.DataFrame]] = None) -> Optional[Dict]:
        from ..wallet import PositionSide, Playbook

        self.last_eval = None
        if df_entry is None or len(df_entry) < self._min_bars():
            return None
        htf_frames = htf_frames or {}

        entry = float(df_entry["close"].iloc[-1])
        emas: List[Optional[float]] = []
        for tf in self.macro_tfs:
            hdf = htf_frames.get(tf)
            if hdf is None or len(hdf) < self.ema_len + 2:
                emas.append(None); continue
            ev = compute_ema(hdf["close"], self.ema_len).iloc[-1]
            emas.append(float(ev) if pd.notna(ev) else None)

        bias = macro_bias(entry, emas, self.min_macro_frames)
        if bias == 0:
            self.last_eval = {"stage": "macro_bias"}
            return None
        is_long = bias == 1

        highs = df_entry["high"].astype(float).values
        lows = df_entry["low"].astype(float).values
        opens = df_entry["open"].astype(float).values
        closes = df_entry["close"].astype(float).values
        n = len(df_entry)
        i = n - 1

        prior_high = float(np.max(highs[i - self.break_lookback:i]))
        prior_low = float(np.min(lows[i - self.break_lookback:i]))
        breakout = (closes[i] > prior_high) if is_long else (closes[i] < prior_low)
        if not breakout:
            self.last_eval = {"stage": "breakout", "long": is_long}
            return None

        eng = (_bullish_engulf(opens[i - 1], closes[i - 1], opens[i], closes[i]) if is_long
               else _bearish_engulf(opens[i - 1], closes[i - 1], opens[i], closes[i]))
        if not eng:
            self.last_eval = {"stage": "engulfing", "long": is_long}
            return None

        atr = float(compute_atr(df_entry, self.atr_period).iloc[-1])
        if not np.isfinite(atr) or atr <= 0:
            atr = entry * 0.005
        if is_long:
            swing = float(np.min(lows[i - self.swing_lookback + 1:i + 1]))
            risk = max(entry - swing, entry * self.min_stop_frac)
            sl = entry - risk
            tp = entry + self.tp_r * risk
            side = PositionSide.LONG
        else:
            swing = float(np.max(highs[i - self.swing_lookback + 1:i + 1]))
            risk = max(swing - entry, entry * self.min_stop_frac)
            sl = entry + risk
            tp = entry - self.tp_r * risk
            side = PositionSide.SHORT
        if risk <= 0:
            return None

        setup = {
            "side": side, "entry_price": entry, "sl_price": float(sl), "tp_price": float(tp),
            "score": 0.75, "name": self.name, "strategy": "mtf_alignment",
            "playbook": Playbook.INTRADAY, "time_stop_hours": self.max_hold_hours, "tp_r": self.tp_r,
            "reason": (f"MTF {side.value} | macro EMA{self.ema_len} aligned ({self.min_macro_frames}+ frames) "
                       f"| breakout+engulf | TP={self.tp_r}R"),
        }
        self.last_eval = {"side": side.value, "entry": entry, "sl": sl, "tp": tp}
        logger.info("[MTA SIGNAL] %s | macro aligned | entry=%.4f sl=%.4f tp=%.4f (%.1fR)",
                    side.value, entry, sl, tp, self.tp_r)
        return setup
