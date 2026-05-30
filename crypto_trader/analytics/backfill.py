"""
crypto_trader.analytics.backfill — reconstruct closed-trade outcomes from history.

The TradeOutcomeJournal only began capturing records once _on_trade_closed was
wired (Phase 1). For trades that closed *before* that wiring existed, the
authoritative history lives in the wallet event log (POSITION_CLOSED events,
which are self-sufficient: they carry entry+exit price, qty, pnl, mfe/mae,
regime, timestamps). This module replays that log to populate the outcome
journal so metrics/calibration see the full trading history.

Read-only against the wallet store; idempotency is the caller's concern (run
once into an empty/temp outcome journal, or dedupe by trade_id afterwards).
"""
from __future__ import annotations

import json
import logging
from typing import Iterable, Optional, Tuple

from ..journal import TradeOutcomeJournal, TradeOutcomeRecord

logger = logging.getLogger("crypto_trader.analytics.backfill")

# event rows are (ts, event_type, symbol, namespace, payload_json)
_CLOSE_TYPES = {"POSITION_CLOSED", "LIQUIDATION"}


def _payload(row: Tuple) -> dict:
    raw = row[4]
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _record_from_close(symbol: str, p: dict, seq: int) -> TradeOutcomeRecord:
    entry_ts = p.get("entry_ts") or p.get("timestamp") or 0
    exit_ts = p.get("exit_ts") or p.get("timestamp") or 0
    holding_s = max(0.0, (float(exit_ts) - float(entry_ts)) / 1000.0)
    entry = float(p.get("entry_price", 0.0) or 0.0)
    exit_px = float(p.get("exit_price", 0.0) or 0.0)
    pnl = float(p.get("remaining_pnl", p.get("pnl", 0.0)) or 0.0)
    tid = p.get("trade_id") or f"{symbol}-{int(exit_ts) or seq}"
    return TradeOutcomeRecord(
        trade_id=tid,
        symbol=symbol,
        side=p.get("side", ""),
        opened_at=int(entry_ts or exit_ts),
        closed_at=int(exit_ts),
        entry_price=entry,
        exit_price=exit_px,
        quantity=float(p.get("quantity", 0.0) or 0.0),
        realized_pnl=pnl,
        holding_time_s=holding_s,
        exit_reason=p.get("exit_reason") or p.get("reason", ""),
        regime=p.get("regime"),
        mfe=p.get("mfe"),
        mae=p.get("mae"),
    )


def rebuild_outcomes_from_events(
    event_rows: Iterable[Tuple],
    outcome_journal: Optional[TradeOutcomeJournal] = None,
) -> int:
    """Replay wallet event rows, emitting one TradeOutcomeRecord per close.

    event_rows: iterable of (ts, event_type, symbol, namespace, payload_json),
        e.g. ``WalletStore(db_url).iter_events()``.
    outcome_journal: sink (defaults to the real ~/.crypto_trader/outcomes).
    Returns the number of records written.
    """
    sink = outcome_journal or TradeOutcomeJournal()
    written = 0
    for seq, row in enumerate(event_rows):
        try:
            event_type = row[1]
            if event_type not in _CLOSE_TYPES:
                continue
            symbol = row[2]
            p = _payload(row)
            if not p:
                continue
            sink.record(_record_from_close(symbol, p, seq))
            written += 1
        except Exception as e:  # never let one bad row abort the backfill
            logger.warning("[BACKFILL] skipped row %s: %s", seq, e)
    logger.info("[BACKFILL] wrote %d outcome records", written)
    return written
