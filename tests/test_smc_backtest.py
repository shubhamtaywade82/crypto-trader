"""SMC backtest replay engine — runs the live PlaybookSMC over synthetic OHLCV."""
import numpy as np
import pandas as pd

from crypto_trader.backtest.smc_engine import run_backtest_smc


def _synthetic(n=800, seed=0):
    rng = np.random.default_rng(seed)
    base, p, closes = 100.0, 100.0, []
    for _ in range(n):
        p = max(1.0, p + 0.03 + rng.normal(0, 0.6))
        closes.append(p)
    closes = np.array(closes)
    op = np.concatenate([[base], closes[:-1]])
    hi = np.maximum(op, closes) + rng.uniform(0.1, 0.6, n)
    lo = np.minimum(op, closes) - rng.uniform(0.1, 0.6, n)
    vol = rng.uniform(800, 5000, n)
    t0 = 1_600_000_000_000
    ot = pd.to_datetime(t0 + np.arange(n) * 900_000, unit="ms", utc=True)
    ct = pd.to_datetime(t0 + (np.arange(n) + 1) * 900_000 - 1, unit="ms", utc=True)
    df = pd.DataFrame(dict(open_time=ot, open=op, high=hi, low=lo, close=closes,
                           volume=vol, quote_volume=vol, close_time=ct))
    htf = (df.resample("4h", on="open_time")
             .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                  close=("close", "last"), volume=("volume", "sum"),
                  quote_volume=("quote_volume", "sum"))
             .dropna().reset_index())
    htf["close_time"] = htf["open_time"] + pd.Timedelta("4h") - pd.Timedelta("1ms")
    return df, htf


def test_backtest_runs_and_reports():
    df, htf = _synthetic()
    res = run_backtest_smc(df, htf, symbol="SYN", timeframe="15m",
                           htf_timeframe="4h", min_score=55.0, leverage=1)
    assert res.bars == len(df)
    assert res.evaluated > 0
    # selective by design: signals are a small fraction of evaluated windows
    assert res.signals <= res.evaluated
    assert len(res.trades) == res.signals or len(res.trades) == res.signals - 1
    for t in res.trades:
        assert t.side in ("LONG", "SHORT")
        assert t.entry_price > 0 and t.exit_price > 0
        assert t.exit_reason in ("TP", "SL", "LIQUIDATION", "TIME_STOP")


def test_backtest_short_or_missing_htf_returns_empty():
    df, _ = _synthetic(n=200)
    res = run_backtest_smc(df, None, symbol="SYN")  # no HTF
    assert res.trades == [] and res.signals == 0


def test_leverage_increases_liquidation_pressure():
    df, htf = _synthetic(seed=3)
    lo = run_backtest_smc(df, htf, min_score=55.0, leverage=1)
    hi = run_backtest_smc(df, htf, min_score=55.0, leverage=25)
    # same entries; high leverage can only add (never remove) liquidations
    assert hi.liquidations >= lo.liquidations
