"""
crypto_trader.backtest.regime_switch_cli — run the regime-switcher backtest.

    python -m crypto_trader.backtest.regime_switch_cli --symbol SOLUSDT --timeframe 1h --bars 4000

Fetches klines, replays the regime-gated Supertrend2, reports metrics.
"""
from __future__ import annotations

import argparse
import logging

from ..analytics import metrics as M
from .cli import fetch_klines
from .regime_switch_engine import run_backtest_regime_switch, BacktestResult

logger = logging.getLogger("crypto_trader.backtest.regime_switch_cli")


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
    print(f"  net return (1x) : {pnl_pct:+.2f}% on notional")
    print(f"  max_drawdown    : {m['max_drawdown']:.4f}")
    print(f"  fee drag        : {res.fee_drag_pct:.2f}%")
    print(f"  avg hold        : {m['avg_holding_time_s']:.1f} bars")
    sharpe = M.sharpe(res.trades)
    print(f"  sharpe          : {sharpe}")
    verdict = "POSITIVE expectancy" if m["expectancy_r"] > 0 and pnl_pct > 0 else "NO EDGE (≤0 after fees)"
    print(f"  VERDICT: {verdict}")
    return {"label": label, "pnl_pct": pnl_pct, **m}


def main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING)
    p = argparse.ArgumentParser(prog="crypto_trader.backtest.regime_switch_cli")
    p.add_argument("--symbol", default="SOLUSDT")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--htf-timeframe", default="4h")
    p.add_argument("--bars", type=int, default=4000)
    p.add_argument("--factor", type=float, default=3.0)
    p.add_argument("--entry-mode", default="flip", choices=["flip", "retracement"])
    p.add_argument("--tp1-pct", type=float, default=1.0)
    p.add_argument("--use-tp", type=bool, default=True)
    p.add_argument("--sl-mode", default="supertrend", choices=["supertrend", "fixed_pct", "atr"])
    p.add_argument("--allowed-regimes", default="TREND_EXPANSION,BREAKOUT_ENVIRONMENT")
    p.add_argument("--taker-fee", type=float, default=0.00059)
    p.add_argument("--leverage", type=int, default=1)
    p.add_argument("--max-hold-bars", type=int, default=0)
    p.add_argument("--split", type=float, default=0.0)
    a = p.parse_args(argv)

    print(f"Fetching {a.bars} {a.timeframe} candles for {a.symbol}...")
    df_pri = fetch_klines(a.symbol, a.timeframe, a.bars)
    print(f"Got {len(df_pri)} primary candles")

    htf_bars = max(int(a.bars * 3_600_000 / 14_400_000), 500)
    print(f"Fetching {htf_bars} {a.htf_timeframe} HTF candles...")
    df_htf = fetch_klines(a.symbol, a.htf_timeframe, htf_bars)
    print(f"Got {len(df_htf)} HTF candles")

    regimes = [r.strip() for r in a.allowed_regimes.split(",")]
    kw = dict(
        symbol=a.symbol, timeframe=a.timeframe, htf_timeframe=a.htf_timeframe,
        factor=a.factor, entry_mode=a.entry_mode,
        tp1_pct=a.tp1_pct, use_tp=a.use_tp, sl_mode=a.sl_mode,
        allowed_regimes=regimes,
        taker_fee=a.taker_fee, leverage=a.leverage, max_hold_bars=a.max_hold_bars,
    )

    if a.split and 0 < a.split < 1:
        cut = int(len(df_pri) * a.split)
        _report("TRAIN", run_backtest_regime_switch(df_pri.iloc[:cut].reset_index(drop=True), df_htf, **kw))
        _report("TEST", run_backtest_regime_switch(df_pri.iloc[cut:].reset_index(drop=True), df_htf, **kw))
    else:
        _report("FULL", run_backtest_regime_switch(df_pri, df_htf, **kw))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
