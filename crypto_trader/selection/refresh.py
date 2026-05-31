"""
crypto_trader.selection.refresh — scheduled champion refresh (cron / /schedule).

Runs the offline selector for the configured symbols and (over)writes
~/.crypto_trader/strategy_champions.json. The live bot's flat-only
``_refresh_champion`` then hot-swaps each FLAT symbol to its new champion on the
next signal tick — open positions are never disturbed.

Config via env (so a cron line needs no args):
    SELECTION_SYMBOLS=ETHUSDT,BTCUSDT,SOLUSDT,XRPUSDT   (default)
    SELECTION_DAYS=150

Schedule weekly, e.g. crontab:
    0 3 * * 1  cd /path/to/crypto-trader && /usr/bin/python3 -m crypto_trader.selection.refresh >> ~/.crypto_trader/refresh.log 2>&1

or via Claude Code:  /schedule  a weekly run of this module.
"""
from __future__ import annotations

import logging
import os
import time

from . import selector


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    log = logging.getLogger("crypto_trader.selection.refresh")
    syms = [s.strip().upper() for s in
            os.environ.get("SELECTION_SYMBOLS", "ETHUSDT,BTCUSDT,SOLUSDT,XRPUSDT").split(",")
            if s.strip()]
    days = int(os.environ.get("SELECTION_DAYS", "150"))
    log.info("champion refresh starting: symbols=%s days=%d", syms, days)
    payload = selector.run(syms, days=days, ts_ms=int(time.time() * 1000))
    path = selector.write_champions(payload)
    chosen = {s: c.get("strategy_mode") for s, c in payload["champions"].items()}
    log.info("champions refreshed → %s | %s", path, chosen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
