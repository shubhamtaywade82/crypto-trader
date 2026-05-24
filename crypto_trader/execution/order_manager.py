"""
crypto_trader.execution.order_manager — Safe order submission
==============================================================
Owns the operational order lifecycle on top of an ``ExecutionEngine``:

* deterministic client order IDs (idempotent retries)
* duplicate-order rejection (same client id, or an existing open order for an
  entry on a symbol that already has a working order)
* bounded retries on transport failures
* records venue fills back into the event-sourced wallet via
  ``wallet.apply_external_fill`` (live) — never the synchronous paper path.

It deliberately does NOT decide *what* to trade; the strategy/orchestrator does.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Dict, Optional

from ..events import EventBus, OrderFilledEvent, OrderSubmittedEvent
from ..wallet import Order, OrderType, PositionSide
from .client_order_id import make_client_order_id
from .state_machine import OrderLifecycle, OrderState

logger = logging.getLogger("crypto_trader.execution.order_manager")


class DuplicateOrderError(Exception):
    pass


class OrderManager:
    def __init__(
        self,
        execution_engine,
        wallet,
        *,
        bus: Optional[EventBus] = None,
        max_retries: int = 2,
        backoff_base: float = 1.5,
    ):
        self.engine = execution_engine
        self.wallet = wallet
        self.bus = bus
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        # client_order_id -> lifecycle
        self.lifecycles: Dict[str, OrderLifecycle] = {}

    def submit(
        self,
        symbol: str,
        side: PositionSide,
        quantity: Decimal,
        order_type: OrderType = OrderType.MARKET,
        *,
        intent: str = "entry",
        nonce: Optional[str] = None,
        trigger_price: Optional[Decimal] = None,
        limit_price: Optional[Decimal] = None,
        reduce_only: bool = False,
        expires_at: Optional[int] = None,
    ) -> Order:
        coid = make_client_order_id(symbol, intent, nonce=nonce)

        existing = self.lifecycles.get(coid)
        if existing and not existing.terminal:
            raise DuplicateOrderError(f"order {coid} already in-flight ({existing.state.value})")

        if not reduce_only and self._has_working_entry(symbol):
            raise DuplicateOrderError(f"working entry already exists for {symbol}")

        lifecycle = self.lifecycles.setdefault(coid, OrderLifecycle(coid))

        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                lifecycle.transition(OrderState.SUBMITTED)
                order = self.engine.place_order(
                    symbol, side, quantity, order_type,
                    trigger_price=trigger_price, limit_price=limit_price,
                    reduce_only=reduce_only, expires_at=expires_at,
                    client_order_id=coid,
                )
                self._on_submitted(order, coid, symbol, side, order_type, quantity, reduce_only)
                self._publish_fill(order, coid, symbol, side)
                return order
            except Exception as e:  # transport/venue failure
                last_exc = e
                logger.warning("submit attempt %d failed for %s: %s", attempt + 1, coid, e)
                if attempt < self.max_retries:
                    time.sleep(self.backoff_base ** attempt)
        lifecycle.transition(OrderState.FAILED)
        raise last_exc if last_exc else RuntimeError("order submission failed")

    def cancel(self, exchange_order_id: str, client_order_id: Optional[str] = None) -> bool:
        ok = self.engine.cancel_order(exchange_order_id)
        if ok and client_order_id and client_order_id in self.lifecycles:
            lc = self.lifecycles[client_order_id]
            if not lc.terminal:
                lc.transition(OrderState.CANCELLED)
        return ok

    # ── internals ────────────────────────────────────────────────────────────
    def _has_working_entry(self, symbol: str) -> bool:
        for coid, lc in self.lifecycles.items():
            if lc.terminal:
                continue
            if symbol.upper() in coid.upper() and "exit" not in coid.lower():
                return True
        return False

    def _on_submitted(self, order, coid, symbol, side, order_type, quantity, reduce_only):
        lc = self.lifecycles[coid]
        if order.status.value == "FILLED":
            lc.transition(OrderState.FILLED)
        elif order.status.value == "PARTIALLY_FILLED":
            lc.transition(OrderState.PARTIAL)
        elif order.status.value in ("REJECTED", "EXPIRED", "CANCELLED"):
            lc.transition(OrderState(order.status.value))
        else:
            lc.transition(OrderState.ACKED)
        if self.bus:
            self.bus.publish(OrderSubmittedEvent(
                symbol=symbol, side=side.value, client_order_id=coid,
                exchange_order_id=order.id, order_type=order_type.value,
                quantity=float(quantity), reduce_only=reduce_only,
            ))

    def _publish_fill(self, order: Order, coid: str, symbol: str, side: PositionSide):
        """Announce a venue fill. Booking into the wallet's event log is the
        orchestrator's responsibility (it holds the entry setup / SL-TP)."""
        filled = Decimal(str(order.filled_quantity or 0))
        if filled <= 0 or not self.bus:
            return
        self.bus.publish(OrderFilledEvent(
            symbol=symbol, side=side.value, client_order_id=coid,
            exchange_order_id=order.id, fill_price=float(order.avg_fill_price or 0),
            fill_quantity=float(filled), partial=(order.status.value == "PARTIALLY_FILLED"),
        ))
