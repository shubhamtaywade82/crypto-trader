"""
crypto_trader.backtest.lsw_engine — bar-by-bar replay of PlaybookLiquiditySweep.

Faithful: calls the LIVE ``PlaybookLiquiditySweep.evaluate`` on trailing windows,
so entries are byte-for-byte the live decision. Models fill, intrabar SL/TP,
round-trip taker fees, leverage + liquidation. HTF frame aligned by timestamp
(no lookahead). Used by the offline champion selector to score this strategy
against the others on equal footing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

from ..journal import TradeOutcomeRecord
from ..strategies.liquidity_sweep import PlaybookLiquiditySweep
from ..wallet import PositionSide

logger = logging.getLogger("crypto_trader.backtest.lsw")

_TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60,
               "2h": 120, "4h": 240, "6h": 360, "8h": 480, "12h": 720, "1d": 1440}


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


def _ts_ms(times, idx):
    try:
        return int(pd.Timestamp(times.iloc[idx]).timestamp() * 1000)
    except Exception:
        return idx


def _epoch_close(df):
    col = "close_time" if "close_time" in df.columns else "open_time"
    return (pd.to_datetime(df[col], utc=True).astype("int64") // 10**9).to_numpy()


def run_backtest_lsw(
    df: pd.DataFrame,
    df_htf: Optional[pd.DataFrame] = None,
    *,
    symbol: str = "SYM",
    timeframe: str = "15m",
    htf_timeframe: str = "4h",
    swing_window: int = 3,
    sweep_lookback: int = 3,
    tp_r: float = 2.5,
    atr_period: int = 14,
    sl_buffer_atr: float = 0.25,
    min_stop_frac: float = 0.002,
    use_htf_filter: bool = False,
    max_hold_hours: int = 24,
    window_bars: int = 150,
    taker_fee: float = 0.00059,
    leverage: int = 1,
    max_hold_bars: int = 0,
) -> BacktestResult:
    pb = PlaybookLiquiditySweep(
        timeframe=timeframe, htf_timeframe=htf_timeframe, swing_window=swing_window,
        sweep_lookback=sweep_lookback, tp_r=tp_r, atr_period=atr_period,
        sl_buffer_atr=sl_buffer_atr, min_stop_frac=min_stop_frac,
        use_htf_filter=use_htf_filter, max_hold_hours=max_hold_hours,
    )
    n = len(df)
    res = BacktestResult(symbol=symbol, timeframe=timeframe, leverage=leverage, bars=n)
    win = max(pb._min_bars(), window_bars)
    if n < win + 2:
        return res
    if not max_hold_bars:
        max_hold_bars = max(1, int(max_hold_hours * 60 / _TF_MINUTES.get(timeframe, 15)))

    highs = df["high"].astype(float).values
    lows = df["low"].astype(float).values
    closes = df["close"].astype(float).values
    times = df["close_time"] if "close_time" in df.columns else df["open_time"]
    use_htf = use_htf_filter and df_htf is not None and len(df_htf) >= 30
    if use_htf:
        pri_ep = _epoch_close(df)
        htf_ep = _epoch_close(df_htf)
    liq_frac = 1.0 / max(leverage, 1)

    i = win
    while i < n - 1:
        window = df.iloc[i - win + 1: i + 1]
        htf_window = None
        if use_htf:
            end = int(np.searchsorted(htf_ep, pri_ep[i], side="right"))
            htf_window = df_htf.iloc[:end]
        setup = pb.evaluate(window, htf_window)
        if not setup:
            i += 1
            continue
        res.signals += 1
        side = setup["side"]
        entry_px = float(setup["entry_price"])
        sl_px = float(setup["sl_price"])
        tp_px = float(setup.get("tp_price", 0)) or None
        entry_idx = i
        exit_idx = exit_px = exit_reason = None
        liquidated = False
        j = i + 1
        while j < n:
            hi, lo, cl = highs[j], lows[j], closes[j]
            if tp_px is not None:
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
        risk = abs(entry_px - sl_px)
        pnl_r = (((exit_px - entry_px) if side == PositionSide.LONG else (entry_px - exit_px)) / risk
                 if risk > 0 else None)
        res.trades.append(TradeOutcomeRecord(
            trade_id=f"{symbol}-{entry_idx}", symbol=symbol, side=side.value,
            opened_at=_ts_ms(times, entry_idx), closed_at=_ts_ms(times, exit_idx),
            entry_price=entry_px, exit_price=exit_px, quantity=1.0,
            realized_pnl=realized, holding_time_s=float(exit_idx - entry_idx),
            exit_reason=exit_reason, regime="backtest",
            stop_loss_pct=risk / entry_px if entry_px else None,
            leverage=leverage, pnl_r=pnl_r,
        ))
        res.fee_drag_pct += fee * 100.0
        if liquidated:
            res.liquidations += 1
        i = exit_idx + 1
    return res
