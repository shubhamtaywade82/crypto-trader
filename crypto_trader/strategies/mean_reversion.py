"""
crypto_trader.strategies.mean_reversion — SMA-Band Mean-Reversion Playbook
===========================================================================
Implements the strategy described in the project's PRD / SRD docs:

    Entry LONG  : last_closed_candle.close <= SMA(period) * (1 - entry_band)
    Entry SHORT : last_closed_candle.close >= SMA(period) * (1 + entry_band)

    Exit  LONG  : close >= SMA(period)           ← price reverted to mean
    Exit  SHORT : close <= SMA(period)

    Hard SL     : entry * (1 ∓ stop_loss_pct)   ← placed immediately after fill

Key design decisions
--------------------
* Operates on **closed candles only** — the last-closed candle is iloc[-2]
  because iloc[-1] is the still-forming bar.
* Symbol-agnostic: the playbook receives a DataFrame of any symbol's klines
  and has zero hard-coded instrument knowledge.
* Integrates into the engine as a drop-in alongside PlaybookSupertrend2:
  - ``evaluate()`` returns the same dict schema the engine expects.
  - ``check_sma_exit()`` is called each signal tick to manage open positions.
* Duplicate-candle protection: tracks the timestamp of the last processed
  candle and skips a tick if the candle hasn't changed.
* Funding-rate guard is applied in the engine layer (not here).
* Stop-loss percentage and entry band are configurable per instance.

Setup dict schema (same as other playbooks)
-------------------------------------------
{
    "side"       : PositionSide,
    "entry_price": float,      # signal close price (engine re-anchors to mid)
    "sl_price"   : float,      # hard stop price
    "tp_price"   : float,      # SMA touch target (soft, engine may override)
    "score"      : float,      # 0.0–1.0 signal quality score
    "reason"     : str,
    "name"       : str,        # playbook identifier for logging
    "strategy"   : str,        # "mean_reversion"
}
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from ..wallet import PositionSide, Playbook  # shared enums used throughout the engine

logger = logging.getLogger("crypto_trader.strategies.mean_reversion")

# ─── sentinel ────────────────────────────────────────────────────────────────
_SENTINEL_TS = pd.Timestamp(0, unit="ms", tz="UTC")


class PlaybookMeanReversion:
    """
    SMA-band mean-reversion playbook for any perpetual futures symbol.

    Parameters
    ----------
    sma_period      : int   — rolling SMA period (default 20, per PRD §5)
    entry_band      : float — entry trigger distance from SMA as a fraction
                              (default 0.015 = 1.5%, per PRD §5)
    stop_loss_pct   : float — hard SL distance from fill price as a fraction
                              (default 0.008 = 0.8%, per PRD §6)
    timeframe       : str   — kline interval string used for logging only;
                              the caller passes the correct DataFrame
    min_bars        : int   — minimum rows required before computing SMA;
                              default sma_period + 5 for warm-up safety
    """

    name = "mean_reversion"

    def __init__(
        self,
        sma_period: int = 20,
        entry_band: float = 0.015,
        stop_loss_pct: float = 0.008,
        timeframe: str = "15m",
        min_bars: Optional[int] = None,
    ) -> None:
        self.sma_period = max(2, sma_period)
        self.entry_band = max(0.0001, entry_band)
        self.stop_loss_pct = max(0.0001, stop_loss_pct)
        self.timeframe = timeframe
        self.min_bars = min_bars if min_bars is not None else self.sma_period + 5

        # Duplicate-candle guard: skip if we've already processed this bar.
        self._last_processed_ts: pd.Timestamp = _SENTINEL_TS
        # Last evaluate() diagnostic (close/sma/deviation/band) for heartbeat logs.
        self.last_eval: Optional[dict] = None

    def apply_overrides(self, ov: dict) -> None:
        """Live-update tunable params from a hot-reload overrides dict (keys are
        prefixed ``mr_``). Only attributes that already exist are touched."""
        for key, val in ov.items():
            if not key.startswith("mr_"):
                continue
            attr = key[len("mr_"):]
            if not hasattr(self, attr):
                continue
            existing = getattr(self, attr)
            try:
                typed_val = type(existing)(val) if existing is not None else val
            except (TypeError, ValueError):
                typed_val = val
            setattr(self, attr, typed_val)

    # ─────────────────────────────────────────────────────────────────────────
    # Core maths
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_sma(close: pd.Series, period: int) -> pd.Series:
        """Simple moving average on the close series."""
        return close.rolling(period).mean()

    def _last_closed(self, df: pd.DataFrame) -> Optional[pd.Series]:
        """
        Return the last *fully closed* candle (iloc[-2]).

        Returns None when there are not enough rows or the SMA is NaN.
        """
        if len(df) < self.min_bars:
            logger.debug(
                "[MR] Not enough bars: need %d, got %d", self.min_bars, len(df)
            )
            return None

        candle = df.iloc[-2].copy()

        # SMA computed over the closed slice (iloc[:-1]) so the forming bar
        # at iloc[-1] never contaminates the indicator.
        sma_series = self._compute_sma(df["close"].iloc[:-1], self.sma_period)
        sma_val = sma_series.iloc[-1]

        if pd.isna(sma_val):
            logger.debug("[MR] SMA is NaN — insufficient history or all-NaN close values")
            return None

        candle["sma"] = float(sma_val)
        return candle

    # ─────────────────────────────────────────────────────────────────────────
    # Signal quality score helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _signal_score(self, close: float, sma: float, side: PositionSide) -> float:
        """
        Compute a [0, 1] quality score for the signal.

        The further price has deviated from SMA (beyond the entry_band),
        the stronger the mean-reversion thesis.  Score is capped at 0.95
        to leave room for LLM fusion to push over 1.0.

        Formula:
            deviation = |close - sma| / sma
            excess     = deviation - entry_band          (≥ 0 by construction)
            raw_score  = 0.60 + min(excess / entry_band, 1.0) * 0.35
            clamped    = clip(raw_score, 0.60, 0.95)
        """
        deviation = abs(close - sma) / sma if sma > 0 else 0.0
        excess = max(deviation - self.entry_band, 0.0)
        normalised = min(excess / self.entry_band, 1.0) if self.entry_band > 0 else 0.0
        raw = 0.60 + normalised * 0.35
        return float(np.clip(raw, 0.60, 0.95))

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def evaluate(self, df: pd.DataFrame) -> Optional[dict]:
        """
        Evaluate mean-reversion entry signal on the provided kline DataFrame.

        Parameters
        ----------
        df : pd.DataFrame
            OHLCV DataFrame with columns [open, high, low, close, volume]
            and a DatetimeTZAware 'timestamp' or DatetimeIndex.
            Must be in ascending chronological order.

        Returns
        -------
        dict or None
            ``None`` when no signal is present or preconditions are unmet.
            Otherwise a setup dict (see module docstring).
        """
        if df is None or len(df) < self.min_bars:
            return None

        candle = self._last_closed(df)
        if candle is None:
            return None

        # ── Duplicate-candle guard ────────────────────────────────────────────
        ts_raw = candle.get("timestamp") or candle.get("open_time")
        if ts_raw is not None:
            try:
                candle_ts = pd.Timestamp(ts_raw).tz_localize("UTC") if pd.Timestamp(ts_raw).tzinfo is None else pd.Timestamp(ts_raw)
            except Exception:
                candle_ts = _SENTINEL_TS
        else:
            candle_ts = _SENTINEL_TS

        if candle_ts != _SENTINEL_TS and candle_ts == self._last_processed_ts:
            logger.debug("[MR] Candle %s already processed — skipping duplicate", candle_ts)
            return None

        close = float(candle["close"])
        sma = float(candle["sma"])
        if sma <= 0 or close <= 0:
            return None

        lower_trigger = sma * (1.0 - self.entry_band)
        upper_trigger = sma * (1.0 + self.entry_band)

        # Diagnostic snapshot for the engine's per-tick heartbeat log (set even
        # when there is no signal, so observers can see how close we are).
        signed_dev = (close - sma) / sma if sma > 0 else 0.0
        self.last_eval = {
            "close": close, "sma": sma,
            "deviation": signed_dev,            # signed fraction vs SMA
            "band": self.entry_band,
            "lower": lower_trigger, "upper": upper_trigger,
        }

        side: Optional[PositionSide] = None
        reason: str = ""

        if close <= lower_trigger:
            side = PositionSide.LONG
            reason = (
                f"Oversold: close {close:.4f} <= SMA{self.sma_period} "
                f"lower band {lower_trigger:.4f} (SMA={sma:.4f})"
            )
        elif close >= upper_trigger:
            side = PositionSide.SHORT
            reason = (
                f"Overbought: close {close:.4f} >= SMA{self.sma_period} "
                f"upper band {upper_trigger:.4f} (SMA={sma:.4f})"
            )
        else:
            logger.debug(
                "[MR] No signal: close=%.4f lower=%.4f upper=%.4f sma=%.4f",
                close, lower_trigger, upper_trigger, sma,
            )
            return None

        # Hard stop-loss price
        if side == PositionSide.LONG:
            sl_price = close * (1.0 - self.stop_loss_pct)
        else:
            sl_price = close * (1.0 + self.stop_loss_pct)

        # Soft TP: price reverts to SMA (the mean)
        tp_price = sma

        score = self._signal_score(close, sma, side)

        # Mark candle as processed
        self._last_processed_ts = candle_ts

        setup = {
            "side": side,
            "entry_price": close,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "score": score,
            "reason": reason,
            "name": self.name,
            "strategy": "mean_reversion",
            # Required by wallet.open_position()
            "playbook": Playbook.INTRADAY,
            # Extra context passed to alerts / journal
            "sma": sma,
            "sma_period": self.sma_period,
            "entry_band": self.entry_band,
            "stop_loss_pct": self.stop_loss_pct,
            "candle_ts": str(candle_ts),
        }
        logger.info(
            "[MR SIGNAL] %s | close=%.4f | SMA%d=%.4f | "
            "lower=%.4f | upper=%.4f | sl=%.4f | tp=%.4f | score=%.2f",
            side.value, close, self.sma_period, sma,
            lower_trigger, upper_trigger, sl_price, tp_price, score,
        )
        return setup

    def check_sma_exit(
        self,
        df: pd.DataFrame,
        open_side: PositionSide,
    ) -> Optional[str]:
        """
        Check whether price has reverted to the SMA (mean-reversion exit).

        Called each signal tick while a position is open.

        Parameters
        ----------
        df        : current kline DataFrame (same series used for entries)
        open_side : PositionSide.LONG or PositionSide.SHORT

        Returns
        -------
        str   — human-readable exit reason when exit is triggered.
        None  — no exit signal yet.
        """
        if df is None or len(df) < self.min_bars:
            return None

        candle = self._last_closed(df)
        if candle is None:
            return None

        close = float(candle["close"])
        sma = float(candle["sma"])
        if sma <= 0:
            return None

        if open_side == PositionSide.LONG and close >= sma:
            return (
                f"MR_SMA_EXIT_LONG: close {close:.4f} >= SMA{self.sma_period} {sma:.4f} "
                f"— mean reverted"
            )

        if open_side == PositionSide.SHORT and close <= sma:
            return (
                f"MR_SMA_EXIT_SHORT: close {close:.4f} <= SMA{self.sma_period} {sma:.4f} "
                f"— mean reverted"
            )

        return None
