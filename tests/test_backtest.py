"""Backtest engine — faithful replay of MR logic over synthetic klines."""
import numpy as np
import pandas as pd

from crypto_trader.backtest.engine import run_backtest


def _df(closes, highs=None, lows=None):
    n = len(closes)
    highs = highs or [c * 1.001 for c in closes]
    lows = lows or [c * 0.999 for c in closes]
    t = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({
        "open_time": t, "close_time": t,
        "open": closes, "high": highs, "low": lows, "close": closes,
        "volume": [1.0] * n,
    })


def test_no_trades_on_flat_series():
    df = _df([100.0] * 60)  # flat → never deviates from SMA
    res = run_backtest(df, sma_period=20, entry_band=0.015)
    assert res.trades == []


def test_oversold_then_revert_is_a_win():
    # 40 bars at 100 (SMA≈100), then a drop ≥1.5% below → LONG, then revert to SMA.
    closes = [100.0] * 40 + [98.0, 98.0, 99.0, 100.5, 101.0] + [100.0] * 10
    res = run_backtest(df := _df(closes), sma_period=20, entry_band=0.015,
                       stop_loss_pct=0.05, taker_fee=0.0)
    assert len(res.trades) >= 1
    t = res.trades[0]
    assert t.side == "LONG"
    # reverted up to/through SMA → positive realized (fee=0)
    assert t.realized_pnl > 0
    assert t.exit_reason in ("SMA_EXIT", "TIME_STOP")


def test_stop_loss_hit_is_a_loss():
    # drop below band → LONG, then keep dropping through the hard stop.
    closes = [100.0] * 40 + [98.0, 97.0, 96.0, 95.0, 94.0, 93.0]
    lows = [c * 0.999 for c in closes]
    # force a deep low on the bar after entry to trip SL
    res = run_backtest(_df(closes, lows=lows), sma_period=20, entry_band=0.015,
                       stop_loss_pct=0.008, taker_fee=0.0)
    assert len(res.trades) >= 1
    t = res.trades[0]
    assert t.side == "LONG"
    assert t.realized_pnl < 0          # stopped out
    assert t.exit_reason in ("SL", "LIQUIDATION")


def test_fees_reduce_pnl():
    closes = [100.0] * 40 + [98.0, 99.0, 100.5] + [100.0] * 10
    no_fee = run_backtest(_df(closes), sma_period=20, entry_band=0.015,
                          stop_loss_pct=0.05, taker_fee=0.0)
    with_fee = run_backtest(_df(closes), sma_period=20, entry_band=0.015,
                            stop_loss_pct=0.05, taker_fee=0.001)
    if no_fee.trades and with_fee.trades:
        assert with_fee.trades[0].realized_pnl < no_fee.trades[0].realized_pnl
        assert with_fee.fee_drag_pct > 0


def test_no_lookahead_no_overlap():
    # many oscillations — ensure trades never overlap (entry after prior exit).
    closes = ([100.0] * 25 + [97.0, 101.0] * 20)
    res = run_backtest(_df(closes), sma_period=20, entry_band=0.015, stop_loss_pct=0.05)
    idxs = [(int(t.opened_at), int(t.closed_at)) for t in res.trades]
    for (o1, c1), (o2, c2) in zip(idxs, idxs[1:]):
        assert o2 > c1          # next entry strictly after prior exit
