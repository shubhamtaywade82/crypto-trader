"""
crypto_trader.backtest.st2_sweep — Supertrend2 parameter sweep on SOLUSDT.

Tests the grid of (factor, entry_mode, tp1_pct, sl_mode) and reports
the top configurations by Sharpe, PF, and net return.
"""
from __future__ import annotations

import argparse
import logging
from itertools import product

from ..analytics import metrics as M
from ..data_feed import BinanceDataFeed
from .st2_engine import run_backtest_st2, BacktestResult
from .cli import fetch_klines

logger = logging.getLogger("crypto_trader.backtest.st2_sweep")


def _report(res: BacktestResult) -> dict:
    m = M.compute_metrics(res.trades)
    pnl_pct = sum(float(t.realized_pnl) / float(t.entry_price) * 100 for t in res.trades
                  if t.entry_price) if res.trades else 0.0
    return {
        "trades": m['total_trades'],
        "win_rate": m['win_rate'] * 100,
        "pf": m['profit_factor'],
        "exp_r": m['expectancy_r'],
        "net_pct": pnl_pct,
        "max_dd": m['max_drawdown'],
        "sharpe": M.sharpe(res.trades),
        "liquidations": res.liquidations,
        "fee_drag": res.fee_drag_pct,
    }


def main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING)
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="SOLUSDT")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--htf-timeframe", default="4h")
    p.add_argument("--bars", type=int, default=4000)
    p.add_argument("--leverage", type=int, default=1)
    p.add_argument("--taker-fee", type=float, default=0.00059)
    a = p.parse_args(argv)

    print(f"Fetching {a.bars} {a.timeframe} candles for {a.symbol}...")
    df_pri = fetch_klines(a.symbol, a.timeframe, a.bars)
    print(f"Got {len(df_pri)} primary candles")

    htf_bars = max(int(a.bars * 3_600_000 / 14_400_000), 500)
    print(f"Fetching {htf_bars} {a.htf_timeframe} HTF candles...")
    df_htf = fetch_klines(a.symbol, a.htf_timeframe, htf_bars)
    print(f"Got {len(df_htf)} HTF candles")

    # Parameter grid
    factors = [2.0, 2.5, 3.0, 3.5, 4.0]
    entry_modes = ["flip", "retracement"]
    tp_pcts = [0.0, 0.5, 1.0, 1.5, 2.0]
    sl_modes = ["supertrend", "fixed_pct", "atr"]
    use_htfs = [True, False]

    results = []
    total = len(factors) * len(entry_modes) * len(tp_pcts) * len(sl_modes) * len(use_htfs)
    print(f"\nRunning {total} configurations...\n")

    for i, (fac, emode, tp, slm, htf) in enumerate(product(factors, entry_modes, tp_pcts, sl_modes, use_htfs)):
        if slm == "fixed_pct":
            sl_p = 1.5
            sl_am = 2.5
        elif slm == "atr":
            sl_p = 1.5
            sl_am = 2.5
        else:
            sl_p = 1.5
            sl_am = 2.5

        res = run_backtest_st2(
            df_pri, df_htf,
            symbol=a.symbol, timeframe=a.timeframe, htf_timeframe=a.htf_timeframe,
            factor=fac, entry_mode=emode, tp1_pct=tp, use_tp=(tp > 0),
            sl_mode=slm, sl_pct=sl_p, sl_atr_mult=sl_am,
            use_htf_filter=htf,
            taker_fee=a.taker_fee, leverage=a.leverage,
        )
        r = _report(res)
        r["cfg"] = f"f={fac} mode={emode} tp={tp} sl={slm} htf={htf}"
        results.append(r)

        if (i + 1) % 10 == 0 or i == total - 1:
            print(f"  {i+1}/{total} done")

    # Sort and display top 10 by Sharpe
    print("\n" + "="*80)
    print("TOP 10 BY SHARPE RATIO")
    print("="*80)
    print(f"{'Config':<45} | {'Trades':>6} | {'Win%':>5} | {'PF':>5} | {'Sharpe':>6} | {'Net%':>7}")
    print("-"*80)
    for r in sorted(results, key=lambda x: x["sharpe"], reverse=True)[:10]:
        print(f"{r['cfg']:<45} | {r['trades']:>6} | {r['win_rate']:>5.1f} | {r['pf']:>5.2f} | {r['sharpe']:>6.2f} | {r['net_pct']:>+7.2f}")

    # Top 10 by Profit Factor
    print("\n" + "="*80)
    print("TOP 10 BY PROFIT FACTOR")
    print("="*80)
    print(f"{'Config':<45} | {'Trades':>6} | {'Win%':>5} | {'PF':>5} | {'Sharpe':>6} | {'Net%':>7}")
    print("-"*80)
    for r in sorted(results, key=lambda x: x["pf"] if x["pf"] is not None else -1, reverse=True)[:10]:
        print(f"{r['cfg']:<45} | {r['trades']:>6} | {r['win_rate']:>5.1f} | {r['pf']:>5.2f} | {r['sharpe']:>6.2f} | {r['net_pct']:>+7.2f}")

    # Top 10 by Net Return
    print("\n" + "="*80)
    print("TOP 10 BY NET RETURN")
    print("="*80)
    print(f"{'Config':<45} | {'Trades':>6} | {'Win%':>5} | {'PF':>5} | {'Sharpe':>6} | {'Net%':>7}")
    print("-"*80)
    for r in sorted(results, key=lambda x: x["net_pct"], reverse=True)[:10]:
        print(f"{r['cfg']:<45} | {r['trades']:>6} | {r['win_rate']:>5.1f} | {r['pf']:>5.2f} | {r['sharpe']:>6.2f} | {r['net_pct']:>+7.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
