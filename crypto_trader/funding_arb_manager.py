"""
crypto_trader.funding_arb_manager — Delta-neutral funding-rate arbitrage
=========================================================================
Non-directional carry strategy: long SPOT + short equal-size PERP, collect the
perp funding every 8h, unwind when funding decays/flips or basis blows out. It
is orthogonal to the directional regime->playbook router and the single-entry
OrderManager guard, so it lives in its own manager invoked alongside the signal
tick (NOT as a pseudo-playbook).

Entry sequences PERP-first (the leg with liquidation/rejection risk) then SPOT,
so a perp failure leaves us cleanly flat and the only naked window is a net-short
perp that can be flattened by buying it back. Unwind reverses that order. Any
state where we cannot neutralise (perp buy-back fails) trips the global HALT.
"""
from __future__ import annotations

import logging
import time
import uuid
from decimal import Decimal, ROUND_DOWN
from typing import Callable, Optional

from .wallet import OrderType, PositionSide
from .arb_book import (
    ArbBook, ArbPosition,
    ST_PLACING_PERP, ST_PERP_ONLY, ST_HEDGED, ST_UNWINDING_PERP, ST_UNWINDING_SPOT, ST_CLOSED,
)
from . import safe_mode

logger = logging.getLogger("crypto_trader.funding_arb_manager")


def _d(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


class FundingArbManager:
    def __init__(
        self,
        symbol: str,
        spot_engine,
        perp_engine,
        arb_book: ArbBook,
        cfg,
        *,
        mark_fn: Callable[[str], float],
        funding_fn: Callable[[str], float],
        spot_mark_fn: Optional[Callable[[str], float]] = None,
        now_ms_fn: Callable[[], int] = lambda: int(time.time() * 1000),
    ):
        self.symbol = symbol
        self.spot = spot_engine
        self.perp = perp_engine
        self.book = arb_book
        self.cfg = cfg
        self.mark_fn = mark_fn
        self.funding_fn = funding_fn
        self.spot_mark_fn = spot_mark_fn or mark_fn
        self._now = now_ms_fn

    # ── config helpers ──
    def _c(self, name, default):
        return getattr(self.cfg, name, default) if self.cfg else default

    @property
    def enabled(self) -> bool:
        return bool(self._c("funding_arb_enabled", False))

    def _basis(self) -> Decimal:
        perp = _d(self.mark_fn(self.symbol))
        spot = _d(self.spot_mark_fn(self.symbol))
        if spot <= 0:
            return Decimal("0")
        return (perp - spot) / spot

    # ── main tick ──
    def on_signal_tick(self):
        if not self.enabled:
            return
        try:
            arb = self.book.get_open(self.symbol)
            if safe_mode.HALT_FILE.exists():
                if arb and arb.state == ST_HEDGED:
                    self._unwind(arb, "halt")
                return
            if arb is None:
                self._maybe_enter()
            elif arb.state == ST_HEDGED:
                self._manage(arb)
            else:
                self._resolve(arb)  # stuck mid-FSM — reconcile/complete
        except Exception as e:  # never let arb crash the signal loop
            logger.error("[ARB] %s tick failed: %s", self.symbol, e)

    # ── entry ──
    def _entry_condition(self, fr: Decimal, basis: Decimal) -> bool:
        return (
            fr >= _d(self._c("min_funding_to_enter", 0.0001))
            and abs(basis) <= _d(self._c("max_entry_basis", 0.005))
        )

    def _size_legs(self, mark: Decimal) -> Decimal:
        notional = _d(self._c("funding_arb_notional_usdt", 0))
        if notional <= 0 or mark <= 0:
            return Decimal("0")
        qty = notional / mark
        # Round to a conservative common precision so both venues' step checks
        # accept the SAME base qty (each engine re-rounds; equal input keeps the
        # legs hedged net of venue rounding).
        step = _d(self._c("funding_arb_qty_step", "0.0001"))
        if step > 0:
            qty = (qty / step).to_integral_value(rounding=ROUND_DOWN) * step
        return qty

    def _maybe_enter(self):
        fr = _d(self.funding_fn(self.symbol))
        basis = self._basis()
        if not self._entry_condition(fr, basis):
            return
        mark = _d(self.mark_fn(self.symbol))
        qty = self._size_legs(mark)
        if qty <= 0:
            logger.info("[ARB] %s entry skipped — zero/invalid size (notional/mark)", self.symbol)
            return

        arb = ArbPosition(
            arb_id=f"arb-{self.symbol}-{uuid.uuid4().hex[:8]}",
            symbol=self.symbol, state=ST_PLACING_PERP,
            perp_qty=qty, spot_qty=qty, entry_basis=basis, open_time=self._now(),
            last_funding_ts=self._now(),
        )
        logger.info("[ARB] %s ENTER funding=%.4f%% basis=%.4f%% qty=%s",
                    self.symbol, float(fr) * 100, float(basis) * 100, qty)

        # Leg 1: short the perp (most likely to fail -> clean flat on failure).
        try:
            perp_order = self.perp.place_order(self.symbol, PositionSide.SHORT, qty, OrderType.MARKET)
        except Exception as e:
            logger.warning("[ARB] %s perp leg failed — staying flat: %s", self.symbol, e)
            # Nothing was opened; mark CLOSED so it's never registered as an open
            # hedge (book.update only tracks non-terminal states as open).
            arb.state = ST_CLOSED
            self.book.update(arb, "ARB_ENTRY_ROLLED_BACK", {"reason": f"perp_leg_failed: {e}"})
            return
        arb.perp_entry_price = _d(getattr(perp_order, "avg_fill_price", 0) or 0)
        arb.perp_order_id = str(getattr(perp_order, "id", "") or "")
        arb.state = ST_PERP_ONLY
        self.book.open_record(arb)
        self.book.update(arb, "ARB_PERP_LEG_FILLED")

        # Leg 2: buy the spot to complete the hedge (immediately, no wait).
        try:
            spot_order = self.spot.place_order(self.symbol, PositionSide.LONG, qty, OrderType.MARKET)
        except Exception as e:
            logger.warning("[ARB] %s spot leg failed — rolling back perp: %s", self.symbol, e)
            self._rollback_perp(arb, reason=f"spot_leg_failed: {e}")
            return
        arb.spot_entry_price = _d(getattr(spot_order, "avg_fill_price", 0) or 0)
        arb.spot_order_id = str(getattr(spot_order, "id", "") or "")
        arb.state = ST_HEDGED
        self.book.update(arb, "ARB_OPENED")
        logger.info("[ARB] %s HEDGED spot@%s perp@%s", self.symbol,
                    arb.spot_entry_price, arb.perp_entry_price)

    def _rollback_perp(self, arb: ArbPosition, reason: str):
        """Buy back the short perp to return to flat. HALT if it cannot."""
        arb.state = ST_UNWINDING_PERP
        self.book.update(arb, "ARB_LEG_UNWOUND", {"leg": "perp_rollback", "reason": reason})
        try:
            self.perp.place_order(self.symbol, PositionSide.LONG, arb.perp_qty, OrderType.MARKET)
        except Exception as e:
            logger.critical("[ARB] %s perp rollback FAILED — naked short, HALTING: %s", self.symbol, e)
            safe_mode.trip_halt(f"funding-arb perp rollback failed for {self.symbol}: {e}")
            return
        self.book.update(arb, "ARB_ENTRY_ROLLED_BACK", {"reason": reason})
        self.book.close(arb, reason="entry_rolled_back", realized_pnl=Decimal("0"))

    # ── manage open hedge ──
    def _manage(self, arb: ArbPosition):
        fr = _d(self.funding_fn(self.symbol))
        self._maybe_apply_funding(arb, fr)
        reason = self._evaluate_exit(arb, fr)
        if reason:
            self._unwind(arb, reason)

    def _maybe_apply_funding(self, arb: ArbPosition, fr: Decimal):
        interval = int(self._c("funding_interval_ms", 28_800_000))
        if self._now() - arb.last_funding_ts < interval:
            return
        perp_notional = arb.perp_qty * _d(self.mark_fn(self.symbol))
        # Short perp RECEIVES funding when the rate is positive (longs pay shorts).
        amount = perp_notional * fr
        self.book.record_funding(arb, fr, perp_notional, amount)
        logger.info("[ARB] %s funding accrued %s (rate=%.4f%%)", self.symbol, amount, float(fr) * 100)

    def _evaluate_exit(self, arb: ArbPosition, fr: Decimal) -> Optional[str]:
        if fr <= _d(self._c("funding_exit_floor", 0.0)):
            return "funding_flip"
        if fr < _d(self._c("min_funding_to_hold", 0.00003)):
            return "funding_decay"
        drift = abs(self._basis() - arb.entry_basis)
        if drift > _d(self._c("max_basis_drift", 0.006)):
            return "basis_drift"
        return None

    # ── unwind ──
    def _unwind(self, arb: ArbPosition, reason: str):
        logger.info("[ARB] %s UNWIND reason=%s", self.symbol, reason)
        # Leg 1: buy back the perp (neutralise liquidation risk first).
        if arb.state in (ST_HEDGED, ST_UNWINDING_PERP):
            arb.state = ST_UNWINDING_PERP
            self.book.update(arb, "ARB_LEG_UNWOUND", {"leg": "perp", "reason": reason})
            try:
                self.perp.place_order(self.symbol, PositionSide.LONG, arb.perp_qty, OrderType.MARKET)
            except Exception as e:
                logger.critical("[ARB] %s perp buy-back FAILED — HALTING: %s", self.symbol, e)
                safe_mode.trip_halt(f"funding-arb perp unwind failed for {self.symbol}: {e}")
                return
            arb.state = ST_UNWINDING_SPOT
            self.book.update(arb, "ARB_LEG_UNWOUND", {"leg": "perp_done"})

        # Leg 2: sell the spot (no time pressure once perp is flat).
        perp_exit = _d(self.mark_fn(self.symbol))
        try:
            spot_order = self.spot.place_order(self.symbol, PositionSide.SHORT, arb.spot_qty, OrderType.MARKET)
        except Exception as e:
            logger.warning("[ARB] %s spot sell failed — will retry next tick: %s", self.symbol, e)
            return  # stays UNWINDING_SPOT; _resolve retries
        spot_exit = _d(getattr(spot_order, "avg_fill_price", 0) or 0) or perp_exit

        spot_pnl = (spot_exit - arb.spot_entry_price) * arb.spot_qty
        perp_pnl = (arb.perp_entry_price - perp_exit) * arb.perp_qty
        realized = arb.accrued_funding + spot_pnl + perp_pnl - arb.entry_fees
        self.book.close(arb, reason=reason, realized_pnl=realized)
        logger.info("[ARB] %s CLOSED reason=%s pnl=%s (funding=%s)",
                    self.symbol, reason, realized, arb.accrued_funding)

    # ── crash/stuck recovery ──
    def _resolve(self, arb: ArbPosition):
        """Best-effort reconcile of a non-terminal arb left by a crash/partial."""
        if arb.state == ST_PERP_ONLY:
            # Hedge incomplete: complete the spot leg, else roll back the perp.
            try:
                spot_order = self.spot.place_order(self.symbol, PositionSide.LONG, arb.spot_qty, OrderType.MARKET)
                arb.spot_entry_price = _d(getattr(spot_order, "avg_fill_price", 0) or 0)
                arb.spot_order_id = str(getattr(spot_order, "id", "") or "")
                arb.state = ST_HEDGED
                self.book.update(arb, "ARB_OPENED")
            except Exception as e:
                self._rollback_perp(arb, reason=f"resolve_perp_only: {e}")
        elif arb.state in (ST_UNWINDING_PERP, ST_UNWINDING_SPOT):
            self._unwind(arb, reason=arb.close_reason or "resume_unwind")
        elif arb.state == ST_PLACING_PERP:
            # Never confirmed a perp fill — treat as flat.
            self.book.close(arb, reason="abandoned_placing", realized_pnl=Decimal("0"))
