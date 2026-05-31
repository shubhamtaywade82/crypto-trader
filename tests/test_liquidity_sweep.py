"""PlaybookLiquiditySweep — sweep-reversal entry, 2.5R target, symbol scoping."""
import pandas as pd

from crypto_trader.strategies.liquidity_sweep import PlaybookLiquiditySweep
from crypto_trader.config import TradingConfig, _parse_symbol_float_map
from crypto_trader.wallet import PositionSide


def _cd(o, c, hi=None, lo=None, v=1000):
    return dict(open=o, high=hi if hi is not None else max(o, c) + 0.2,
                low=lo if lo is not None else min(o, c) - 0.2,
                close=c, volume=v, quote_volume=v)


def _bullish_sweep_df():
    """Confirmed swing low ~96.8, then a final bar that wicks below it and closes
    back above = a fresh bullish liquidity sweep."""
    lead = list(range(84, 100))
    mid = [100, 99, 98, 97, 98, 99, 100, 102, 104, 103, 102, 101, 100, 99, 98, 99, 100, 101]
    rows, prev = [], lead[0]
    for c in lead[1:] + mid:
        rows.append(_cd(prev, c)); prev = c
    rows.append(_cd(101, 100.5, lo=95.0))   # sweep wick below 96.8, close back above
    df = pd.DataFrame(rows); df["open_time"] = range(len(df))
    return df


def test_bullish_sweep_long_with_2p5R_target():
    pb = PlaybookLiquiditySweep(tp_r=2.5)
    s = pb.evaluate(_bullish_sweep_df())
    assert s is not None
    assert s["side"] == PositionSide.LONG
    e, sl, tp = s["entry_price"], s["sl_price"], s["tp_price"]
    assert sl < e < tp
    rr = (tp - e) / (e - sl)
    assert abs(rr - 2.5) < 1e-6                     # TP is exactly 2.5R
    assert s["strategy"] == "liquidity_sweep"


def test_tp_r_is_configurable():
    df = _bullish_sweep_df()
    s = PlaybookLiquiditySweep(tp_r=3.0).evaluate(df)
    rr = (s["tp_price"] - s["entry_price"]) / (s["entry_price"] - s["sl_price"])
    assert abs(rr - 3.0) < 1e-6


def test_no_sweep_returns_none():
    # Flat, no swing breaches → no sweep → no signal.
    rows = [_cd(100, 100) for _ in range(40)]
    df = pd.DataFrame(rows); df["open_time"] = range(len(df))
    assert PlaybookLiquiditySweep().evaluate(df) is None


def test_insufficient_bars_returns_none():
    rows = [_cd(100, 101) for _ in range(10)]
    df = pd.DataFrame(rows); df["open_time"] = range(len(df))
    assert PlaybookLiquiditySweep().evaluate(df) is None


def test_per_symbol_tp_override_parsing():
    m = _parse_symbol_float_map("ETHUSDT:2.5,BTCUSDT:3.0, junk, BAD:x")
    assert m == {"ETHUSDT": 2.5, "BTCUSDT": 3.0}


def test_config_strategy_symbols_and_overrides_from_env(monkeypatch):
    monkeypatch.setenv("STRATEGY_MODE", "liquidity_sweep")
    monkeypatch.setenv("STRATEGY_SYMBOLS", "ETHUSDT,BTCUSDT")
    monkeypatch.setenv("LSW_TP_R_OVERRIDES", "ETHUSDT:2.5,BTCUSDT:3.0")
    cfg = TradingConfig.from_env()
    assert cfg.strategy_symbols == ["ETHUSDT", "BTCUSDT"]
    assert cfg.lsw_tp_r_overrides == {"ETHUSDT": 2.5, "BTCUSDT": 3.0}
