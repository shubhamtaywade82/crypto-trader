"""
crypto_trader.execution.state_machine — Operational order lifecycle
====================================================================
Tracks the *operational* state of an order as it travels to the venue and back.
This is intentionally separate from the wallet's *accounting* ``OrderStatus``:
this models "where is this order in its journey", the accounting enum models
"how does this order affect the portfolio".

    CREATED -> SUBMITTED -> ACKED -> PARTIAL -> FILLED
                                  -> CANCELLED
                                  -> REJECTED
                                  -> EXPIRED
                                  -> FAILED
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, Set


class OrderState(str, Enum):
    CREATED = "CREATED"        # persisted locally, not yet sent
    SUBMITTED = "SUBMITTED"    # sent to venue, awaiting ack
    ACKED = "ACKED"            # venue accepted, resting/working
    PARTIAL = "PARTIAL"        # partially filled
    FILLED = "FILLED"          # terminal: fully filled
    CANCELLED = "CANCELLED"    # terminal
    REJECTED = "REJECTED"      # terminal: venue refused
    EXPIRED = "EXPIRED"        # terminal
    FAILED = "FAILED"          # terminal: transport/unknown failure


_TERMINAL: Set[OrderState] = {
    OrderState.FILLED,
    OrderState.CANCELLED,
    OrderState.REJECTED,
    OrderState.EXPIRED,
    OrderState.FAILED,
}

_ALLOWED: Dict[OrderState, Set[OrderState]] = {
    OrderState.CREATED: {OrderState.SUBMITTED, OrderState.FAILED, OrderState.REJECTED},
    OrderState.SUBMITTED: {
        OrderState.ACKED, OrderState.PARTIAL, OrderState.FILLED,
        OrderState.REJECTED, OrderState.EXPIRED, OrderState.FAILED, OrderState.CANCELLED,
    },
    OrderState.ACKED: {
        OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELLED,
        OrderState.EXPIRED, OrderState.REJECTED, OrderState.FAILED,
    },
    OrderState.PARTIAL: {
        OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELLED,
        OrderState.EXPIRED, OrderState.FAILED,
    },
}


def is_terminal(state: OrderState) -> bool:
    return state in _TERMINAL


def can_transition(current: OrderState, target: OrderState) -> bool:
    if is_terminal(current):
        return False
    return target in _ALLOWED.get(current, set())


class InvalidTransition(Exception):
    pass


class OrderLifecycle:
    """Mutable holder enforcing legal transitions for a single order."""

    def __init__(self, client_order_id: str, state: OrderState = OrderState.CREATED,
                 symbol: str = "", intent: str = ""):
        self.client_order_id = client_order_id
        self.state = state
        self.symbol = symbol
        self.intent = intent

    def transition(self, target: OrderState) -> OrderState:
        if self._is_repeated_partial_fill(target):
            return self.state  # repeated partial fills are allowed
        if not self._can_transition_to(target):
            raise InvalidTransition(
                f"{self.client_order_id}: illegal {self.state.value} -> {target.value}"
            )
        self.state = target
        return self.state

    def _is_repeated_partial_fill(self, target: OrderState) -> bool:
        return self.state == OrderState.PARTIAL and target == OrderState.PARTIAL

    def _can_transition_to(self, target: OrderState) -> bool:
        return can_transition(self.state, target)

    @property
    def terminal(self) -> bool:
        return is_terminal(self.state)

