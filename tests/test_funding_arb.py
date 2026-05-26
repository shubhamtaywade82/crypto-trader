"""Tests for delta-neutral funding-rate arbitrage (Strategy C, paper)."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from crypto_trader.wallet import Order, OrderStatus, OrderType, PositionSide
from crypto_trader.arb_book import ArbBook, ST_HEDGED, ST_CLOSED, ST_PERP_ONLY
from crypto_trader.funding_arb_manager import FundingArbManager
from crypto_trader.exchanges.coindcx_spot_execution import PaperSpotExecutionEngine


class _FakePerp:
    def __init__(self, price_fn, fail_sides=()):
        self.price_fn = price_fn
        self.fail_sides = set(fail_sides)
        self.calls = []

    def place_order(self, symbol, side, qty, order_type, **kw):
        self.calls.append((side, Decimal(str(qty))))
        if side in self.fail_sides:
            raise RuntimeError(f"perp {side} rejected")
        px = Decimal(str(self.price_fn(symbol)))
        return Order(id=f"perp-{len(self.calls)}", symbol=symbol, side=side,
                     order_type=order_type, quantity=Decimal(str(qty)),
                     status=OrderStatus.FILLED, created_at=0,
                     filled_quantity=Decimal(str(qty)), avg_fill_price=px)


class _FakeSpot(PaperSpotExecutionEngine):
    def __init__(self, price_fn, fail_sides=()):
        super().__init__(price_fn)
        self.fail_sides = set(fail_sides)

    def place_order(self, symbol, side, qty, order_type, **kw):
        if side in self.fail_sides:
            raise RuntimeError(f"spot {side} rejected")
        return super().place_order(symbol, side, qty, order_type, **kw)


def _cfg(**over):
    base = dict(
        funding_arb_enabled=True, funding_arb_notional_usdt=1000.0,
        funding_arb_qty_step=0.001, min_funding_to_enter=0.0001,
        funding_exit_floor=0.0, min_funding_to_hold=0.00003,
        max_entry_basis=0.005, max_basis_drift=0.006, funding_interval_ms=1000,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _mgr(tmp_path, *, funding, perp=None, spot=None, clock=None):
    price = lambda s: 100.0
    fr = {"v": funding}
    book = ArbBook(namespace="t", data_dir=tmp_path)
    m = FundingArbManager(
        "SOLUSDT",
        spot or _FakeSpot(price),
        perp or _FakePerp(price),
        book, _cfg(),
        mark_fn=price, funding_fn=lambda s: fr["v"],
        now_ms_fn=clock or (lambda: 0),
    )
    return m, book, fr


def test_enter_then_hedged(tmp_path):
    m, book, _ = _mgr(tmp_path, funding=0.0005)
    m.on_signal_tick()
    arb = book.get_open("SOLUSDT")
    assert arb is not None and arb.state == ST_HEDGED
    assert arb.spot_qty == arb.perp_qty
    assert arb.spot_qty > 0


def test_no_entry_when_funding_low(tmp_path):
    m, book, _ = _mgr(tmp_path, funding=0.00001)
    m.on_signal_tick()
    assert book.get_open("SOLUSDT") is None


def test_spot_leg_failure_rolls_back_perp_to_flat(tmp_path):
    price = lambda s: 100.0
    perp = _FakePerp(price)
    spot = _FakeSpot(price, fail_sides=(PositionSide.LONG,))  # buy fails
    m, book, _ = _mgr(tmp_path, funding=0.0005, perp=perp, spot=spot)
    m.on_signal_tick()
    # No open hedge; perp was bought back (short then long).
    assert book.get_open("SOLUSDT") is None
    sides = [s for s, _ in perp.calls]
    assert sides == [PositionSide.SHORT, PositionSide.LONG]


def test_funding_accrues_then_unwinds_on_decay(tmp_path):
    clock = {"t": 0}
    price = lambda s: 100.0
    book = ArbBook(namespace="t2", data_dir=tmp_path)
    fr = {"v": 0.0005}
    m = FundingArbManager(
        "SOLUSDT", _FakeSpot(price), _FakePerp(price), book, _cfg(),
        mark_fn=price, funding_fn=lambda s: fr["v"], now_ms_fn=lambda: clock["t"],
    )
    m.on_signal_tick()  # enter -> HEDGED
    arb = book.get_open("SOLUSDT")
    assert arb.state == ST_HEDGED

    # Advance past one funding interval -> accrues (short receives positive funding).
    clock["t"] = 2000
    m.on_signal_tick()
    arb = book.get_open("SOLUSDT")
    assert arb.accrued_funding > 0
    assert arb.funding_payments == 1

    # Funding decays below hold threshold -> unwind -> CLOSED.
    fr["v"] = 0.000001
    clock["t"] = 3000
    m.on_signal_tick()
    assert book.get_open("SOLUSDT") is None


def test_unwind_on_funding_flip(tmp_path):
    m, book, fr = _mgr(tmp_path, funding=0.0005)
    m.on_signal_tick()
    assert book.get_open("SOLUSDT").state == ST_HEDGED
    fr["v"] = -0.0002  # flips negative
    m.on_signal_tick()
    assert book.get_open("SOLUSDT") is None


def test_arb_book_persistence_and_reload(tmp_path):
    m, book, _ = _mgr(tmp_path, funding=0.0005)
    m.on_signal_tick()
    assert book.get_open("SOLUSDT").state == ST_HEDGED
    # A fresh book over the same dir recovers the open hedge from the snapshot.
    book2 = ArbBook(namespace="t", data_dir=tmp_path)
    reloaded = book2.get_open("SOLUSDT")
    assert reloaded is not None and reloaded.state == ST_HEDGED
    assert reloaded.spot_qty == m.book.get_open("SOLUSDT").spot_qty


def test_perp_leg_failure_stays_flat(tmp_path):
    price = lambda s: 100.0
    perp = _FakePerp(price, fail_sides=(PositionSide.SHORT,))  # perp short rejected
    m, book, _ = _mgr(tmp_path, funding=0.0005, perp=perp)
    m.on_signal_tick()
    assert book.get_open("SOLUSDT") is None  # never registered as open
    assert book.non_terminal() == []
