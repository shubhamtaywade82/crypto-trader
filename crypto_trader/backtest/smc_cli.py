"""
crypto_trader.backtest.smc_cli — run the SMC pipeline backtest from the shell.

    python -m crypto_trader.backtest.smc_cli --symbol SOLUSDT --timeframe 15m --bars 6000
    python -m crypto_trader.backtest.smc_cli --symbol BTCUSDT --timeframe 15m --bars 8000 --leverage 5 --split 0.7
    python -m crypto_trader.backtest.smc_cli --symbol ETHUSDT --min-score 60 --tp-rr 2.5

Fetches klines from Binance USDⓈ-M (free, no creds), replays the LIVE SMC
pipeline (PlaybookSMC), prints expectancy / win-rate / profit-factor / Sharpe /
max-DD after fees. HTF bias frame is aligned by timestamp (no lookahead).
"""
from __future__ import annotations

import argparse
import logging
import time

import pandas as pd

from ..analytics import metrics as M
from ..data_feed import BinanceDataFeed
from .smc_engine import run_backtest_smc, BacktestResult

logger = logging.getLogger("crypto_trader.backtest.smc_cli")

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
    print(f"  bars={res.bars} evaluated={res.evaluated} signals={res.signals} "
          f"trades={m['total_trades']} leverage={res.leverage}x liquidations={res.liquidations}")
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
    p = argparse.ArgumentParser(prog="crypto_trader.backtest.smc_cli")
    p.add_argument("--symbol", default="SOLUSDT")
    p.add_argument("--timeframe", default="15m")
    p.add_argument("--htf-timeframe", default="4h")
    p.add_argument("--bars", type=int, default=6000)
    p.add_argument("--swing-window", type=int, default=3)
    p.add_argument("--min-score", type=float, default=70.0)
    p.add_argument("--sweep-lookback", type=int, default=12)
    p.add_argument("--structure-lookback", type=int, default=12)
    p.add_argument("--volume-mult-target", type=float, default=3.0)
    p.add_argument("--rvol-target", type=float, default=1.2)
    p.add_argument("--atr-period", type=int, default=14)
    p.add_argument("--sl-atr-mult", type=float, default=1.5)
    p.add_argument("--tp-rr", type=float, default=2.0)
    p.add_argument("--max-hold-hours", type=int, default=24)
    p.add_argument("--taker-fee", type=float, default=0.00059)
    p.add_argument("--leverage", type=int, default=1)
    p.add_argument("--max-hold-bars", type=int, default=0)
    p.add_argument("--split", type=float, default=0.0,
                   help="train fraction for an out-of-sample test fold (e.g. 0.7)")
    a = p.parse_args(argv)

    print(f"Fetching {a.bars} {a.timeframe} candles for {a.symbol} from Binance…")
    df_pri = fetch_klines(a.symbol, a.timeframe, a.bars)
    print(f"Got {len(df_pri)} primary candles: {df_pri['open_time'].iloc[0]} → {df_pri['open_time'].iloc[-1]}")

    htf_bars = max(int(a.bars * _TF_MS.get(a.timeframe, 900_000) / _TF_MS.get(a.htf_timeframe, 14_400_000)), 500)
    print(f"Fetching {htf_bars} {a.htf_timeframe} HTF candles…")
    df_htf = fetch_klines(a.symbol, a.htf_timeframe, htf_bars)
    print(f"Got {len(df_htf)} HTF candles")

    kw = dict(
        symbol=a.symbol, timeframe=a.timeframe, htf_timeframe=a.htf_timeframe,
        swing_window=a.swing_window, min_score=a.min_score,
        sweep_lookback=a.sweep_lookback, structure_lookback=a.structure_lookback,
        volume_mult_target=a.volume_mult_target, rvol_target=a.rvol_target,
        atr_period=a.atr_period, sl_atr_mult=a.sl_atr_mult, tp_rr=a.tp_rr,
        max_hold_hours=a.max_hold_hours, taker_fee=a.taker_fee,
        leverage=a.leverage, max_hold_bars=a.max_hold_bars,
    )

    if a.split and 0 < a.split < 1:
        cut = int(len(df_pri) * a.split)
        # HTF split mirrors the primary cut by timestamp so each fold gets only
        # its own HTF history.
        cut_ts = df_pri["open_time"].iloc[cut]
        htf_train = df_htf[df_htf["open_time"] < cut_ts].reset_index(drop=True)
        _report("TRAIN (in-sample)",
                run_backtest_smc(df_pri.iloc[:cut].reset_index(drop=True), htf_train, **kw))
        _report("TEST (out-of-sample)",
                run_backtest_smc(df_pri.iloc[cut:].reset_index(drop=True), df_htf, **kw))
    else:
        _report("FULL", run_backtest_smc(df_pri, df_htf, **kw))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
