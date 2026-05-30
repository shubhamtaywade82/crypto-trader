"""
crypto_trader.backtest.smc_engine — bar-by-bar replay of the SMC pipeline.

Faithful: calls the LIVE ``PlaybookSMC.evaluate`` on trailing windows, so the
entry decision (HTF gate → sweep → BOS/CHoCH → score ≥ min_score) is byte-for-byte
what the live engine would make. Models the parts the playbook doesn't: fill,
hard SL/TP (intrabar), round-trip taker fees, leverage + liquidation.

Fill model (conservative — does not flatter the strategy):
  * Decision on the last CLOSED bar i (window ends with i as iloc[-1]).
  * Entry at that bar's CLOSE (the price evaluate() decided on).
  * TP/SL checked intrabar via high/low; fill assumed at the level.
  * Optional max-hold cap (bars) as a time-stop.
  * Round-trip taker fee charged on notional both legs.
  * Liquidation flagged if adverse excursion exceeds ~1/leverage (isolated).

HTF alignment is by TIMESTAMP (htf bars whose close_time ≤ the primary bar's
close_time), so a 4h bias never peeks at a candle that hasn't closed yet — the
off-by-frame lookahead present in the ST2 backtest is avoided here.

KNOWN SIMPLIFICATIONS (documented, not hidden):
  * Liquidation intensity is passed as 0.0 — historical per-symbol liquidation
    volume isn't in the kline feed, so the cascade gate is effectively off in
    backtest (live computes it from forceOrders). Real cascades that would block
    a live entry are NOT blocked here → backtest is slightly optimistic.
  * OI factor is dropped (oi_available=False), same as live today.
  * RVOL is computed on the entry-TF window (live computes it on the 1h frame).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

from ..journal import TradeOutcomeRecord
from ..regime import calculate_rvol
from ..strategies.smc_pipeline import PlaybookSMC
from ..wallet import PositionSide

logger = logging.getLogger("crypto_trader.backtest.smc")

_TF_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480, "12h": 720, "1d": 1440,
}


@dataclass
class BacktestResult:
    trades: List[TradeOutcomeRecord] = field(default_factory=list)
    liquidations: int = 0
    fee_drag_pct: float = 0.0
    bars: int = 0
    evaluated: int = 0          # bars where a window was scored
    signals: int = 0           # bars where evaluate() returned a setup
    symbol: str = ""
    timeframe: str = ""
    htf_timeframe: str = ""
    leverage: int = 1


def _ts_ms(times, idx: int) -> int:
    try:
        return int(pd.Timestamp(times.iloc[idx]).timestamp() * 1000)
    except Exception:
        return idx


def _epoch_close(df: pd.DataFrame) -> np.ndarray:
    col = "close_time" if "close_time" in df.columns else "open_time"
    return (pd.to_datetime(df[col], utc=True).astype("int64") // 10**9).to_numpy()


def run_backtest_smc(
    df_primary: pd.DataFrame,
    df_htf: pd.DataFrame,
    *,
    symbol: str = "SYM",
    timeframe: str = "15m",
    htf_timeframe: str = "4h",
    swing_window: int = 3,
    min_score: float = 70.0,
    sweep_lookback: int = 12,
    structure_lookback: int = 12,
    volume_mult_target: float = 3.0,
    rvol_target: float = 1.2,
    liq_cascade_threshold: float = 0.5,
    atr_period: int = 14,
    sl_buffer_atr: float = 0.5,
    sl_atr_mult: float = 1.5,
    tp_rr: float = 2.0,
    max_hold_hours: int = 24,
    window_bars: int = 150,
    taker_fee: float = 0.00059,
    leverage: int = 1,
    max_hold_bars: int = 0,
    entry_mode: str = "signal",     # "signal" = market at signal close; "retrace" = limit at FVG/OB
    retrace_timeout: int = 8,       # bars to wait for a limit fill (retrace mode)
) -> BacktestResult:
    """Replay the SMC pipeline over OHLCV ``df_primary`` (ascending) with HTF
    bias from ``df_htf``. Returns a BacktestResult of TradeOutcomeRecords.

    entry_mode:
      * "signal"  — enter at the signal candle's CLOSE (top of displacement).
      * "retrace" — place a LIMIT at the 50% FVG fill (guide Phase 3); enter only
                    if price retraces into it within ``retrace_timeout`` bars, with
                    the stop anchored just beyond the sweep wick (structural
                    invalidation) and a fixed ``tp_rr``-R target. Models the entry
                    timing the guide actually prescribes — and which the live
                    market-execution path can't currently do.
    """

    pb = PlaybookSMC(
        entry_timeframe=timeframe, htf_timeframe=htf_timeframe,
        swing_window=swing_window, min_score=min_score,
        sweep_lookback=sweep_lookback, structure_lookback=structure_lookback,
        volume_mult_target=volume_mult_target, rvol_target=rvol_target,
        liq_cascade_threshold=liq_cascade_threshold, atr_period=atr_period,
        sl_buffer_atr=sl_buffer_atr, sl_atr_mult=sl_atr_mult, tp_rr=tp_rr,
        max_hold_hours=max_hold_hours,
    )

    n = len(df_primary)
    res = BacktestResult(
        symbol=symbol, timeframe=timeframe, htf_timeframe=htf_timeframe,
        leverage=leverage, bars=n,
    )
    win = max(pb._min_bars(), window_bars)
    if n < win + 2 or df_htf is None or len(df_htf) < 30:
        return res

    # Default time-stop from hold-hours when not given explicitly.
    if not max_hold_bars:
        tf_min = _TF_MINUTES.get(timeframe, 15)
        max_hold_bars = max(1, int(max_hold_hours * 60 / tf_min))

    highs = df_primary["high"].astype(float).values
    lows = df_primary["low"].astype(float).values
    closes = df_primary["close"].astype(float).values
    times = df_primary["close_time"] if "close_time" in df_primary.columns else df_primary["open_time"]

    pri_close_epoch = _epoch_close(df_primary)
    htf_close_epoch = _epoch_close(df_htf)
    liq_frac = 1.0 / max(leverage, 1)

    i = win
    while i < n - 1:
        # Decision on bar i (last CLOSED): window ends with i at iloc[-1].
        window = df_primary.iloc[i - win + 1: i + 1]
        # HTF bars that have already closed by the time bar i closed (no lookahead).
        htf_end = int(np.searchsorted(htf_close_epoch, pri_close_epoch[i], side="right"))
        htf_window = df_htf.iloc[:htf_end]
        res.evaluated += 1

        if len(htf_window) < 30:
            i += 1
            continue

        rvol = calculate_rvol(window, window=20)
        setup = pb.evaluate(
            window, htf_window,
            oi_delta=None, oi_available=False,
            rvol=rvol, liquidation_intensity=0.0,
        )
        if not setup:
            i += 1
            continue
        res.signals += 1

        side = setup["side"]
        is_long = side == PositionSide.LONG

        # ── Resolve entry (idx, price, SL, TP) per entry_mode ───────────────────
        if entry_mode == "retrace":
            limit = setup.get("limit_entry")
            if limit is None:
                i += 1
                continue  # no FVG/OB zone to rest a limit on
            swept = float(setup["sweep_price"])
            atr_v = float(setup["atr"])
            limit = float(limit)
            if is_long:
                sl_px = swept - sl_buffer_atr * atr_v
                if sl_px >= limit:
                    sl_px = limit - 0.5 * atr_v
            else:
                sl_px = swept + sl_buffer_atr * atr_v
                if sl_px <= limit:
                    sl_px = limit + 0.5 * atr_v
            risk = abs(limit - sl_px)
            tp_px = (limit + tp_rr * risk) if is_long else (limit - tp_rr * risk)

            # Wait for a tap into the limit within the timeout window.
            fill_idx = None
            jj = i + 1
            stop = min(n, i + 1 + retrace_timeout)
            while jj < stop:
                if is_long and lows[jj] <= limit:
                    fill_idx = jj; break
                if (not is_long) and highs[jj] >= limit:
                    fill_idx = jj; break
                jj += 1
            if fill_idx is None:
                i += 1
                continue  # limit never filled → cancel, look for the next setup
            entry_idx = fill_idx
            entry_px = limit
        else:
            entry_idx = i
            entry_px = float(setup["entry_price"])
            sl_px = float(setup["sl_price"])
            tp_px = float(setup.get("tp_price", 0)) or None

        exit_idx = exit_px = exit_reason = None
        liquidated = False

        j = entry_idx + 1
        while j < n:
            hi, lo, cl = highs[j], lows[j], closes[j]

            # 1) TP intrabar (checked before SL — standard optimistic convention)
            if tp_px is not None:
                if side == PositionSide.LONG and hi >= tp_px:
                    exit_idx, exit_px, exit_reason = j, tp_px, "TP"; break
                if side == PositionSide.SHORT and lo <= tp_px:
                    exit_idx, exit_px, exit_reason = j, tp_px, "TP"; break
            # 2) hard SL intrabar
            if side == PositionSide.LONG and lo <= sl_px:
                exit_idx, exit_px, exit_reason = j, sl_px, "SL"; break
            if side == PositionSide.SHORT and hi >= sl_px:
                exit_idx, exit_px, exit_reason = j, sl_px, "SL"; break
            # 3) liquidation (adverse excursion beyond 1/lev)
            if side == PositionSide.LONG and (entry_px - lo) / entry_px >= liq_frac:
                exit_idx, exit_px, exit_reason = j, entry_px * (1 - liq_frac), "LIQUIDATION"
                liquidated = True; break
            if side == PositionSide.SHORT and (hi - entry_px) / entry_px >= liq_frac:
                exit_idx, exit_px, exit_reason = j, entry_px * (1 + liq_frac), "LIQUIDATION"
                liquidated = True; break
            # 4) time stop
            if max_hold_bars and (j - entry_idx) >= max_hold_bars:
                exit_idx, exit_px, exit_reason = j, cl, "TIME_STOP"; break
            j += 1

        if exit_idx is None:
            break  # ran out of data with position open — drop (no lookahead)

        if side == PositionSide.LONG:
            move = (exit_px - entry_px) / entry_px
        else:
            move = (entry_px - exit_px) / entry_px
        fee = taker_fee * 2.0
        realized = (move - fee) * entry_px
        risk_per_unit = abs(entry_px - sl_px)
        pnl_r = ((exit_px - entry_px) if side == PositionSide.LONG else (entry_px - exit_px)) / risk_per_unit \
            if risk_per_unit > 0 else None

        res.trades.append(TradeOutcomeRecord(
            trade_id=f"{symbol}-{entry_idx}",
            symbol=symbol, side=side.value,
            opened_at=_ts_ms(times, entry_idx), closed_at=_ts_ms(times, exit_idx),
            entry_price=entry_px, exit_price=exit_px, quantity=1.0,
            realized_pnl=realized, holding_time_s=float(exit_idx - entry_idx),
            exit_reason=exit_reason, regime="backtest",
            stop_loss_pct=risk_per_unit / entry_px if entry_px else None,
            leverage=leverage, pnl_r=pnl_r,
        ))
        res.fee_drag_pct += fee * 100.0
        if liquidated:
            res.liquidations += 1

        i = exit_idx + 1

    return res
