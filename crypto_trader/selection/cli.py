"""
crypto_trader.selection.cli — run the offline champion selector.

    python -m crypto_trader.selection.cli --symbols ETHUSDT,BTCUSDT,SOLUSDT,XRPUSDT --days 150

Backtests every strategy per symbol on an OOS slice, prints the full candidate
table, and writes ~/.crypto_trader/strategy_champions.json. The live bot picks
up that file when USE_STRATEGY_CHAMPIONS=true. Re-run manually or on a schedule
(cron) — selection never happens inside the live trading loop.
"""
from __future__ import annotations

import argparse
import logging

from . import selector


def main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING)
    p = argparse.ArgumentParser(prog="crypto_trader.selection.cli")
    p.add_argument("--symbols", default="ETHUSDT,BTCUSDT,SOLUSDT,XRPUSDT")
    p.add_argument("--days", type=int, default=150)
    p.add_argument("--test-frac", type=float, default=0.35)
    p.add_argument("--taker-fee", type=float, default=0.00059)
    p.add_argument("--leverage", type=int, default=1)
    p.add_argument("--ts-ms", type=int, default=0, help="generated_at stamp (caller supplies)")
    p.add_argument("--dry-run", action="store_true", help="print only, do not write the file")
    a = p.parse_args(argv)
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]

    print(f"Champion selection | {a.days}d | OOS test_frac={a.test_frac} | gates={selector.GATES}")
    payload = selector.run(syms, days=a.days, test_frac=a.test_frac,
                           taker_fee=a.taker_fee, leverage=a.leverage, ts_ms=a.ts_ms)

    for sym in syms:
        print(f"\n### {sym}")
        print(f"{'strategy':<18}{'params':<18}{'OOS_tr':>7}{'win%':>7}{'PF':>7}{'expR':>8}")
        print("-" * 70)
        for mode, params, m in payload["_detail"].get(sym, []):
            ps = ",".join(f"{k}={v}" for k, v in params.items()) or "-"
            print(f"{mode:<18}{ps:<18}{m['oos_trades']:>7}{m['oos_win_rate']*100:>6.1f}%"
                  f"{m['oos_pf']:>7.2f}{m['oos_expectancy_r']:>8.3f}")
        champ = payload["champions"][sym]
        if champ["strategy_mode"] == "none":
            print(f"  → CHAMPION: none ({champ.get('reason')})")
        else:
            print(f"  → CHAMPION: {champ['strategy_mode']} {champ.get('params', {})} "
                  f"(PF {champ['oos_pf']}, expR {champ['oos_expectancy_r']}, {champ['oos_trades']} tr)")

    if a.dry_run:
        print("\n[dry-run] not written")
        return 0
    path = selector.write_champions(payload)
    print(f"\nWrote {path}")
    print("Enable in the bot with USE_STRATEGY_CHAMPIONS=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
