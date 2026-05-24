"""
crypto_trader.safe_mode — Live trading kill-switch.

Triple-gate any real-money order op (place / modify / cancel).
Default = blocked. Engines must opt in explicitly AND env must allow AND
no HALT file present.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("crypto_trader.safe_mode")

DATA_DIR = Path.home() / ".crypto_trader"
HALT_FILE = DATA_DIR / "HALT"
LIVE_ENV_VAR = "LIVE_TRADING_ENABLED"
ACK_ENV_VAR = "LIVE_TRADING_ACK"
ACK_PHRASE = "I_UNDERSTAND_REAL_MONEY_WILL_BE_LOST"


class LiveTradingBlocked(RuntimeError):
    """Raised when a live order op is attempted but gate is closed."""


def _env_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def is_live_enabled() -> bool:
    """Returns True only if every gate is open."""
    if HALT_FILE.exists():
        return False
    if not _env_true(LIVE_ENV_VAR):
        return False
    if os.getenv(ACK_ENV_VAR, "") != ACK_PHRASE:
        return False
    return True


def assert_live_allowed(
    op: str,
    *,
    venue: str,
    constructor_ack: bool,
    symbol: Optional[str] = None,
) -> None:
    """Call at start of every live place/modify/cancel.

    Raises LiveTradingBlocked unless ALL of:
      1. constructor_ack=True  (engine was built with i_understand_real_money=True)
      2. env LIVE_TRADING_ENABLED=true
      3. env LIVE_TRADING_ACK="I_UNDERSTAND_REAL_MONEY_WILL_BE_LOST"
      4. ~/.crypto_trader/HALT file absent
    """
    reasons = []
    if not constructor_ack:
        reasons.append(f"constructor flag i_understand_real_money not set on {venue}ExecutionEngine")
    if not _env_true(LIVE_ENV_VAR):
        reasons.append(f"env {LIVE_ENV_VAR} != true")
    if os.getenv(ACK_ENV_VAR, "") != ACK_PHRASE:
        reasons.append(f"env {ACK_ENV_VAR} != '{ACK_PHRASE}'")
    if HALT_FILE.exists():
        reasons.append(f"HALT file present at {HALT_FILE}")

    if reasons:
        msg = (
            f"BLOCKED live {op} on {venue} (symbol={symbol}): "
            + "; ".join(reasons)
        )
        logger.critical(msg)
        raise LiveTradingBlocked(msg)

    logger.warning(
        "LIVE %s on %s symbol=%s — gate OPEN, sending to exchange", op, venue, symbol
    )


def trip_halt(reason: str = "manual") -> None:
    """Create HALT file. Blocks all future live ops until removed."""
    DATA_DIR.mkdir(exist_ok=True)
    HALT_FILE.write_text(f"halted: {reason}\n", encoding="utf-8")
    logger.critical("HALT tripped: %s (file: %s)", reason, HALT_FILE)


def clear_halt() -> bool:
    """Remove HALT file. Returns True if file existed."""
    if HALT_FILE.exists():
        HALT_FILE.unlink()
        logger.warning("HALT cleared (%s removed)", HALT_FILE)
        return True
    return False
