"""State machine, idempotent client IDs, order-manager duplicate guard, reconciler."""
from decimal import Decimal

import pytest

from crypto_trader.execution.client_order_id import make_client_order_id
from crypto_trader.execution.state_machine import (
    InvalidTransition,
    OrderLifecycle,
    OrderState,
    can_transition,
    is_terminal,
)
from crypto_trader.execution.order_manager import DuplicateOrderError, OrderManager
from crypto_trader.execution.reconciler import Reconciler
from crypto_trader.wallet import Order, OrderStatus, OrderType, PositionSide


# ── client order ids ──────────────────────────────────────────────────────
def test_client_order_id_deterministic_and_bounded():
    a = make_client_order_id("SOLUSDT", "entry-long", nonce="123")
    b = make_client_order_id("SOLUSDT", "entry-long", nonce="123")
    c = make_client_order_id("SOLUSDT", "entry-long", nonce="124")
    assert a == b              # same intent+nonce -> idempotent
    assert a != c              # different nonce -> unique
    assert len(a) <= 36


# ── state machine ──────────────────────────────────────────────────────────
def test_state_machine_legal_path():
    lc = OrderLifecycle("x")
    lc.transition(OrderState.SUBMITTED)
    lc.transition(OrderState.ACKED)
    lc.transition(OrderState.PARTIAL)
    lc.transition(OrderState.PARTIAL)   # repeated partials allowed
    lc.transition(OrderState.FILLED)
    assert lc.terminal


def test_state_machine_rejects_illegal_and_terminal():
    lc = OrderLifecycle("x")
    with pytest.raises(InvalidTransition):
        lc.transition(OrderState.FILLED)  # cannot fill before submit
    lc2 = OrderLifecycle("y", OrderState.FILLED)
    assert is_terminal(lc2.state)
    assert not can_transition(OrderState.FILLED, OrderState.PARTIAL)


# ── order manager ──────────────────────────────────────────────────────────
class _FakeEngine:
    """Returns a still-working (non-terminal) order so the entry stays 'working'."""
    def __init__(self, status=OrderStatus.NEW):
        self.placed = 0
        self.status = status

    def place_order(self, symbol, side, qty, otype, *, trigger_price=None, limit_price=None,
                    reduce_only=False, expires_at=None, client_order_id=None):
        self.placed += 1
        filled = qty if self.status == OrderStatus.FILLED else Decimal("0")
        return Order(id=f"ex-{self.placed}", symbol=symbol, side=side, order_type=otype,
                     quantity=qty, status=self.status, created_at=0,
                     reduce_only=reduce_only, filled_quantity=filled, avg_fill_price=Decimal("100"))

    def cancel_order(self, oid):
        return True


def test_order_manager_blocks_duplicate_working_entry():
    om = OrderManager(_FakeEngine(), wallet=None)
    om.submit("SOLUSDT", PositionSide.LONG, Decimal("1"), intent="entry", nonce="1")
    # a still-working entry exists for the symbol -> second entry rejected
    with pytest.raises(DuplicateOrderError):
        om.submit("SOLUSDT", PositionSide.LONG, Decimal("1"), intent="entry", nonce="2")


def test_order_manager_blocks_inflight_same_client_id():
    om = OrderManager(_FakeEngine(), wallet=None)
    om.submit("SOLUSDT", PositionSide.LONG, Decimal("1"), intent="entry", nonce="1")
    with pytest.raises(DuplicateOrderError):
        om.submit("SOLUSDT", PositionSide.LONG, Decimal("1"), intent="entry", nonce="1")


def test_order_manager_single_placement():
    eng = _FakeEngine()
    om = OrderManager(eng, wallet=None)
    om.submit("SOLUSDT", PositionSide.LONG, Decimal("1"), intent="entry", nonce="1")
    assert eng.placed == 1


# ── reconciler ─────────────────────────────────────────────────────────────
class _Pos:
    def __init__(self, qty, side="LONG"):
        self.status = "OPEN"
        self.remaining_quantity = Decimal(str(qty))
        self.side = PositionSide(side)
        self.entry_price = Decimal("100")
        self.mode = "live"


class _Wallet:
    def __init__(self, positions):
        self.positions = positions
        self.live_execution = True
        self.symbol = "SOLUSDT"


class _VenueEngine:
    def __init__(self, positions):
        self._positions = positions

    def get_balances(self):
        return {"USDT": 100.0}

    def get_positions(self):
        return self._positions

    def get_open_orders(self, symbol=None):
        return []

    def get_fills(self, symbol=None):
        return []


class _Risk:
    def __init__(self):
        self.killed = False

    def trigger_kill_switch(self, reason):
        self.killed = True


def test_reconciler_detects_ghost_and_trips_kill_switch():
    wallet = _Wallet({"SOLUSDT": _Pos("2.0")})
    venue = _VenueEngine([])  # venue has no position -> ghost
    risk = _Risk()
    rec = Reconciler(wallet, venue, risk)
    mismatches = rec.reconcile("SOLUSDT")
    assert any(m.kind == "ghost_position" for m in mismatches)
    assert risk.killed is True


def test_reconciler_clean_when_matching():
    wallet = _Wallet({"SOLUSDT": _Pos("2.0")})
    venue = _VenueEngine([{"symbol": "SOLUSDT", "quantity": 2.0, "side": "LONG",
                           "entry_price": 100.0, "leverage": 2}])
    risk = _Risk()
    rec = Reconciler(wallet, venue, risk)
    assert rec.reconcile("SOLUSDT") == []
    assert risk.killed is False
