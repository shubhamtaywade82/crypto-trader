"""
crypto_trader.strategies.supertrend2 — Supertrend2 strategy
=============================================================
Python port of the Pine Script ``Supertrend2 - Adaptive ATR`` strategy.

Key concepts:
  - customATR(): Wilder's RMA-smoothed ATR that accepts a float length,
    allowing the ATR period to scale with the chart timeframe.
  - compute_supertrend2(): replicates the Pine Script supertrend2() function.
  - adaptive_atr_length(): scales the base ATR period to the timeframe.
  - PlaybookSupertrend2: drop-in playbook for the engine.

Entry modes
    ``flip``         — enter on the bar where the Supertrend direction flips.
    ``retracement``  — enter when price pulls back within *retracement_pct* %
                       of the flip bar close (gives a slightly better entry).

Exit (engine-side)
    The engine calls ``check_flip_exit()`` every signal tick; when the
    Supertrend direction has flipped against the open position it closes it.
    The setup also includes a hard SL (Supertrend line / fixed % / ATR-based)
    which the wallet's normal SL check handles as a catastrophic backstop.
"""

from __future__ import annotations

import logging
from typing import Optional, Dict, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("crypto_trader.strategies.supertrend2")


# ─────────────────────────────────────────────────────────────────────────────
# 1.  Core maths
# ─────────────────────────────────────────────────────────────────────────────

def _wilder_atr(df: pd.DataFrame, length: int) -> pd.Series:
    """Wilder's RMA (EWM alpha=1/length) ATR — matches Pine Script customATR()."""
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values

    tr_arr = np.empty(len(df))
    tr_arr[0] = high[0] - low[0]
    for i in range(1, len(df)):
        tr_arr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )

    alpha = 1.0 / max(length, 1)
    atr_arr = np.empty(len(df))
    atr_arr[0] = tr_arr[0]
    for i in range(1, len(df)):
        atr_arr[i] = atr_arr[i - 1] + alpha * (tr_arr[i] - atr_arr[i - 1])

    return pd.Series(atr_arr, index=df.index, name="atr")


def adaptive_atr_length(base: int, timeframe: str) -> int:
    """
    Scale ATR period to the chart timeframe — mirrors Pine Script's
    ``adaptiveAtrLength`` calculation.

    Returns a positive integer.
    """
    tf = str(timeframe).lower().strip()

    # Minutes
    if tf.endswith("m"):
        try:
            mins = int(tf[:-1])
        except ValueError:
            return base
        if mins <= 1:
            return base
        if mins <= 5:
            return max(1, round(base * 1.2))
        if mins <= 15:
            return max(1, round(base * 1.5))
        if mins <= 60:
            return max(1, round(base * 2.0))
        return max(1, round(base * 3.0))

    # Hours
    if tf.endswith("h"):
        try:
            h = int(tf[:-1])
        except ValueError:
            return base
        if h <= 1:
            return max(1, round(base * 2.0))
        if h <= 4:
            return max(1, round(base * 3.0))
        return max(1, round(base * 4.0))

    # Daily / weekly / monthly (including Binance "1d"/"1w" format)
    if tf in ("1d", "d", "day", "1day"):
        return max(1, round(base * 4.0))
    if tf in ("1w", "w", "week", "1week"):
        return max(1, round(base * 8.0))
    if tf in ("1mo", "mo", "month", "1month"):
        return max(1, round(base * 12.0))

    return base


def compute_supertrend2(
    df: pd.DataFrame,
    factor: float = 3.0,
    atr_length: int = 10,
) -> Tuple[pd.Series, pd.Series]:
    """
    Supertrend2 — replicates Pine Script's supertrend2() function.

    Uses Wilder's ATR (series-float length) so it can be adapted per timeframe.

    Returns
    -------
    st : pd.Series
        Supertrend line values.
    direction : pd.Series[int]
        -1 = uptrend, +1 = downtrend  (same sign convention as ta.supertrend).
    """
    atr = _wilder_atr(df, atr_length)
    src = (df["high"] + df["low"]) / 2.0  # hl2

    upper_basic = (src + factor * atr).values.astype(float)
    lower_basic = (src - factor * atr).values.astype(float)
    close_arr = df["close"].values.astype(float)
    n = len(df)

    upper = upper_basic.copy()
    lower = lower_basic.copy()
    direction = np.full(n, -1, dtype=np.int8)
    st = np.full(n, np.nan, dtype=float)

    # Initial bar
    st[0] = lower[0]
    direction[0] = -1

    for i in range(1, n):
        prev_close = close_arr[i - 1]
        prev_upper = upper[i - 1]
        prev_lower = lower[i - 1]

        if prev_close > prev_upper:
            direction[i] = -1
            # Don't trail lower band when switching to uptrend — use fresh band
            st[i] = lower[i]
        elif prev_close < prev_lower:
            direction[i] = 1
            st[i] = upper[i]
        else:
            direction[i] = direction[i - 1]
            if direction[i] == -1:
                lower[i] = max(lower[i], prev_lower)  # trail up
                st[i] = lower[i]
            else:
                upper[i] = min(upper[i], prev_upper)  # trail down
                st[i] = upper[i]

    return (
        pd.Series(st, index=df.index, name="supertrend"),
        pd.Series(direction.astype(int), index=df.index, name="st_direction"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2.  PlaybookSupertrend2
# ─────────────────────────────────────────────────────────────────────────────

class PlaybookSupertrend2:
    """
    Supertrend2 entry signal generator.

    Parameters
    ----------
    atr_period : int
        Base ATR length (scaled if ``use_adaptive_atr=True``).
    factor : float
        Supertrend multiplier.
    use_adaptive_atr : bool
        When True, ``atr_period`` is automatically scaled to the timeframe.
    timeframe : str
        Primary signal timeframe (e.g. ``"1h"``, ``"15m"``).
    entry_mode : str
        ``"flip"``       — enter immediately on the direction-change bar.
        ``"retracement"``— enter when price pulls back within
                           ``retracement_pct``% of the flip close.
    retracement_pct : float
        Maximum pull-back from the flip close to still be considered an entry
        (retracement mode only).
    flip_lookback : int
        How many bars to look back for a valid flip (retracement mode).
    use_htf_filter : bool
        Require higher-timeframe Supertrend to align with the signal direction.
    use_vol_filter : bool
        Require RVOL >= ``vol_multiplier``.
    vol_multiplier : float
        RVOL threshold.
    use_adx_filter : bool
        Require ADX >= ``adx_threshold``.
    adx_threshold : int
        Minimum ADX for entry.
    tp1_pct : float
        Take-profit percentage from entry price.
    use_tp : bool
        When False the position exits ONLY via Supertrend flip (no TP).
    sl_mode : str
        ``"supertrend"`` — SL = current Supertrend line (trailing).
        ``"fixed_pct"``  — SL = entry_price ± ``sl_pct``%.
        ``"atr"``        — SL = entry_price ± ATR × ``sl_atr_mult``.
    sl_pct : float
        SL distance in % (fixed_pct mode).
    sl_atr_mult : float
        SL ATR multiplier (atr mode).
    max_hold_hours : int
        Safety time-stop.  Passed as ``time_stop_hours`` in the setup; the
        wallet's normal time-stop logic handles it.  Defaults to 7 days so the
        Supertrend flip can exit first in virtually all circumstances.
    """

    name = "playbook_supertrend2"

    def __init__(
        self,
        atr_period: int = 10,
        factor: float = 3.0,
        use_adaptive_atr: bool = True,
        timeframe: str = "1h",
        htf_timeframe: str = "4h",
        entry_mode: str = "flip",
        retracement_pct: float = 1.0,
        flip_lookback: int = 5,
        use_htf_filter: bool = True,
        use_vol_filter: bool = False,
        vol_multiplier: float = 1.2,
        use_adx_filter: bool = False,
        adx_threshold: int = 20,
        tp1_pct: float = 1.0,
        use_tp: bool = True,
        sl_mode: str = "supertrend",
        sl_pct: float = 1.5,
        sl_atr_mult: float = 2.5,
        max_hold_hours: int = 168,
    ):
        self.atr_period = max(1, atr_period)
        self.factor = max(0.1, factor)
        self.use_adaptive_atr = use_adaptive_atr
        self.timeframe = timeframe
        self.htf_timeframe = htf_timeframe
        self.entry_mode = entry_mode
        self.retracement_pct = max(0.0, retracement_pct)
        self.flip_lookback = max(1, flip_lookback)
        self.use_htf_filter = use_htf_filter
        self.use_vol_filter = use_vol_filter
        self.vol_multiplier = max(0.1, vol_multiplier)
        self.use_adx_filter = use_adx_filter
        self.adx_threshold = max(1, adx_threshold)
        self.tp1_pct = max(0.0, tp1_pct)
        self.use_tp = use_tp
        self.sl_mode = sl_mode
        self.sl_pct = max(0.0, sl_pct)
        self.sl_atr_mult = max(0.0, sl_atr_mult)
        self.max_hold_hours = max(1, max_hold_hours)

    def apply_overrides(self, ov: dict) -> None:
        """Live-update tunable params from a hot-reload overrides dict (keys are
        prefixed ``st2_``). Only attributes that already exist are touched.
        SL-defining keys (sl_*, max_hold_hours) are gated upstream to flat-only."""
        for key, val in ov.items():
            if not key.startswith("st2_"):
                continue
            attr = key[len("st2_"):]
            if not hasattr(self, attr):
                continue
            existing = getattr(self, attr)
            try:
                typed_val = type(existing)(val) if existing is not None else val
            except (TypeError, ValueError):
                typed_val = val
            setattr(self, attr, typed_val)

    # ── helpers ──

    def _effective_atr_len(self) -> int:
        if self.use_adaptive_atr:
            return adaptive_atr_length(self.atr_period, self.timeframe)
        return self.atr_period

    def _min_bars(self) -> int:
        return max(self._effective_atr_len() * 3, 20)

    # ── public API ──

    def evaluate(
        self,
        df_1h: pd.DataFrame,
        df_htf: Optional[pd.DataFrame] = None,
    ) -> Optional[Dict]:
        """
        Generate a trade setup from the latest bar of ``df_1h``.

        Parameters
        ----------
        df_1h : pd.DataFrame
            Primary timeframe OHLCV data (expected 1H candles but works on any).
        df_htf : pd.DataFrame, optional
            Higher-timeframe data for the HTF alignment filter (4H default).

        Returns
        -------
        dict  — compatible setup for ``engine_ws._execute_entry``
        None  — no signal this tick
        """
        from ..wallet import PositionSide, Playbook  # local import avoids circular dep

        min_bars = self._min_bars()
        if len(df_1h) < min_bars + 2:
            logger.debug("[ST2] Not enough bars: %d < %d", len(df_1h), min_bars + 2)
            return None

        atr_len = self._effective_atr_len()
        st, direction = compute_supertrend2(df_1h, self.factor, atr_len)

        curr_dir = int(direction.iloc[-1])
        prev_dir = int(direction.iloc[-2])
        curr_st = float(st.iloc[-1])
        curr_close = float(df_1h["close"].iloc[-1])

        # ── HTF filter ──────────────────────────────────────────────────────
        if self.use_htf_filter and df_htf is not None and len(df_htf) >= min_bars + 2:
            htf_atr_len = (
                adaptive_atr_length(self.atr_period, self.htf_timeframe)
                if self.use_adaptive_atr
                else self.atr_period
            )
            _, htf_dir = compute_supertrend2(df_htf, self.factor, htf_atr_len)
            htf_curr = int(htf_dir.iloc[-1])
            if curr_dir != htf_curr:
                logger.debug(
                    "[ST2] HTF filter blocked: 1h_dir=%d htf_dir=%d", curr_dir, htf_curr
                )
                return None

        # ── Volume filter ────────────────────────────────────────────────────
        if self.use_vol_filter and "quote_volume" in df_1h.columns:
            avg_vol = df_1h["quote_volume"].rolling(20).mean().iloc[-1]
            curr_vol = float(df_1h["quote_volume"].iloc[-1])
            if pd.notna(avg_vol) and float(avg_vol) > 0:
                rvol = curr_vol / float(avg_vol)
                if rvol < self.vol_multiplier:
                    logger.debug("[ST2] Vol filter blocked: rvol=%.2f", rvol)
                    return None

        # ── ADX filter ───────────────────────────────────────────────────────
        if self.use_adx_filter and "adx" in df_1h.columns:
            adx_val = df_1h["adx"].iloc[-1]
            if pd.notna(adx_val) and float(adx_val) < self.adx_threshold:
                logger.debug("[ST2] ADX filter blocked: adx=%.1f", adx_val)
                return None

        # ── Entry signal ─────────────────────────────────────────────────────
        side: Optional[PositionSide] = None

        if self.entry_mode == "flip":
            # Enter on the exact bar the direction changed
            if curr_dir == -1 and prev_dir == 1:
                side = PositionSide.LONG
            elif curr_dir == 1 and prev_dir == -1:
                side = PositionSide.SHORT

        elif self.entry_mode == "retracement":
            side = self._retracement_signal(df_1h, curr_dir, curr_close)
            
        elif self.entry_mode == "continuous":
            # Enter immediately in the direction of the intact trend.
            # The engine already prevents double-entries when a position is open.
            if curr_dir == -1:
                side = PositionSide.LONG
            elif curr_dir == 1:
                side = PositionSide.SHORT

        if side is None:
            return None

        # ── SL calculation ───────────────────────────────────────────────────
        atr_val = float(_wilder_atr(df_1h, atr_len).iloc[-1])
        is_long = side == PositionSide.LONG
        entry_price = curr_close

        if self.sl_mode == "supertrend":
            sl = curr_st
        elif self.sl_mode == "atr":
            sl = (
                entry_price - atr_val * self.sl_atr_mult
                if is_long
                else entry_price + atr_val * self.sl_atr_mult
            )
        else:  # fixed_pct
            sl = (
                entry_price * (1.0 - self.sl_pct / 100.0)
                if is_long
                else entry_price * (1.0 + self.sl_pct / 100.0)
            )

        # Guard: SL must be on the correct side of entry
        if is_long and sl >= entry_price:
            logger.debug("[ST2] SL %.4f >= entry %.4f for LONG — clamping", sl, entry_price)
            sl = entry_price * 0.99
        elif not is_long and sl <= entry_price:
            logger.debug("[ST2] SL %.4f <= entry %.4f for SHORT — clamping", sl, entry_price)
            sl = entry_price * 1.01

        # ── TP calculation ───────────────────────────────────────────────────
        tp: Optional[float] = None
        if self.use_tp and self.tp1_pct > 0:
            tp = (
                entry_price * (1.0 + self.tp1_pct / 100.0)
                if is_long
                else entry_price * (1.0 - self.tp1_pct / 100.0)
            )

        setup: Dict = {
            "playbook": Playbook.INTRADAY,
            "side": side,
            "entry_price": entry_price,
            "sl_price": sl,
            "time_stop_hours": self.max_hold_hours,
            "score": 1.0,
            "reason": (
                f"ST2 {side.value} | mode={self.entry_mode} "
                f"factor={self.factor} atr_len={atr_len} "
                f"st={curr_st:.4f} sl_mode={self.sl_mode}"
            ),
        }
        if tp is not None:
            setup["tp_price"] = tp

        return setup

    # ── retracement helper ────────────────────────────────────────────────────

    def _retracement_signal(
        self,
        df: pd.DataFrame,
        curr_dir: int,
        curr_close: float,
    ) -> Optional["PositionSide"]:
        """
        Find the most recent Supertrend flip within ``flip_lookback`` bars and
        check whether the current price has pulled back enough.

        Returns PositionSide or None.
        """
        from ..wallet import PositionSide

        lookback = min(self.flip_lookback + 1, len(df))
        atr_len = self._effective_atr_len()
        _, dir_series = compute_supertrend2(df, self.factor, atr_len)
        dirs = dir_series.values[-lookback:]
        closes = df["close"].values[-lookback:]

        # Walk back from the second-most-recent bar to find a flip
        for i in range(len(dirs) - 2, 0, -1):
            d_i = int(dirs[i])
            d_prev = int(dirs[i - 1])

            if d_i == d_prev:
                continue  # no flip here

            flip_price = float(closes[i])
            # Ensure direction has NOT flipped again since the flip bar
            still_valid = all(int(dirs[j]) == d_i for j in range(i, len(dirs)))
            if not still_valid:
                break  # direction changed again; stale signal

            if d_i == -1:  # bullish flip
                lo = flip_price * (1.0 - self.retracement_pct / 100.0)
                hi = flip_price * (1.0 + self.retracement_pct / 100.0)
                if lo <= curr_close <= hi:
                    return PositionSide.LONG

            elif d_i == 1:  # bearish flip
                lo = flip_price * (1.0 - self.retracement_pct / 100.0)
                hi = flip_price * (1.0 + self.retracement_pct / 100.0)
                if lo <= curr_close <= hi:
                    return PositionSide.SHORT

            break  # only use the most recent flip

        return None

    # ── utility used by the engine for exit decisions ─────────────────────────

    def get_current_direction(
        self,
        df_1h: pd.DataFrame,
    ) -> Optional[int]:
        """
        Return current Supertrend direction (-1 uptrend / +1 downtrend) or
        None if there are insufficient bars.
        """
        if len(df_1h) < self._min_bars():
            return None
        _, direction = compute_supertrend2(df_1h, self.factor, self._effective_atr_len())
        return int(direction.iloc[-1])
