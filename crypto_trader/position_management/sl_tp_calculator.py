"""
crypto_trader.position_management.sl_tp_calculator
====================================================
Deterministic SL/TP computation — the AI never invents price levels.

The AI assessor may recommend a *style* (e.g. "swing_low" or "atr"),
but this module is the only place where actual price levels are produced.

Supported SL styles:
  atr              → entry ± n * ATR
  swing_low        → recent swing low (LONG) or swing high (SHORT)
  structure        → BOS/CHoCH invalidation level
  vwap             → VWAP ± buffer
  order_block      → nearest OB high/low
  pct_from_entry   → fixed percentage from entry

Supported TP styles:
  fixed_rr         → entry ± (sl_distance * rr_ratio)
  liquidity_target → nearest swing high/low beyond entry
  resistance       → identified resistance level
  atr_expansion    → entry ± n * ATR
  multi_level      → returns TP1, TP2, TP3 based on rr_ratios
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("crypto_trader.position_management.sl_tp_calculator")


@dataclass
class SLResult:
    price: float
    style: str
    invalidation_note: str = ""
    atr_used: float = 0.0


@dataclass
class TPResult:
    levels: List[float] = field(default_factory=list)  # TP1, TP2, TP3 …
    style: str = ""
    rr_ratios: List[float] = field(default_factory=list)


class StopLossCalculator:
    """
    Computes a stop loss price using one of several deterministic styles.
    The SL is always placed on the losing side of the entry.
    """

    def __init__(
        self,
        df: pd.DataFrame,
        symbol: str,
        side: str,           # "LONG" | "SHORT"
        entry_price: float,
        atr_multiplier: float = 1.5,
        swing_window: int = 3,
        pct_default: float = 0.007,  # fallback 0.7%
    ):
        self.df = df
        self.symbol = symbol
        self.side = side.upper()
        self.entry = entry_price
        self.atr_mult = atr_multiplier
        self.swing_window = swing_window
        self.pct_default = pct_default
        self._atr: Optional[float] = None

    def calculate(self, style: str = "atr") -> SLResult:
        """
        Calculate a stop loss.  Returns SLResult with .price.
        Falls back to pct_from_entry if the preferred style fails.
        """
        try:
            fn = {
                "atr":           self._atr_sl,
                "swing_low":     self._swing_sl,
                "swing_high":    self._swing_sl,
                "structure":     self._structure_sl,
                "vwap":          self._vwap_sl,
                "order_block":   self._order_block_sl,
                "pct_from_entry": self._pct_sl,
            }.get(style, self._atr_sl)
            result = fn()
            result.style = style
            return result
        except Exception as exc:
            logger.warning("[SLCalc] %s failed for %s (%s): %s — using pct fallback",
                           style, self.symbol, self.side, exc)
            return self._pct_sl()

    # ── SL methods ────────────────────────────────────────────────────────

    def _atr_sl(self) -> SLResult:
        atr = self._get_atr()
        dist = atr * self.atr_mult
        price = self.entry - dist if self.side == "LONG" else self.entry + dist
        return SLResult(
            price=price,
            style="atr",
            invalidation_note=f"Entry ± {self.atr_mult}×ATR({atr:.4f})",
            atr_used=atr,
        )

    def _swing_sl(self) -> SLResult:
        highs = self.df["high"].astype(float).values
        lows  = self.df["low"].astype(float).values
        w = self.swing_window
        swing_lows, swing_highs = [], []
        for i in range(w, len(lows) - w):
            if all(lows[i] <= lows[i - j] for j in range(1, w + 1)) and \
               all(lows[i] <= lows[i + j] for j in range(1, w + 1)):
                swing_lows.append(float(lows[i]))
            if all(highs[i] >= highs[i - j] for j in range(1, w + 1)) and \
               all(highs[i] >= highs[i + j] for j in range(1, w + 1)):
                swing_highs.append(float(highs[i]))

        atr = self._get_atr()
        buffer = atr * 0.3

        if self.side == "LONG":
            candidates = [s for s in swing_lows if s < self.entry]
            if not candidates:
                return self._atr_sl()
            sl = max(candidates) - buffer
            return SLResult(price=sl, style="swing_low",
                            invalidation_note=f"Below swing low {max(candidates):.4f}",
                            atr_used=atr)
        else:
            candidates = [s for s in swing_highs if s > self.entry]
            if not candidates:
                return self._atr_sl()
            sl = min(candidates) + buffer
            return SLResult(price=sl, style="swing_high",
                            invalidation_note=f"Above swing high {min(candidates):.4f}",
                            atr_used=atr)

    def _structure_sl(self) -> SLResult:
        """Use SMC BOS/CHoCH invalidation level."""
        try:
            from ..structure import MarketStructureAnalyzer
            analyzer = MarketStructureAnalyzer(self.df, swing_window=self.swing_window)
            result = analyzer.analyze()
            bos_events = result.get("bos_events", [])
            atr = self._get_atr()

            if self.side == "LONG":
                # SL below last bullish BOS level or last swing low
                bullish_bos = [e for e in bos_events if "bullish" in e.get("type", "")]
                if bullish_bos:
                    invalidation = float(bullish_bos[-1].get("swing_price", 0))
                    if invalidation > 0 and invalidation < self.entry:
                        return SLResult(
                            price=invalidation - atr * 0.2,
                            style="structure",
                            invalidation_note=f"Below BOS level {invalidation:.4f}",
                            atr_used=atr,
                        )
            else:
                bearish_bos = [e for e in bos_events if "bearish" in e.get("type", "")]
                if bearish_bos:
                    invalidation = float(bearish_bos[-1].get("swing_price", 0))
                    if invalidation > 0 and invalidation > self.entry:
                        return SLResult(
                            price=invalidation + atr * 0.2,
                            style="structure",
                            invalidation_note=f"Above BOS level {invalidation:.4f}",
                            atr_used=atr,
                        )
        except Exception:
            pass
        return self._swing_sl()

    def _vwap_sl(self) -> SLResult:
        closes = self.df["close"].astype(float).values
        highs  = self.df["high"].astype(float).values
        lows   = self.df["low"].astype(float).values
        vols   = self.df["volume"].astype(float).values if "volume" in self.df.columns else np.ones(len(closes))
        typical = (highs + lows + closes) / 3.0
        vwap = float(np.sum(typical * vols) / np.sum(vols)) if np.sum(vols) > 0 else float(closes[-1])
        atr = self._get_atr()
        buffer = atr * 0.5
        if self.side == "LONG":
            sl = vwap - buffer
        else:
            sl = vwap + buffer
        return SLResult(price=sl, style="vwap",
                        invalidation_note=f"VWAP({vwap:.4f}) ± buffer",
                        atr_used=atr)

    def _order_block_sl(self) -> SLResult:
        """Place SL just beyond the nearest order block."""
        try:
            from ..structure import MarketStructureAnalyzer
            analyzer = MarketStructureAnalyzer(self.df, swing_window=self.swing_window)
            result = analyzer.analyze()
            obs = result.get("order_blocks", [])
            atr = self._get_atr()
            if self.side == "LONG":
                bullish_obs = [ob for ob in obs if ob.get("type") == "bullish" and not ob.get("mitigated")]
                if bullish_obs:
                    nearest = min(bullish_obs, key=lambda ob: abs(self.entry - float(ob.get("low", 0))))
                    sl = float(nearest.get("low", 0)) - atr * 0.2
                    return SLResult(price=sl, style="order_block",
                                    invalidation_note=f"Below bullish OB {nearest.get('low'):.4f}",
                                    atr_used=atr)
            else:
                bearish_obs = [ob for ob in obs if ob.get("type") == "bearish" and not ob.get("mitigated")]
                if bearish_obs:
                    nearest = min(bearish_obs, key=lambda ob: abs(self.entry - float(ob.get("high", 0))))
                    sl = float(nearest.get("high", 0)) + atr * 0.2
                    return SLResult(price=sl, style="order_block",
                                    invalidation_note=f"Above bearish OB {nearest.get('high'):.4f}",
                                    atr_used=atr)
        except Exception:
            pass
        return self._atr_sl()

    def _pct_sl(self) -> SLResult:
        dist = self.entry * self.pct_default
        price = self.entry - dist if self.side == "LONG" else self.entry + dist
        return SLResult(price=price, style="pct_from_entry",
                        invalidation_note=f"Entry ± {self.pct_default*100:.2f}%")

    # ── ATR helper ────────────────────────────────────────────────────────

    def _get_atr(self) -> float:
        if self._atr is not None:
            return self._atr
        closes = self.df["close"].astype(float).values
        highs  = self.df["high"].astype(float).values
        lows   = self.df["low"].astype(float).values
        if len(closes) < 2:
            self._atr = self.entry * self.pct_default
            return self._atr
        tr_list = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            tr_list.append(tr)
        period = min(14, len(tr_list))
        self._atr = float(np.mean(tr_list[-period:]))
        return self._atr


class TakeProfitCalculator:
    """
    Computes take profit levels using deterministic styles.
    Always returns at least one level; multi_level returns TP1/2/3.
    """

    def __init__(
        self,
        df: pd.DataFrame,
        symbol: str,
        side: str,
        entry_price: float,
        stop_loss: float,
        swing_window: int = 3,
    ):
        self.df = df
        self.symbol = symbol
        self.side = side.upper()
        self.entry = entry_price
        self.sl = stop_loss
        self.risk = abs(entry_price - stop_loss)
        self.swing_window = swing_window

    def calculate(self, style: str = "fixed_rr", rr_ratios: Optional[List[float]] = None) -> TPResult:
        rr_ratios = rr_ratios or [2.0, 3.0, 4.5]
        try:
            fn = {
                "fixed_rr":         lambda: self._fixed_rr(rr_ratios[0]),
                "multi_level":      lambda: self._multi_level(rr_ratios),
                "liquidity_target": self._liquidity_target,
                "resistance":       self._resistance_tp,
                "atr_expansion":    self._atr_expansion,
            }.get(style, lambda: self._fixed_rr(rr_ratios[0]))
            return fn()
        except Exception as exc:
            logger.warning("[TPCalc] %s failed for %s: %s — using fixed_rr", style, self.symbol, exc)
            return self._fixed_rr(2.0)

    # ── TP methods ────────────────────────────────────────────────────────

    def _fixed_rr(self, rr: float) -> TPResult:
        if self.side == "LONG":
            tp = self.entry + self.risk * rr
        else:
            tp = self.entry - self.risk * rr
        return TPResult(levels=[tp], style="fixed_rr", rr_ratios=[rr])

    def _multi_level(self, rr_ratios: List[float]) -> TPResult:
        levels = []
        for rr in rr_ratios:
            if self.side == "LONG":
                levels.append(self.entry + self.risk * rr)
            else:
                levels.append(self.entry - self.risk * rr)
        return TPResult(levels=levels, style="multi_level", rr_ratios=rr_ratios)

    def _liquidity_target(self) -> TPResult:
        highs = self.df["high"].astype(float).values
        lows  = self.df["low"].astype(float).values
        w = self.swing_window
        if self.side == "LONG":
            swing_highs = []
            for i in range(w, len(highs) - w):
                if all(highs[i] >= highs[i - j] for j in range(1, w + 1)) and \
                   all(highs[i] >= highs[i + j] for j in range(1, w + 1)):
                    swing_highs.append(float(highs[i]))
            candidates = [s for s in swing_highs if s > self.entry]
            if candidates:
                return TPResult(levels=[min(candidates)], style="liquidity_target", rr_ratios=[])
        else:
            swing_lows = []
            for i in range(w, len(lows) - w):
                if all(lows[i] <= lows[i - j] for j in range(1, w + 1)) and \
                   all(lows[i] <= lows[i + j] for j in range(1, w + 1)):
                    swing_lows.append(float(lows[i]))
            candidates = [s for s in swing_lows if s < self.entry]
            if candidates:
                return TPResult(levels=[max(candidates)], style="liquidity_target", rr_ratios=[])
        return self._fixed_rr(2.0)

    def _resistance_tp(self) -> TPResult:
        return self._liquidity_target()

    def _atr_expansion(self, multiplier: float = 3.0) -> TPResult:
        closes = self.df["close"].astype(float).values
        highs  = self.df["high"].astype(float).values
        lows   = self.df["low"].astype(float).values
        if len(closes) < 2:
            return self._fixed_rr(2.0)
        tr_list = [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
                   for i in range(1, len(closes))]
        atr = float(np.mean(tr_list[-14:]))
        if self.side == "LONG":
            tp = self.entry + atr * multiplier
        else:
            tp = self.entry - atr * multiplier
        return TPResult(levels=[tp], style="atr_expansion", rr_ratios=[multiplier])
