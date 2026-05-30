"""
crypto_trader.execution.reconciler — State reconciliation vs CoinDCX
=====================================================================
Compares the venue's authoritative account state against the wallet's
event-sourced internal state and reports drift. CoinDCX is the source of truth.

Detected mismatches:
* ghost_position    — internal open position the venue doesn't have
* missing_position  — venue position the internal state is missing
* position_qty      — quantity disagreement beyond tolerance

On unresolved divergence the reconciler trips the RiskManager kill switch so
``can_trade()`` refuses new entries until an operator clears it. Run on boot
(``reconcile``) and periodically.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import List, Optional

from ..events import EventBus, KillSwitchTriggeredEvent, ReconciliationMismatchEvent
from .account_sync import AccountSnapshot, AccountSync

logger = logging.getLogger("crypto_trader.execution.reconciler")

_QTY_TOLERANCE = Decimal("0.01")   # relative drift allowed before flagging


class Reconciler:
    def __init__(
        self,
        wallet,
        execution_engine,
        risk_manager=None,
        *,
        bus: Optional[EventBus] = None,
        qty_tolerance: Decimal = _QTY_TOLERANCE,
        strict_cancel: bool = False,
    ):
        self.wallet = wallet
        self.engine = execution_engine
        self.account_sync = AccountSync(execution_engine)
        self.risk = risk_manager
        self.bus = bus
        self.qty_tolerance = qty_tolerance
        # G5: on unresolved desync, cancel ALL venue orders to protect capital.
        self.strict_cancel = strict_cancel
        # True after a reconcile where the venue snapshot could not be fetched.
        self.snapshot_failed = False

    def reconcile(self, symbol: Optional[str] = None) -> List[ReconciliationMismatchEvent]:
        snap = self.account_sync.snapshot(symbol)
        self.snapshot_failed = not snap.ok
        if not snap.ok:
            # Truth unverifiable (transient). Do NOT persist a kill switch — the
            # caller (gate / runtime loop) pauses trading for now and retries.
            logger.warning("Reconciliation snapshot unavailable: %s", snap.error)
            return []
        mismatches = self._compare(snap, symbol)
        for m in mismatches:
            if self.bus:
                self.bus.publish(m)
            logger.warning("Reconciliation mismatch: %s internal=%s exchange=%s",
                           m.kind, m.internal, m.exchange)
        unresolved = [m for m in mismatches if not m.repaired]
        if unresolved:
            # G5: strict mode flattens the venue order book before halting, so a
            # corrupted local state can't leave working orders unsupervised.
            if self.strict_cancel:
                try:
                    n = self.engine.cancel_all_orders(symbol)
                    logger.critical("Strict reconcile: cancelled %d venue order(s) on desync", n)
                except Exception as e:
                    logger.error("Strict cancel-all failed: %s", e)
            self._trip(f"{len(unresolved)} unresolved reconciliation mismatch(es)")
        return mismatches

    # ── comparison ───────────────────────────────────────────────────────────
    def _compare(self, snap: AccountSnapshot,
                 symbol: Optional[str] = None) -> List[ReconciliationMismatchEvent]:
        internal = self._internal_positions()
        venue = snap.positions

        # The venue snapshot returns ALL open positions, but a per-symbol
        # reconcile must only compare THAT symbol — otherwise a live position in
        # one symbol is flagged as a "missing_position" on every OTHER watchlist
        # symbol's pass, tripping a false kill switch. Scope both sides to the
        # target symbol when one is given (None = whole-account reconcile).
        if symbol is not None:
            internal = {s: p for s, p in internal.items() if s == symbol}
            venue = {s: p for s, p in venue.items() if s == symbol}

        out: List[ReconciliationMismatchEvent] = []
        out.extend(self._find_ghost_positions(internal, venue))
        out.extend(self._find_quantity_drifts(internal, venue))
        out.extend(self._find_missing_positions(internal, venue))
        out.extend(self._reconcile_protective_orders(snap, venue))
        return out

    def _find_ghost_positions(self, internal: dict, venue: dict) -> List[ReconciliationMismatchEvent]:
        out: List[ReconciliationMismatchEvent] = []
        for sym, ipos in internal.items():
            if sym not in venue:
                out.append(ReconciliationMismatchEvent(
                    symbol=sym, kind="ghost_position",
                    internal=str(ipos["quantity"]), exchange="0",
                    detail="internal position not present on venue",
                ))
        return out

    def _find_quantity_drifts(self, internal: dict, venue: dict) -> List[ReconciliationMismatchEvent]:
        out: List[ReconciliationMismatchEvent] = []
        for sym, ipos in internal.items():
            if sym in venue:
                iq = Decimal(str(ipos["quantity"]))
                vq = Decimal(str(venue[sym]["quantity"]))
                if iq > 0 and abs(iq - vq) / iq > self.qty_tolerance:
                    out.append(ReconciliationMismatchEvent(
                        symbol=sym, kind="position_qty",
                        internal=str(iq), exchange=str(vq),
                        detail="quantity drift beyond tolerance",
                    ))
        return out

    def _find_missing_positions(self, internal: dict, venue: dict) -> List[ReconciliationMismatchEvent]:
        out: List[ReconciliationMismatchEvent] = []
        for sym, vpos in venue.items():
            if sym not in internal and Decimal(str(vpos["quantity"])) > 0:
                out.append(ReconciliationMismatchEvent(
                    symbol=sym, kind="missing_position",
                    internal="0", exchange=str(vpos["quantity"]),
                    detail="venue position missing internally",
                ))
        return out

    # ── protective-order reconciliation (F1) ──────────────────────────────────
    def _reconcile_protective_orders(self, snap: AccountSnapshot, venue: dict) -> List[ReconciliationMismatchEvent]:
        """Keep venue-resident protective stops in sync with internal positions.

        * SL order vanished while the venue still holds the position -> re-place it.
        * Open order tied to no internal position -> cancel it (orphan).
        Repairs do not trip the kill switch (``repaired=True``).
        """
        open_ids = {str(o.get("exchange_order_id")) for o in snap.open_orders if o.get("exchange_order_id")}
        tracked_ids: set = set()

        out: List[ReconciliationMismatchEvent] = []
        out.extend(self._sync_protective_stops(venue, open_ids, tracked_ids))
        out.extend(self._cancel_orphan_orders(snap, tracked_ids))
        return out

    def _sync_protective_stops(self, venue: dict, open_ids: set, tracked_ids: set) -> List[ReconciliationMismatchEvent]:
        out: List[ReconciliationMismatchEvent] = []
        for sym, pos in self.wallet.positions.items():
            if getattr(pos, "status", "") != "OPEN":
                continue
            protective = getattr(pos, "protective_orders", {}) or {}
            for oid in protective.values():
                if oid:
                    tracked_ids.add(str(oid))
            sl_id = protective.get("sl")
            # Re-place only when the venue still holds the position but the stop is
            # gone (otherwise the stop fired and the position is being closed).
            if sl_id and sym in venue and str(sl_id) not in open_ids:
                replaced = self.wallet._place_protective_orders(pos)
                out.append(ReconciliationMismatchEvent(
                    symbol=sym, kind="missing_protective_stop",
                    internal=str(sl_id), exchange="absent",
                    repaired=bool(replaced),
                    detail="venue protective stop missing; re-placed" if replaced else "venue protective stop missing; re-place failed",
                ))
        return out

    def _cancel_orphan_orders(self, snap: AccountSnapshot, tracked_ids: set) -> List[ReconciliationMismatchEvent]:
        out: List[ReconciliationMismatchEvent] = []
        internal_syms = {s for s, p in self.wallet.positions.items() if getattr(p, "status", "") == "OPEN"}
        for o in snap.open_orders:
            oid = str(o.get("exchange_order_id") or "")
            osym = o.get("symbol", "")
            if not oid or oid in tracked_ids:
                continue
            if osym and osym not in internal_syms:
                ok = False
                try:
                    ok = self.account_sync.engine.cancel_order(oid)
                except Exception as e:
                    logger.warning("Orphan order cancel failed for %s: %s", oid, e)
                out.append(ReconciliationMismatchEvent(
                    symbol=osym, kind="orphan_order",
                    internal="absent", exchange=oid,
                    repaired=bool(ok),
                    detail="venue order with no internal position; cancelled" if ok else "orphan order; cancel failed",
                ))
        return out


    def _internal_positions(self) -> dict:
        result = {}
        for sym, pos in self.wallet.positions.items():
            if getattr(pos, "status", "") == "OPEN":
                result[sym] = {
                    "quantity": pos.remaining_quantity,
                    "side": pos.side.value,
                    "entry_price": pos.entry_price,
                }
        return result

    def _trip(self, reason: str):
        if self.risk:
            self.risk.trigger_kill_switch(reason)
        if self.bus:
            self.bus.publish(KillSwitchTriggeredEvent(reason=reason, source="reconciliation"))
