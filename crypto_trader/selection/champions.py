"""
crypto_trader.selection.champions — read the per-symbol champion map.

The bot loads ``DATA_DIR/strategy_champions.json`` at engine startup and routes
each symbol to its champion strategy. A symbol with no entry (or strategy_mode
"none") is NOT traded — no edge was validated for it.

Schema:
{
  "generated_at": 1717100000000,
  "lookback_days": 150,
  "champions": {
    "ETHUSDT": {"strategy_mode": "liquidity_sweep", "params": {"lsw_tp_r": 2.5},
                "oos_pf": 1.55, "oos_expectancy_r": 0.14, "oos_trades": 39,
                "oos_win_rate": 0.41},
    "SOLUSDT": {"strategy_mode": "none", "reason": "no candidate passed gates"}
  }
}
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("crypto_trader.selection.champions")

DATA_DIR = Path.home() / ".crypto_trader"
CHAMPIONS_PATH = DATA_DIR / "strategy_champions.json"


def load_champions(path: Optional[Path] = None) -> Dict:
    p = Path(path) if path is not None else CHAMPIONS_PATH
    try:
        with open(p, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("[champions] bad file %s: %s", p, e)
        return {}
    return data.get("champions", {}) if isinstance(data, dict) else {}


def champion_for(symbol: str, path: Optional[Path] = None) -> Optional[Dict]:
    """Return the champion entry for ``symbol`` (or None if absent / 'none')."""
    champs = load_champions(path)
    entry = champs.get(symbol)
    if not entry:
        return None
    if entry.get("strategy_mode", "none") == "none":
        return None
    return entry
