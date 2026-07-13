"""
crypto_trader.research.cli — Research engine CLI

Run:
    python -m crypto_trader.research.cli \\
        --symbols SOLUSDT,ETHUSDT,XRPUSDT \\
        --capital 10000 --leverage 10 --bars 3000

This fetches live Binance data, runs all 7 strategies, simulates ₹10k at 10x,
and prints a ranked leaderboard sorted by composite score.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time


def main():
    parser = argparse.ArgumentParser(
        description="Crypto Futures Research Engine — rank strategies by expectancy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--symbols", default="SOLUSDT,ETHUSDT",
                        help="Comma-separated symbols (default: SOLUSDT,ETHUSDT)")
    parser.add_argument("--capital", type=float, default=10_000.0,
                        help="Initial capital in INR (default: 10000)")
    parser.add_argument("--leverage", type=int, default=10,
                        help="Leverage (default: 10)")
    parser.add_argument("--bars", type=int, default=3000,
                        help="Kline bars per symbol (default: 3000)")
    parser.add_argument("--fee", type=float, default=0.0005,
                        help="Taker fee fraction (default: 0.0005 = 0.05%%)")
    parser.add_argument("--risk-pct", type=float, default=0.01,
                        help="Risk per trade as fraction of capital (default: 0.01)")
    parser.add_argument("--top", type=int, default=25,
                        help="Show top N results (default: 25)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Verbose logging")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname).1s %(message)s",
        stream=sys.stderr,
    )

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("No symbols specified")
        sys.exit(1)

    t0 = time.time()

    from .engine import ResearchEngine

    engine = ResearchEngine(
        symbols=symbols,
        bars=args.bars,
        initial_capital_inr=args.capital,
        leverage=args.leverage,
        risk_per_trade_pct=args.risk_pct,
        taker_fee=args.fee,
    )

    print(f"\n{'='*70}")
    print(f"  Crypto Futures Research Engine")
    print(f"  Capital: ₹{args.capital:,.0f} | Leverage: {args.leverage}x")
    print(f"  Symbols: {', '.join(symbols)} | Bars: {args.bars}")
    print(f"  Taker Fee: {args.fee*100:.3f}% | Risk/Trade: {args.risk_pct*100:.1f}%")
    print(f"{'='*70}")

    results = engine.run()

    elapsed = time.time() - t0
    print(f"\n── Ranked Leaderboard (top {args.top}) ──")
    print(f"  {len(results)} strategy-symbol combos passed | {elapsed:.1f}s")
    engine.print_ranking(results, top_n=args.top)
    engine.print_summary(results)

    if results:
        best = results[0]
        print(f"\n── Best Result ──")
        print(f"  {best.symbol} / {best.strategy}")
        print(f"  {best.trades} trades | Win Rate: {best.win_rate}%")
        print(f"  Profit Factor: {best.profit_factor or '∞'}")
        print(f"  Expectancy (R): {best.expectancy_r:.3f}")
        print(f"  Return: {best.total_return_pct:+.1f}%")
        print(f"  Final Capital: ₹{best.final_capital_inr:,.0f}")
        print(f"  Max Drawdown: {best.max_drawdown_pct:.1f}%")


if __name__ == "__main__":
    main()
