"""
Close flip-guard: CoinDCX has no reduce-only flag — an opposite-side exit that
exceeds the venue's actual position nets through zero and OPENS an opposite
position. close_position must clamp the exit qty to the venue qty, and skip the
venue order entirely when the venue is already flat (SL/TP/liq fired).
"""
import uuid
from decimal import Decimal

import pytest

from crypto_trader.wallet import (
    DATA_DIR, EnhancedFuturesWallet, PositionSide, Playbook, OrderType,
)


class FakeEngine:
    """Records venue orders; reports a scriptable venue position list."""
    def __init__(self, venue_positions=None):
        self.calls = []
        self.cancelled = []
        self._venue_positions = venue_positions or []
        self._n = 0

    def place_order(self, symbol, side, quantity, order_type, *, trigger_price=None,
                    limit_price=None, reduce_only=False, expires_at=None,
                    client_order_id=None, leverage=None):
        self._n += 1
        self.calls.append({"type": order_type, "side": side, "qty": Decimal(str(quantity))})
        from crypto_trader.wallet import Order, OrderStatus
        px = Decimal("100")
        return Order(id=f"o{self._n}", symbol=symbol, side=side, order_type=order_type,
                     quantity=Decimal(str(quantity)), status=OrderStatus.FILLED, created_at=0,
                     avg_fill_price=px, filled_quantity=Decimal(str(quantity)))

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True

    def get_balances(self):
        return {}

    def get_positions(self):
        return self._venue_positions

    def get_open_orders(self, symbol=None):
        return []

    def get_fills(self, symbol=None, **kw):
        return []

    def sync_positions(self):
        return {p["symbol"]: p for p in self._venue_positions}


def _wallet(symbol, engine, monkeypatch, tmp_path):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    ns = f"test_{uuid.uuid4().hex}"
    w = EnhancedFuturesWallet(symbol=symbol, state_namespace=ns, leverage=1)
    w.attach_execution_engine(engine, live=True)
    return w


def _setup():
    return {"entry_price": "100", "side": PositionSide.LONG,
            "playbook": Playbook.INTRADAY, "sl_price": "90", "tp_price": "110"}


def _market_exits(eng):
    return [c for c in eng.calls if c["type"] == OrderType.MARKET]


def test_venue_position_qty_reads_venue(monkeypatch, tmp_path):
    eng = FakeEngine(venue_positions=[{"symbol": "SOLUSDT", "quantity": 1.5}])
    w = _wallet("SOLUSDT", eng, monkeypatch, tmp_path)
    assert w._venue_position_qty("SOLUSDT") == Decimal("1.5")
    assert w._venue_position_qty("XRPUSDT") == Decimal("0")  # not listed → flat


def test_close_clamps_exit_to_venue_qty(monkeypatch, tmp_path):
    # internal opens 2.0, but venue only holds 1.2 → exit must clamp to 1.2 (no flip)
    eng = FakeEngine(venue_positions=[{"symbol": "SOLUSDT", "quantity": 1.2}])
    w = _wallet("SOLUSDT", eng, monkeypatch, tmp_path)
    w.open_position("SOLUSDT", _setup(), mark_price=100, custom_quantity=2)
    eng.calls.clear()
    w.close_position("SOLUSDT", mark_price=105, reason="T")
    exits = _market_exits(eng)
    assert len(exits) == 1
    assert exits[0]["qty"] == Decimal("1.2")          # clamped to venue, NOT 2.0
    assert exits[0]["side"] == PositionSide.SHORT


def test_close_skips_venue_order_when_already_flat(monkeypatch, tmp_path):
    # venue flat (SL/TP/liq already fired) → no venue exit order, just book close
    eng = FakeEngine(venue_positions=[])
    w = _wallet("SOLUSDT", eng, monkeypatch, tmp_path)
    w.open_position("SOLUSDT", _setup(), mark_price=100, custom_quantity=1)
    eng.calls.clear()
    pos = w.close_position("SOLUSDT", mark_price=105, reason="SL")
    assert _market_exits(eng) == []                   # no flipping order sent
    assert pos is not None and pos.status == "CLOSED" # internal still flattened


def test_close_uses_internal_qty_when_venue_read_fails(monkeypatch, tmp_path):
    eng = FakeEngine(venue_positions=[{"symbol": "SOLUSDT", "quantity": 1.0}])
    w = _wallet("SOLUSDT", eng, monkeypatch, tmp_path)
    w.open_position("SOLUSDT", _setup(), mark_price=100, custom_quantity=1)
    # make the venue read fail → falls back to internal qty (no clamp, no skip)
    def _boom():
        raise RuntimeError("venue down")
    eng.get_positions = _boom
    eng.calls.clear()
    w.close_position("SOLUSDT", mark_price=105, reason="T")
    exits = _market_exits(eng)
    assert len(exits) == 1
    assert exits[0]["qty"] == Decimal("1")            # internal qty used
