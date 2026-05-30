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
        adopt_venue: bool = False,
    ):
        self.wallet = wallet
        self.engine = execution_engine
        self.account_sync = AccountSync(execution_engine)
        self.risk = risk_manager
        self.bus = bus
        self.qty_tolerance = qty_tolerance
        # G5: on unresolved desync, cancel ALL venue orders to protect capital.
        self.strict_cancel = strict_cancel
        # Recovery: adopt a venue position the internal wallet lacks instead of
        # halting (synthesize internal state + place a stop). Default OFF.
        self.adopt_venue = adopt_venue
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
        # Sync leverage for the target symbol or all positions in the snapshot
        if getattr(self.wallet, "live_execution", False) and self.engine:
            if symbol:
                if symbol in snap.positions:
                    lev = snap.positions[symbol].get("leverage")
                    if lev is not None:
                        if not hasattr(self.wallet, "symbol_leverages"):
                            self.wallet.symbol_leverages = {}
                        self.wallet.symbol_leverages[symbol] = int(lev)
                        if symbol == self.wallet.symbol:
                            self.wallet.leverage = int(lev)
                        if hasattr(self.engine, "leverage"):
                            self.engine.leverage = int(lev)
                else:
                    if hasattr(self.wallet, "sync_leverage_from_venue"):
                        self.wallet.sync_leverage_from_venue(symbol)
            else:
                for sym, pos_data in snap.positions.items():
                    lev = pos_data.get("leverage")
                    if lev is not None:
                        if not hasattr(self.wallet, "symbol_leverages"):
                            self.wallet.symbol_leverages = {}
                        self.wallet.symbol_leverages[sym] = int(lev)
                        if sym == self.wallet.symbol:
                            self.wallet.leverage = int(lev)
                if getattr(self.wallet, "symbol", None) and hasattr(self.wallet, "sync_leverage_from_venue"):
                    self.wallet.sync_leverage_from_venue(self.wallet.symbol)

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
                    if self.adopt_venue:
                        pos = self.wallet.positions.get(sym)
                        if pos:
                            pos.remaining_quantity = vq
                            pos.original_quantity = vq
                            if hasattr(self.wallet, "_runtime_reducer_state"):
                                rstate = self.wallet._runtime_reducer_state
                                if hasattr(rstate, "open_positions") and sym in rstate.open_positions:
                                    rstate.open_positions[sym]["quantity"] = vq
                                    entry = rstate.open_positions[sym].get("entry_price", pos.entry_price)
                                    lev = rstate.open_positions[sym].get("leverage", pos.leverage)
                                    rstate.open_positions[sym]["margin"] = (vq * entry) / lev
                            if hasattr(self.wallet, "_save_state"):
                                self.wallet._save_state()
                            logger.info("[RECON] Auto-aligned quantity drift for %s: local %s -> venue %s", sym, iq, vq)
                            out.append(ReconciliationMismatchEvent(
                                symbol=sym, kind="position_qty",
                                internal=str(iq), exchange=str(vq),
                                repaired=True,
                                detail="quantity drift auto-aligned to venue",
                            ))
                            continue
                    out.append(ReconciliationMismatchEvent(
                        symbol=sym, kind="position_qty",
                        internal=str(iq), exchange=str(vq),
                        detail="quantity drift beyond tolerance",
                    ))
        return out

    def _find_missing_positions(self, internal: dict, venue: dict) -> List[ReconciliationMismatchEvent]:
        out: List[ReconciliationMismatchEvent] = []
        for sym, vpos in venue.items():
            if sym in internal or Decimal(str(vpos["quantity"])) <= 0:
                continue
            # Recovery path: adopt the venue position into the wallet (synthesize
            # internal state + place a protective stop) so it's managed rather
            # than halting. repaired=True keeps the kill switch from tripping.
            if self.adopt_venue:
                adopted = self._adopt(sym, vpos)
                out.append(ReconciliationMismatchEvent(
                    symbol=sym, kind="missing_position",
                    internal="0", exchange=str(vpos["quantity"]),
                    repaired=bool(adopted),
                    detail="venue position adopted into wallet" if adopted
                           else "venue position adoption failed",
                ))
            else:
                out.append(ReconciliationMismatchEvent(
                    symbol=sym, kind="missing_position",
                    internal="0", exchange=str(vpos["quantity"]),
                    detail="venue position missing internally",
                ))
        return out

    def _adopt(self, sym: str, vpos: dict) -> bool:
        """Adopt a single venue position into the wallet. Returns success."""
        if not hasattr(self.wallet, "adopt_venue_position"):
            logger.error("[RECON] wallet has no adopt_venue_position; cannot adopt %s", sym)
            return False
        try:
            from ..wallet import PositionSide
            raw_side = str(vpos.get("side", "LONG")).upper()
            side = PositionSide.LONG if raw_side in ("LONG", "BUY") else PositionSide.SHORT
            pos = self.wallet.adopt_venue_position(
                symbol=sym,
                side=side,
                quantity=float(vpos["quantity"]),
                entry_price=float(vpos.get("entry_price") or vpos.get("avg_price") or 0),
                leverage=int(vpos["leverage"]) if vpos.get("leverage") else None,
            )
            return pos is not None
        except Exception as e:
            logger.error("[RECON] adoption of %s failed: %s", sym, e)
            return False

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
            if getattr(pos, "status", "") != "OPEN" or getattr(pos, "mode", "paper") != "live":
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
        internal_syms = {s for s, p in self.wallet.positions.items() if getattr(p, "status", "") == "OPEN" and getattr(p, "mode", "paper") == "live"}
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
            if getattr(pos, "status", "") == "OPEN" and getattr(pos, "mode", "paper") == "live":
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
