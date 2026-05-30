"""
crypto_trader.backtest.engine — bar-by-bar replay of the mean-reversion strategy.

Faithful: calls the live ``PlaybookMeanReversion.evaluate`` / ``check_sma_exit``
on trailing windows, so the entry/exit decisions are byte-for-byte what the live
engine would make. Models the parts the playbook doesn't: fill, hard SL (intrabar),
SMA-touch exit, round-trip taker fees, leverage + liquidation, and exposes the
result as TradeOutcomeRecords so ``analytics.metrics`` produces the same stats the
live ``/metrics`` endpoint reports.

Fill model (deliberately conservative — does not flatter the strategy):
  * Entry  at the signal candle's CLOSE (the price evaluate() decided on).
  * SL hit if a later candle's low<=SL (long) / high>=SL (short) → fill at SL.
  * Otherwise exit at the candle CLOSE when check_sma_exit fires (mean reverted).
  * Optional max-hold cap (bars) as a time-stop.
  * Round-trip taker fee charged on notional both legs.
  * Liquidation flagged if the adverse excursion exceeds ~1/leverage (isolated).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from ..journal import TradeOutcomeRecord
from ..strategies.mean_reversion import PlaybookMeanReversion
from ..wallet import PositionSide

logger = logging.getLogger("crypto_trader.backtest")


@dataclass
class BacktestResult:
    trades: List[TradeOutcomeRecord] = field(default_factory=list)
    liquidations: int = 0
    fee_drag_pct: float = 0.0          # total fees as % of notional traded, summed
    bars: int = 0
    symbol: str = ""
    timeframe: str = ""
    leverage: int = 1


def run_backtest(
    df: pd.DataFrame,
    *,
    symbol: str = "SYM",
    timeframe: str = "15m",
    sma_period: int = 20,
    entry_band: float = 0.015,
    stop_loss_pct: float = 0.008,
    taker_fee: float = 0.0005,          # fraction per side (CoinDCX ~0.059% → 0.00059)
    leverage: int = 1,
    max_hold_bars: int = 0,            # 0 = no time stop
) -> BacktestResult:
    """Replay mean-reversion over OHLCV ``df`` (ascending). Returns a
    BacktestResult of TradeOutcomeRecords + fee/liquidation stats."""
    pb = PlaybookMeanReversion(
        sma_period=sma_period, entry_band=entry_band,
        stop_loss_pct=stop_loss_pct, timeframe=timeframe,
    )
    n = len(df)
    win = sma_period + 6                       # trailing window the playbook needs
    res = BacktestResult(symbol=symbol, timeframe=timeframe, leverage=leverage, bars=n)
    if n < win + 2:
        return res

    highs = df["high"].astype(float).values
    lows = df["low"].astype(float).values
    closes = df["close"].astype(float).values
    times = (df["close_time"] if "close_time" in df.columns else df["open_time"])

    def _ts_ms(idx: int) -> int:
        try:
            return int(pd.Timestamp(times.iloc[idx]).timestamp() * 1000)
        except Exception:
            return idx

    i = win
    while i < n - 1:
        # Decision on bar i (last CLOSED): window ends with i as iloc[-2].
        window = df.iloc[i - win + 1: i + 2]
        setup = pb.evaluate(window)
        if not setup:
            i += 1
            continue

        side = setup["side"]
        entry_px = float(setup["entry_price"])
        sl_px = float(setup["sl_price"])
        entry_idx = i

        # Walk forward to find the exit.
        exit_idx = None
        exit_px = None
        exit_reason = None
        liquidated = False
        # adverse fraction that liquidates an isolated position (~1/lev).
        liq_frac = 1.0 / max(leverage, 1)

        j = i + 1
        while j < n:
            hi, lo, cl = highs[j], lows[j], closes[j]
            # 1) hard SL intrabar (conservative: assume SL fills at the stop)
            if side == PositionSide.LONG and lo <= sl_px:
                exit_idx, exit_px, exit_reason = j, sl_px, "SL"
                break
            if side == PositionSide.SHORT and hi >= sl_px:
                exit_idx, exit_px, exit_reason = j, sl_px, "SL"
                break
            # 2) liquidation check (adverse excursion beyond 1/lev) — pre-SL only
            #    matters if SL is wider than liq distance; flag it.
            if side == PositionSide.LONG and (entry_px - lo) / entry_px >= liq_frac:
                exit_idx, exit_px, exit_reason = j, entry_px * (1 - liq_frac), "LIQUIDATION"
                liquidated = True
                break
            if side == PositionSide.SHORT and (hi - entry_px) / entry_px >= liq_frac:
                exit_idx, exit_px, exit_reason = j, entry_px * (1 + liq_frac), "LIQUIDATION"
                liquidated = True
                break
            # 3) SMA-touch exit (mean reverted) — evaluated on closed bar j
            exwin = df.iloc[j - win + 1: j + 2]
            if pb.check_sma_exit(exwin, side):
                exit_idx, exit_px, exit_reason = j, cl, "SMA_EXIT"
                break
            # 4) optional time stop
            if max_hold_bars and (j - entry_idx) >= max_hold_bars:
                exit_idx, exit_px, exit_reason = j, cl, "TIME_STOP"
                break
            j += 1

        if exit_idx is None:  # ran out of data still open — drop (no lookahead)
            break

        # PnL on notional (price move), signed by side, net of round-trip taker fee.
        if side == PositionSide.LONG:
            move = (exit_px - entry_px) / entry_px
        else:
            move = (entry_px - exit_px) / entry_px
        fee = taker_fee * 2.0                       # entry + exit, as fraction of notional
        net_ret = move - fee                        # return on notional (1x)
        qty = 1.0                                    # unit notional; metrics are %-based
        realized = net_ret * entry_px * qty          # currency on 1-unit notional
        risk_per_unit = abs(entry_px - sl_px)
        pnl_r = ((exit_px - entry_px) if side == PositionSide.LONG else (entry_px - exit_px)) / risk_per_unit \
            if risk_per_unit > 0 else None

        res.trades.append(TradeOutcomeRecord(
            trade_id=f"{symbol}-{entry_idx}",
            symbol=symbol, side=side.value,
            opened_at=_ts_ms(entry_idx), closed_at=_ts_ms(exit_idx),
            entry_price=entry_px, exit_price=exit_px, quantity=qty,
            realized_pnl=realized, holding_time_s=float((exit_idx - entry_idx)),
            exit_reason=exit_reason, regime="backtest",
            stop_loss_pct=stop_loss_pct, leverage=leverage, pnl_r=pnl_r,
        ))
        res.fee_drag_pct += fee * 100.0
        if liquidated:
            res.liquidations += 1

        # Resume scanning AFTER the exit bar (no overlapping positions).
        i = exit_idx + 1

    return res
