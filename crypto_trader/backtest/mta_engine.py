"""
crypto_trader.backtest.mta_engine — replay of the MTF-alignment strategy.

Cached/vectorised (EMAs are causal → precompute once, O(n)). Uses the SAME rule
helpers as the live ``PlaybookMTFAlignment`` (macro_bias / engulfing) so live and
backtest cannot drift. HTF EMA50s are aligned to each entry bar by timestamp
(searchsorted — no lookahead). Models intrabar TP/SL, fees, leverage, liquidation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..journal import TradeOutcomeRecord
from ..regime import compute_ema, compute_atr
from ..strategies.mtf_alignment import macro_bias, _bullish_engulf, _bearish_engulf
from ..wallet import PositionSide

logger = logging.getLogger("crypto_trader.backtest.mta")

_TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60,
               "2h": 120, "4h": 240, "1d": 1440}


@dataclass
class BacktestResult:
    trades: List[TradeOutcomeRecord] = field(default_factory=list)
    liquidations: int = 0
    fee_drag_pct: float = 0.0
    bars: int = 0
    signals: int = 0
    symbol: str = ""
    timeframe: str = ""
    leverage: int = 1


def _epoch_close(df):
    col = "close_time" if "close_time" in df.columns else "open_time"
    return (pd.to_datetime(df[col], utc=True).astype("int64") // 10**9).to_numpy()


def _ts_ms(times, idx):
    try:
        return int(pd.Timestamp(times.iloc[idx]).timestamp() * 1000)
    except Exception:
        return idx


def run_backtest_mta(
    df_entry: pd.DataFrame,
    htf_frames: Dict[str, pd.DataFrame],
    *,
    symbol: str = "SYM",
    timeframe: str = "5m",
    macro_tfs: Optional[List[str]] = None,
    ema_len: int = 50,
    min_macro_frames: int = 2,
    break_lookback: int = 5,
    swing_lookback: int = 6,
    tp_r: float = 3.0,
    atr_period: int = 14,
    min_stop_frac: float = 0.0055,
    max_hold_hours: int = 24,
    taker_fee: float = 0.00059,
    leverage: int = 1,
    max_hold_bars: int = 0,
) -> BacktestResult:
    macro_tfs = macro_tfs or ["1h", "4h", "1d"]
    n = len(df_entry)
    res = BacktestResult(symbol=symbol, timeframe=timeframe, leverage=leverage, bars=n)
    win = max(break_lookback + 2, swing_lookback + 2, atr_period + 2, 30)
    if n < win + 2:
        return res
    if not max_hold_bars:
        max_hold_bars = max(1, int(max_hold_hours * 60 / _TF_MINUTES.get(timeframe, 5)))

    highs = df_entry["high"].astype(float).values
    lows = df_entry["low"].astype(float).values
    opens = df_entry["open"].astype(float).values
    closes = df_entry["close"].astype(float).values
    times = df_entry["close_time"] if "close_time" in df_entry.columns else df_entry["open_time"]
    pri_ep = _epoch_close(df_entry)

    # Per-entry-bar HTF EMA50 (causal): for each macro frame, EMA50 at the last
    # HTF bar closed by each entry bar's time.
    ema_cols = []
    for tf in macro_tfs:
        hdf = htf_frames.get(tf)
        if hdf is None or len(hdf) < ema_len + 2:
            ema_cols.append(None); continue
        h_ep = _epoch_close(hdf)
        h_ema = compute_ema(hdf["close"], ema_len).to_numpy()
        idx = np.clip(np.searchsorted(h_ep, pri_ep, side="right") - 1, 0, len(hdf) - 1)
        ema_cols.append(h_ema[idx])

    atr_arr = compute_atr(df_entry, atr_period).to_numpy()
    liq_frac = 1.0 / max(leverage, 1)

    i = win
    while i < n - 1:
        emas = [(col[i] if col is not None else None) for col in ema_cols]
        bias = macro_bias(float(closes[i]), emas, min_macro_frames)
        if bias == 0:
            i += 1; continue
        is_long = bias == 1
        prior_high = float(np.max(highs[i - break_lookback:i]))
        prior_low = float(np.min(lows[i - break_lookback:i]))
        breakout = (closes[i] > prior_high) if is_long else (closes[i] < prior_low)
        if not breakout:
            i += 1; continue
        eng = (_bullish_engulf(opens[i - 1], closes[i - 1], opens[i], closes[i]) if is_long
               else _bearish_engulf(opens[i - 1], closes[i - 1], opens[i], closes[i]))
        if not eng:
            i += 1; continue

        res.signals += 1
        entry_px = float(closes[i])
        atr = float(atr_arr[i]) if np.isfinite(atr_arr[i]) and atr_arr[i] > 0 else entry_px * 0.005
        if is_long:
            swing = float(np.min(lows[i - swing_lookback + 1:i + 1]))
            risk = max(entry_px - swing, entry_px * min_stop_frac)
            sl_px = entry_px - risk; tp_px = entry_px + tp_r * risk
            side = PositionSide.LONG
        else:
            swing = float(np.max(highs[i - swing_lookback + 1:i + 1]))
            risk = max(swing - entry_px, entry_px * min_stop_frac)
            sl_px = entry_px + risk; tp_px = entry_px - tp_r * risk
            side = PositionSide.SHORT
        entry_idx = i
        exit_idx = exit_px = exit_reason = None
        liquidated = False
        j = i + 1
        while j < n:
            hi, lo, cl = highs[j], lows[j], closes[j]
            if side == PositionSide.LONG and hi >= tp_px:
                exit_idx, exit_px, exit_reason = j, tp_px, "TP"; break
            if side == PositionSide.SHORT and lo <= tp_px:
                exit_idx, exit_px, exit_reason = j, tp_px, "TP"; break
            if side == PositionSide.LONG and lo <= sl_px:
                exit_idx, exit_px, exit_reason = j, sl_px, "SL"; break
            if side == PositionSide.SHORT and hi >= sl_px:
                exit_idx, exit_px, exit_reason = j, sl_px, "SL"; break
            if side == PositionSide.LONG and (entry_px - lo) / entry_px >= liq_frac:
                exit_idx, exit_px, exit_reason = j, entry_px * (1 - liq_frac), "LIQUIDATION"
                liquidated = True; break
            if side == PositionSide.SHORT and (hi - entry_px) / entry_px >= liq_frac:
                exit_idx, exit_px, exit_reason = j, entry_px * (1 + liq_frac), "LIQUIDATION"
                liquidated = True; break
            if max_hold_bars and (j - entry_idx) >= max_hold_bars:
                exit_idx, exit_px, exit_reason = j, cl, "TIME_STOP"; break
            j += 1
        if exit_idx is None:
            break
        move = ((exit_px - entry_px) if side == PositionSide.LONG else (entry_px - exit_px)) / entry_px
        fee = taker_fee * 2.0
        realized = (move - fee) * entry_px
        pnl_r = (((exit_px - entry_px) if side == PositionSide.LONG else (entry_px - exit_px)) / risk
                 if risk > 0 else None)
        res.trades.append(TradeOutcomeRecord(
            trade_id=f"{symbol}-{entry_idx}", symbol=symbol, side=side.value,
            opened_at=_ts_ms(times, entry_idx), closed_at=_ts_ms(times, exit_idx),
            entry_price=entry_px, exit_price=exit_px, quantity=1.0,
            realized_pnl=realized, holding_time_s=float(exit_idx - entry_idx),
            exit_reason=exit_reason, regime="backtest",
            stop_loss_pct=risk / entry_px if entry_px else None, leverage=leverage, pnl_r=pnl_r,
        ))
        res.fee_drag_pct += fee * 100.0
        if liquidated:
            res.liquidations += 1
        i = exit_idx + 1
    return res
