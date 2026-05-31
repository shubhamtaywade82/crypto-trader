"""
crypto_trader.strategies.liquidity_sweep — Liquidity-Sweep reversal playbook.
=============================================================================
The one configuration that showed a net-positive, fee-aware, out-of-sample edge
in the strategy-builder research (see python_strategy_builder/RESEARCH_FINDINGS.md):

    ETH, liquidity-sweep entry, TP = 2.5R, stop at the swept wick
    → PF ~1.55, +0.14R/trade, 39 OOS trades (90d), net of CoinDCX fees.

Logic (faithful to the validated builder condition `liquidity_sweep_mss_entry`):
  * A liquidity sweep = price wicks BEYOND a prior swing (taking stops) then
    closes BACK inside — a bullish sweep takes sell-side liquidity below a swing
    low; a bearish sweep takes buy-side liquidity above a swing high.
  * Enter on the reversal in the sweep direction (bullish sweep → LONG).
  * Stop just beyond the swept wick (the structural invalidation).
  * Take-profit at ``tp_r`` × risk (default 2.5R). Edge is from a LOW win-rate
    (~40%) paid off by winners running 2-2.5× the loss — NOT from a high hit-rate.

Sweep detection reuses ``MarketStructureAnalyzer`` (the same swing/sweep engine
the rest of the bot uses, incl. the CHoCH/OB-anchor fixes).

EDGE IS ETH-SPECIFIC and modest-sample. Run paper / forward-test ≥30 days and
confirm the +0.14R holds before any capital. Scope it to ETH via
``STRATEGY_SYMBOLS=ETHUSDT`` — it loses on SOL and is marginal on XRP.

Setup dict schema (same as the other playbooks).
"""
from __future__ import annotations

import logging
from typing import Optional, Dict

import numpy as np
import pandas as pd

from ..regime import compute_atr, compute_ema
from ..structure import MarketStructureAnalyzer

logger = logging.getLogger("crypto_trader.strategies.liquidity_sweep")


class PlaybookLiquiditySweep:
    name = "liquidity_sweep"

    def __init__(
        self,
        *,
        timeframe: str = "15m",
        htf_timeframe: str = "4h",
        swing_window: int = 3,
        sweep_lookback: int = 3,      # sweep must be this fresh (candles ago) to act
        tp_r: float = 2.5,            # take-profit as a multiple of risk (validated 2.5R)
        atr_period: int = 14,
        sl_buffer_atr: float = 0.25,  # padding beyond the swept wick
        min_stop_frac: float = 0.002, # floor stop at 0.2% of price (avoid micro-stops)
        use_htf_filter: bool = False, # optional: require HTF EMA bias to agree
        max_hold_hours: int = 24,
    ) -> None:
        self.timeframe = timeframe
        self.htf_timeframe = htf_timeframe
        self.swing_window = max(2, swing_window)
        self.sweep_lookback = max(1, sweep_lookback)
        self.tp_r = max(0.5, tp_r)
        self.atr_period = max(2, atr_period)
        self.sl_buffer_atr = max(0.0, sl_buffer_atr)
        self.min_stop_frac = max(0.0, min_stop_frac)
        self.use_htf_filter = use_htf_filter
        self.max_hold_hours = max(1, max_hold_hours)
        self.last_eval: Optional[dict] = None

    def apply_overrides(self, ov: dict) -> None:
        """Hot-reload tunables prefixed ``lsw_``. Only existing attrs are touched."""
        for key, val in ov.items():
            if not key.startswith("lsw_"):
                continue
            attr = key[len("lsw_"):]
            if not hasattr(self, attr):
                continue
            existing = getattr(self, attr)
            try:
                typed_val = type(existing)(val) if existing is not None else val
            except (TypeError, ValueError):
                typed_val = val
            setattr(self, attr, typed_val)

    def _min_bars(self) -> int:
        return max(self.swing_window * 2 + 1, self.atr_period + 2, 30)

    @staticmethod
    def _htf_bias(df_htf: Optional[pd.DataFrame]) -> int:
        if df_htf is None or len(df_htf) < 30:
            return 0
        close = df_htf["close"]
        ef = compute_ema(close, 9).iloc[-1]
        es = compute_ema(close, 21).iloc[-1]
        if pd.isna(ef) or pd.isna(es):
            return 0
        price = float(close.iloc[-1])
        if ef > es and price > es:
            return 1
        if ef < es and price < es:
            return -1
        return 0

    def evaluate(self, df: pd.DataFrame, df_htf: Optional[pd.DataFrame] = None) -> Optional[Dict]:
        from ..wallet import PositionSide, Playbook

        self.last_eval = None
        if df is None or len(df) < self._min_bars():
            return None

        struct = MarketStructureAnalyzer(df, swing_window=self.swing_window).analyze()
        sweep = struct.get("recent_sweep")
        if not sweep:
            self.last_eval = {"stage": "no_sweep"}
            return None
        if sweep.get("candles_ago", 10 ** 9) > self.sweep_lookback:
            self.last_eval = {"stage": "stale_sweep", "ago": sweep.get("candles_ago")}
            return None

        is_long = sweep.get("type") == "BULLISH"   # swept sell-side → bullish reversal
        want = 1 if is_long else -1

        if self.use_htf_filter:
            bias = self._htf_bias(df_htf)
            if bias != 0 and bias != want:
                self.last_eval = {"stage": "htf_block", "bias": bias, "want": want}
                return None

        entry = float(df["close"].iloc[-1])
        atr = float(compute_atr(df, self.atr_period).iloc[-1])
        if not np.isfinite(atr) or atr <= 0:
            atr = entry * 0.005
        swept = float(sweep["swept_price"])

        if is_long:
            sl = min(swept, entry) - self.sl_buffer_atr * atr
            risk = entry - sl
            if risk < entry * self.min_stop_frac:
                risk = entry * self.min_stop_frac
                sl = entry - risk
            tp = entry + self.tp_r * risk
            side = PositionSide.LONG
        else:
            sl = max(swept, entry) + self.sl_buffer_atr * atr
            risk = sl - entry
            if risk < entry * self.min_stop_frac:
                risk = entry * self.min_stop_frac
                sl = entry + risk
            tp = entry - self.tp_r * risk
            side = PositionSide.SHORT

        if risk <= 0:
            return None

        score = 0.75   # deterministic; this strategy gates on structure, not a score fusion
        setup = {
            "side": side,
            "entry_price": entry,
            "sl_price": float(sl),
            "tp_price": float(tp),
            "score": score,
            "reason": (
                f"LiqSweep {side.value} | {sweep['type']} sweep {sweep['candles_ago']}c ago "
                f"@ swept={swept:.4f} | TP={self.tp_r}R"
            ),
            "name": self.name,
            "strategy": "liquidity_sweep",
            "playbook": Playbook.INTRADAY,
            "time_stop_hours": self.max_hold_hours,
            "tp_r": self.tp_r,
            "swept_price": swept,
        }
        self.last_eval = {"side": sweep["type"], "ago": sweep["candles_ago"],
                          "entry": entry, "sl": sl, "tp": tp}
        logger.info(
            "[LSW SIGNAL] %s | sweep %s %dc ago | entry=%.4f sl=%.4f tp=%.4f (%.1fR)",
            side.value, sweep["type"], sweep["candles_ago"], entry, sl, tp, self.tp_r,
        )
        return setup
