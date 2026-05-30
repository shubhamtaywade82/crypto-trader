"""
crypto_trader.backtest.regime_switch_engine — bar-by-bar replay of the regime switcher.

Identical to st2_engine except the playbook is PlaybookRegimeSwitch, which gates
Supertrend2 entries by market regime (ADX or ExtendedRegime).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from ..journal import TradeOutcomeRecord
from ..regime import compute_adx
from ..strategies.regime_switch import PlaybookRegimeSwitch
from ..wallet import PositionSide

logger = logging.getLogger("crypto_trader.backtest.regime_switch")


@dataclass
class BacktestResult:
    trades: List[TradeOutcomeRecord] = field(default_factory=list)
    liquidations: int = 0
    fee_drag_pct: float = 0.0
    bars: int = 0
    symbol: str = ""
    timeframe: str = ""
    htf_timeframe: str = ""
    leverage: int = 1


def _ts_ms(times, idx: int) -> int:
    try:
        return int(pd.Timestamp(times.iloc[idx]).timestamp() * 1000)
    except Exception:
        return idx


def run_backtest_regime_switch(
    df_primary: pd.DataFrame,
    df_htf: Optional[pd.DataFrame] = None,
    *,
    symbol: str = "SYM",
    timeframe: str = "1h",
    htf_timeframe: str = "4h",
    atr_period: int = 10,
    factor: float = 3.0,
    use_adaptive_atr: bool = True,
    entry_mode: str = "flip",
    retracement_pct: float = 1.0,
    flip_lookback: int = 5,
    use_htf_filter: bool = True,
    tp1_pct: float = 1.0,
    use_tp: bool = True,
    sl_mode: str = "supertrend",
    sl_pct: float = 1.5,
    sl_atr_mult: float = 2.5,
    max_hold_hours: int = 168,
    allowed_regimes: Optional[list] = None,
    taker_fee: float = 0.00059,
    leverage: int = 1,
    max_hold_bars: int = 0,
) -> BacktestResult:
    """Replay regime-switcher over OHLCV ``df_primary`` (ascending)."""

    pb = PlaybookRegimeSwitch(
        atr_period=atr_period, factor=factor, use_adaptive_atr=use_adaptive_atr,
        timeframe=timeframe, htf_timeframe=htf_timeframe,
        entry_mode=entry_mode, retracement_pct=retracement_pct,
        flip_lookback=flip_lookback, use_htf_filter=use_htf_filter,
        tp1_pct=tp1_pct, use_tp=use_tp, sl_mode=sl_mode,
        sl_pct=sl_pct, sl_atr_mult=sl_atr_mult, max_hold_hours=max_hold_hours,
        allowed_regimes=allowed_regimes,
    )

    n = len(df_primary)
    min_bars = pb.st2._min_bars() + 2
    res = BacktestResult(
        symbol=symbol, timeframe=timeframe, htf_timeframe=htf_timeframe,
        leverage=leverage, bars=n,
    )
    if n < min_bars + 2:
        return res

    highs = df_primary["high"].astype(float).values
    lows = df_primary["low"].astype(float).values
    closes = df_primary["close"].astype(float).values
    times = df_primary["close_time"] if "close_time" in df_primary.columns else df_primary["open_time"]

    # Pre-compute ADX for regime gate
    if "adx" not in df_primary.columns:
        try:
            df_primary = df_primary.copy()
            df_primary["adx"] = compute_adx(df_primary, period=14)
        except Exception:
            pass

    i = min_bars
    while i < n - 1:
        window = df_primary.iloc[i - min_bars + 1: i + 2].copy()
        htf_window = df_htf.iloc[: i + 2].copy() if df_htf is not None else None

        setup = pb.evaluate(window, df_htf=htf_window)
        if not setup:
            i += 1
            continue

        side = setup["side"]
        entry_px = float(setup["entry_price"])
        sl_px = float(setup["sl_price"])
        tp_px = float(setup.get("tp_price", 0)) or None
        entry_idx = i

        exit_idx = exit_px = exit_reason = None
        liquidated = False
        liq_frac = 1.0 / max(leverage, 1)

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

            flip_window = df_primary.iloc[j - min_bars + 1: j + 2].copy()
            curr_dir = pb.get_current_direction(flip_window)
            if curr_dir is not None:
                if side == PositionSide.LONG and curr_dir == 1:
                    exit_idx, exit_px, exit_reason = j, cl, "ST2_FLIP"; break
                if side == PositionSide.SHORT and curr_dir == -1:
                    exit_idx, exit_px, exit_reason = j, cl, "ST2_FLIP"; break

            if max_hold_bars and (j - entry_idx) >= max_hold_bars:
                exit_idx, exit_px, exit_reason = j, cl, "TIME_STOP"; break

            j += 1

        if exit_idx is None:
            break

        if side == PositionSide.LONG:
            move = (exit_px - entry_px) / entry_px
        else:
            move = (entry_px - exit_px) / entry_px
        fee = taker_fee * 2.0
        net_ret = move - fee
        qty = 1.0
        realized = net_ret * entry_px * qty
        risk_per_unit = abs(entry_px - sl_px)
        pnl_r = ((exit_px - entry_px) if side == PositionSide.LONG else (entry_px - exit_px)) / risk_per_unit \
            if risk_per_unit > 0 else None

        res.trades.append(TradeOutcomeRecord(
            trade_id=f"{symbol}-{entry_idx}",
            symbol=symbol, side=side.value,
            opened_at=_ts_ms(times, entry_idx), closed_at=_ts_ms(times, exit_idx),
            entry_price=entry_px, exit_price=exit_px, quantity=qty,
            realized_pnl=realized, holding_time_s=float((exit_idx - entry_idx)),
            exit_reason=exit_reason, regime="backtest",
            stop_loss_pct=abs(sl_px - entry_px) / entry_px, leverage=leverage, pnl_r=pnl_r,
        ))
        res.fee_drag_pct += fee * 100.0
        if liquidated:
            res.liquidations += 1

        i = exit_idx + 1

    return res
