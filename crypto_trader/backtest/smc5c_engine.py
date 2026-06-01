"""
crypto_trader.backtest.smc5c_engine — replay of the SMC 5-Condition strategy.

Cached/causal. Reuses the SAME rule helpers as the live ``PlaybookSMC5C``
(trend_dir / bos_dir / fvg_recent) so live and backtest cannot drift. HTF series
(15m/1h/4h) aligned to each entry bar by timestamp (searchsorted — no lookahead).
Models intrabar TP/SL, fees, leverage, liquidation. Used by the champion selector
to score this blueprint OOS against the others.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..journal import TradeOutcomeRecord
from ..regime import compute_atr, compute_ema
from ..strategies.smc_5c import trend_dir, bos_dir, fvg_recent
from ..wallet import PositionSide

logger = logging.getLogger("crypto_trader.backtest.smc5c")

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


def run_backtest_smc5c(
    df_entry: pd.DataFrame,
    htf_frames: Dict[str, pd.DataFrame],
    *,
    symbol: str = "SYM",
    timeframe: str = "5m",
    ema_fast: int = 50,
    ema_slow: int = 200,
    bos_lookback: int = 5,
    fvg_window: int = 10,
    atr_period: int = 14,
    vol_window: int = 200,
    atr_pctile: float = 60.0,
    vol_pctile: float = 70.0,
    tp_pct: float = 0.010,
    sl_pct: float = 0.005,
    max_hold_hours: int = 4,
    taker_fee: float = 0.00059,
    leverage: int = 1,
    max_hold_bars: int = 0,
) -> BacktestResult:
    n = len(df_entry)
    res = BacktestResult(symbol=symbol, timeframe=timeframe, leverage=leverage, bars=n)
    win = max(atr_period + 2, vol_window + 2, 60)
    if n < win + 2:
        return res
    if not max_hold_bars:
        max_hold_bars = max(1, int(max_hold_hours * 60 / _TF_MINUTES.get(timeframe, 5)))

    highs = df_entry["high"].astype(float).values
    lows = df_entry["low"].astype(float).values
    closes = df_entry["close"].astype(float).values
    vols = df_entry["volume"].astype(float).values
    times = df_entry["close_time"] if "close_time" in df_entry.columns else df_entry["open_time"]
    atr_arr = compute_atr(df_entry, atr_period).to_numpy()
    pri_ep = _epoch_close(df_entry)

    # Index maps: for each entry bar, the last-closed HTF bar (causal).
    htf_idx = {}
    for tf in ("15m", "1h", "4h"):
        hdf = htf_frames.get(tf)
        if hdf is None or len(hdf) < 5:
            htf_idx[tf] = None
            continue
        h_ep = _epoch_close(hdf)
        htf_idx[tf] = (hdf, np.clip(np.searchsorted(h_ep, pri_ep, side="right") - 1, 0, len(hdf) - 1))

    liq_frac = 1.0 / max(leverage, 1)
    i = win
    while i < n - 1:
        # 1) 4H trend (slice HTF up to its causal index)
        h4 = htf_idx.get("4h")
        if h4 is None:
            break
        hdf4, idx4 = h4
        k4 = int(idx4[i])
        if k4 < ema_slow + 2:
            i += 1; continue
        t = trend_dir(hdf4["close"].iloc[:k4 + 1], ema_fast, ema_slow)
        if t == 0:
            i += 1; continue
        is_long = t == 1
        # 2) 1H BOS
        h1 = htf_idx.get("1h")
        if h1 is None:
            i += 1; continue
        hdf1, idx1 = h1
        b = bos_dir(hdf1.iloc[:int(idx1[i]) + 1], bos_lookback)
        if (is_long and b != 1) or (not is_long and b != -1):
            i += 1; continue
        # 3) 15m FVG
        h15 = htf_idx.get("15m")
        if h15 is None:
            i += 1; continue
        hdf15, idx15 = h15
        f = fvg_recent(hdf15.iloc[:int(idx15[i]) + 1], fvg_window)
        if (is_long and not (f & 1)) or (not is_long and not (f & 2)):
            i += 1; continue
        # 4) ATR percentile
        a = atr_arr[max(0, i - vol_window + 1):i + 1]
        a = a[np.isfinite(a)]
        if len(a) < 5 or atr_arr[i] <= np.percentile(a, atr_pctile):
            i += 1; continue
        # 5) volume percentile
        vw = vols[max(0, i - vol_window + 1):i + 1]
        if len(vw) < 5 or vols[i] <= np.percentile(vw, vol_pctile):
            i += 1; continue

        res.signals += 1
        entry_px = float(closes[i])
        if is_long:
            sl_px = entry_px * (1 - sl_pct); tp_px = entry_px * (1 + tp_pct); side = PositionSide.LONG
        else:
            sl_px = entry_px * (1 + sl_pct); tp_px = entry_px * (1 - tp_pct); side = PositionSide.SHORT
        risk = abs(entry_px - sl_px)
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
