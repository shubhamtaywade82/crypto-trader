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
# Read-only switch: PLACE_ORDER=false makes live mode observe-only — all outbound
# order mutations (place / cancel / exit) are blocked while reads keep working.
# Unset defaults to true so existing behavior is unchanged.
PLACE_ORDER_ENV = "PLACE_ORDER"


class LiveTradingBlocked(RuntimeError):
    """Raised when a live order op is attempted but gate is closed."""


def _env_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_true_default(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw == "":
        return default
    return raw in {"1", "true", "yes", "on"}


def order_execution_enabled() -> bool:
    """False when PLACE_ORDER is explicitly disabled (read-only live mode)."""
    return _env_true_default(PLACE_ORDER_ENV, True)


def _has_halt_file() -> bool:
    return HALT_FILE.exists()


def _is_live_env_enabled() -> bool:
    return _env_true(LIVE_ENV_VAR)


def _is_ack_phrase_correct() -> bool:
    return os.getenv(ACK_ENV_VAR, "") == ACK_PHRASE


def is_live_enabled() -> bool:
    """Returns True only if every gate is open."""
    if _has_halt_file():
        return False
    if not _is_live_env_enabled():
        return False
    if not _is_ack_phrase_correct():
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
    if not order_execution_enabled():
        reasons.append(f"env {PLACE_ORDER_ENV}=false (read-only live mode — no order execution)")
    if not constructor_ack:
        reasons.append(f"constructor flag i_understand_real_money not set on {venue}ExecutionEngine")
    if not _is_live_env_enabled():
        reasons.append(f"env {LIVE_ENV_VAR} != true")
    if not _is_ack_phrase_correct():
        reasons.append(f"env {ACK_ENV_VAR} != '{ACK_PHRASE}'")
    if _has_halt_file():
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
