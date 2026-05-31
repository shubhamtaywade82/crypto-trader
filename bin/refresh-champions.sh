#!/usr/bin/env bash
# refresh-champions.sh — run the offline champion selector and refresh
# ~/.crypto_trader/strategy_champions.json. The live bot's flat-only
# _refresh_champion picks it up on the next signal tick (open positions untouched).
#
# Schedule weekly (Mon 03:00 local) via crontab:
#   0 3 * * 1 /home/nemesis/project/trading-workspace/coindcx/crypto-trader/bin/refresh-champions.sh
#
# Override symbols/days/python from the environment or a .env if you like.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# Config (env overrides win; sensible defaults otherwise).
export SELECTION_SYMBOLS="${SELECTION_SYMBOLS:-ETHUSDT,BTCUSDT,SOLUSDT,XRPUSDT}"
export SELECTION_DAYS="${SELECTION_DAYS:-150}"
PY="${CHAMPIONS_PYTHON:-/usr/bin/python3}"

LOG_DIR="${HOME}/.crypto_trader"
mkdir -p "$LOG_DIR"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] champion refresh: symbols=$SELECTION_SYMBOLS days=$SELECTION_DAYS"
exec "$PY" -m crypto_trader.selection.refresh
