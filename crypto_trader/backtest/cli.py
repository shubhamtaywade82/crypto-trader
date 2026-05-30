"""
crypto_trader.backtest.cli — run the mean-reversion backtest from the shell.

    python -m crypto_trader.backtest.cli --symbol SOLUSDT --timeframe 15m --bars 5000
    python -m crypto_trader.backtest.cli --symbol SOLUSDT --bars 8000 --leverage 10 --split 0.7

Fetches klines from Binance USDⓈ-M (free, no creds), replays the live strategy,
prints expectancy / win-rate / profit-factor / Sharpe / max-DD after fees. Use
--split to hold out an out-of-sample test fold (train stats vs test stats).
"""
from __future__ import annotations

import argparse
import logging
import time

import pandas as pd

from ..analytics import metrics as M
from ..data_feed import BinanceDataFeed
from .engine import run_backtest, BacktestResult

logger = logging.getLogger("crypto_trader.backtest.cli")

_TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000,
}


def fetch_klines(symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
    """Page back from now to assemble ``bars`` candles (Binance caps 1500/call)."""
    feed = BinanceDataFeed()
    step = _TF_MS.get(timeframe, 900_000)
    end = int(time.time() * 1000)
    frames = []
    remaining = bars
    while remaining > 0:
        lim = min(1500, remaining)
        start = end - lim * step
        df = feed.get_klines(symbol, timeframe, limit=lim, start_time=start, end_time=end)
        if df is None or len(df) == 0:
            break
        frames.append(df)
        end = int(df["open_time"].iloc[0].timestamp() * 1000) - 1
        remaining -= len(df)
        if len(df) < lim:
            break
    if not frames:
        raise SystemExit("No kline data fetched")
    out = pd.concat(frames[::-1], ignore_index=True).drop_duplicates(subset="open_time")
    return out.sort_values("open_time").reset_index(drop=True)


def _report(label: str, res: BacktestResult) -> dict:
    m = M.compute_metrics(res.trades)
    pnl_pct = sum(float(t.realized_pnl) / float(t.entry_price) * 100 for t in res.trades
                  if t.entry_price) if res.trades else 0.0
    print(f"\n=== {label} ===")
    print(f"  bars={res.bars} trades={m['total_trades']} leverage={res.leverage}x liquidations={res.liquidations}")
    print(f"  win_rate        : {m['win_rate']*100:.1f}%")
    print(f"  profit_factor   : {m['profit_factor']}")
    print(f"  expectancy_r    : {m['expectancy_r']:+.3f} R / trade")
    print(f"  avg_win / loss R: {m['avg_win_r']:+.2f} / {m['avg_loss_r']:+.2f}")
    print(f"  net return (1x) : {pnl_pct:+.2f}% on notional (sum of per-trade %)")
    print(f"  max_drawdown    : {m['max_drawdown']:.4f} (currency, 1-unit notional)")
    print(f"  fee drag        : {res.fee_drag_pct:.2f}% (round-trip taker, summed)")
    print(f"  avg hold        : {m['avg_holding_time_s']:.1f} bars")
    sharpe = M.sharpe(res.trades)
    print(f"  sharpe (per-trade, annualized@365): {sharpe}")
    verdict = "POSITIVE expectancy" if m["expectancy_r"] > 0 and pnl_pct > 0 else "NO EDGE (≤0 after fees)"
    print(f"  VERDICT: {verdict}")
    return {"label": label, "pnl_pct": pnl_pct, **m}


def main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING)
    p = argparse.ArgumentParser(prog="crypto_trader.backtest.cli")
    p.add_argument("--symbol", default="SOLUSDT")
    p.add_argument("--timeframe", default="15m")
    p.add_argument("--bars", type=int, default=5000)
    p.add_argument("--sma-period", type=int, default=20)
    p.add_argument("--entry-band", type=float, default=0.015)
    p.add_argument("--stop-loss-pct", type=float, default=0.008)
    p.add_argument("--taker-fee", type=float, default=0.00059)   # CoinDCX taker ~0.059%
    p.add_argument("--leverage", type=int, default=1)
    p.add_argument("--max-hold-bars", type=int, default=0)
    p.add_argument("--split", type=float, default=0.0,
                   help="train fraction for an out-of-sample test fold (e.g. 0.7)")
    a = p.parse_args(argv)

    print(f"Fetching {a.bars} {a.timeframe} candles for {a.symbol} from Binance…")
    df = fetch_klines(a.symbol, a.timeframe, a.bars)
    print(f"Got {len(df)} candles: {df['open_time'].iloc[0]} → {df['open_time'].iloc[-1]}")

    kw = dict(symbol=a.symbol, timeframe=a.timeframe, sma_period=a.sma_period,
              entry_band=a.entry_band, stop_loss_pct=a.stop_loss_pct,
              taker_fee=a.taker_fee, leverage=a.leverage, max_hold_bars=a.max_hold_bars)

    if a.split and 0 < a.split < 1:
        cut = int(len(df) * a.split)
        _report("TRAIN (in-sample)", run_backtest(df.iloc[:cut].reset_index(drop=True), **kw))
        _report("TEST (out-of-sample)", run_backtest(df.iloc[cut:].reset_index(drop=True), **kw))
    else:
        _report("FULL", run_backtest(df, **kw))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
