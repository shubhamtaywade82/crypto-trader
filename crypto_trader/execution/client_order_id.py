"""
crypto_trader.execution.client_order_id — Idempotent client order IDs
=======================================================================
Deterministic, collision-resistant client order IDs let us safely retry a
submission without double-placing: the same logical intent always produces the
same ID, so the venue (and our duplicate guard) can dedupe.

Format: ``ct-<symbol>-<intent>-<short-hash>`` (<= 36 chars, alphanumeric+dash).
"""
from __future__ import annotations

import hashlib
import time


_MAX_LEN = 36


def make_client_order_id(symbol: str, intent: str, *, nonce: str | None = None) -> str:
    """Build a deterministic client order id.

    ``intent`` is a stable description of the logical action, e.g.
    "entry-long", "exit-tp1", "exit-sl". Pass an explicit ``nonce`` (e.g. the
    signal timestamp) to make retries of the *same* intent idempotent while
    distinct intents stay unique. If omitted, a coarse time bucket is used.
    """
    nonce = nonce if nonce is not None else str(int(time.time()))
    raw = f"{symbol}|{intent}|{nonce}".upper()
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    sym = "".join(c for c in symbol.upper() if c.isalnum())[:10]
    intent_slug = "".join(c for c in intent.lower() if c.isalnum() or c == "-")[:8]
    coid = f"ct-{sym}-{intent_slug}-{digest}"
    return coid[:_MAX_LEN]
