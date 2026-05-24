"""F1 — venue-resident protective stops + software-backup gating + reconciler repair."""
from decimal import Decimal
import uuid

from crypto_trader.wallet import (
    DATA_DIR, EnhancedFuturesWallet, Order, OrderStatus, OrderType, Playbook, PositionSide,
)


class FakeEngine:
    """Records orders/cancels. Market orders fill at 100; resting orders get ids."""

    def __init__(self, venue_positions=None, open_orders=None):
        self.calls = []
        self.cancelled = []
        self._n = 0
        self._venue_positions = venue_positions if venue_positions is not None else []
        self._open_orders = open_orders if open_orders is not None else []

    def place_order(self, symbol, side, quantity, order_type, *, trigger_price=None,
                    limit_price=None, reduce_only=False, expires_at=None, client_order_id=None):
        self._n += 1
        self.calls.append({
            "symbol": symbol, "side": side, "qty": Decimal(str(quantity)),
            "type": order_type, "trigger": trigger_price, "reduce_only": reduce_only,
        })
        if order_type == OrderType.MARKET:
            return Order(id=f"ex-{self._n}", symbol=symbol, side=side, order_type=order_type,
                         quantity=Decimal(str(quantity)), status=OrderStatus.FILLED, created_at=0,
                         avg_fill_price=Decimal("100"), filled_quantity=Decimal(str(quantity)))
        return Order(id=f"prot-{self._n}", symbol=symbol, side=side, order_type=order_type,
                     quantity=Decimal(str(quantity)), status=OrderStatus.NEW, created_at=0)

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True

    # account-state reads (for the reconciler)
    def get_balances(self):
        return {}

    def get_positions(self):
        return self._venue_positions

    def get_open_orders(self, symbol=None):
        return self._open_orders

    def get_fills(self, symbol=None, **kw):
        return []

    def sync_positions(self):
        return {p["symbol"]: p for p in self._venue_positions}


def _setup(side=PositionSide.LONG):
    return {
        "entry_price": "100",
        "side": side,
        "playbook": Playbook.INTRADAY,
        "sl_price": "90" if side == PositionSide.LONG else "110",
        "tp_price": "110" if side == PositionSide.LONG else "90",
    }


def _live_wallet(symbol, engine, **kwargs):
    ns = f"test_{uuid.uuid4().hex}"
    for pat in (f"wallet_{ns}_{symbol}*", f"wallet_events_{ns}_{symbol}*"):
        for p in DATA_DIR.glob(pat):
            p.unlink(missing_ok=True)
    w = EnhancedFuturesWallet(symbol=symbol, state_namespace=ns, leverage=1, **kwargs)
    w.attach_execution_engine(engine, live=True)
    return w


def _stop_orders(engine):
    return [c for c in engine.calls if c["type"] == OrderType.STOP_MARKET]


def test_open_places_opposite_side_stop(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    eng = FakeEngine()
    w = _live_wallet("SOLUSDT", eng)
    pos = w.open_position("SOLUSDT", _setup(), mark_price=100, custom_quantity=1)
    stops = _stop_orders(eng)
    assert len(stops) == 1
    s = stops[0]
    assert s["side"] == PositionSide.SHORT          # opposite of LONG
    assert s["trigger"] == pos.sl_price             # at the SL
    assert s["qty"] == pos.remaining_quantity       # full fill qty
    assert pos.protective_orders.get("sl")          # id recorded on the position


def test_close_cancels_protective_stop(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    eng = FakeEngine()
    w = _live_wallet("SOLUSDT", eng)
    pos = w.open_position("SOLUSDT", _setup(), mark_price=100, custom_quantity=1)
    sl_id = pos.protective_orders["sl"]
    w.close_position("SOLUSDT", mark_price=105, reason="T")
    assert sl_id in eng.cancelled


def test_partial_close_rePlaces_stop_at_reduced_qty(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    eng = FakeEngine()
    w = _live_wallet("SOLUSDT", eng)
    pos = w.open_position("SOLUSDT", _setup(), mark_price=100, custom_quantity=2)
    first_sl = pos.protective_orders["sl"]
    w.partial_close("SOLUSDT", mark_price=105, pct=0.5, reason="T1")
    pos2 = w.get_open_position("SOLUSDT")
    # old stop cancelled, a new one placed sized to the reduced position
    assert first_sl in eng.cancelled
    stops = _stop_orders(eng)
    assert len(stops) == 2
    assert stops[-1]["qty"] == pos2.remaining_quantity
    assert pos2.protective_orders["sl"] != first_sl


def test_software_sl_level_is_backup_when_venue_stop_rests(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    eng = FakeEngine()
    w = _live_wallet("SOLUSDT", eng, software_sl_backup_bps=15)
    pos = w.open_position("SOLUSDT", _setup(), mark_price=100, custom_quantity=1)
    level = w._software_sl_level(pos)
    # LONG backup level sits below the real SL by the buffer.
    assert level < pos.sl_price
    assert abs(level - pos.sl_price * (Decimal("1") - Decimal("15") / Decimal("10000"))) < Decimal("1e-9")
    # With no venue stop, the software level equals the raw SL.
    pos.protective_orders = {}
    assert w._software_sl_level(pos) == pos.sl_price


def test_software_monitor_holds_at_exact_sl_when_venue_stop_present(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    eng = FakeEngine()
    w = _live_wallet("SOLUSDT", eng)
    setup = _setup()
    setup["sl_price"] = "99"   # 1% stop, 1x leverage -> no liquidation/catastrophic at SL
    pos = w.open_position("SOLUSDT", setup, mark_price=100, custom_quantity=1)
    assert pos.protective_orders.get("sl")
    # Price touches the exact SL: the venue stop is primary, software must NOT exit.
    w.update_positions("SOLUSDT", mark_price=99, candle_close_time=pos.open_time + 1000)
    assert w.get_open_position("SOLUSDT") is not None
    # Past the backup buffer: software net fires.
    w.update_positions("SOLUSDT", mark_price=98.5, candle_close_time=pos.open_time + 2000)
    assert w.get_open_position("SOLUSDT") is None


def test_reconciler_repairs_missing_stop_without_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from crypto_trader.execution.reconciler import Reconciler
    from crypto_trader.risk import RiskManager

    eng = FakeEngine()
    w = _live_wallet("SOLUSDT", eng)
    pos = w.open_position("SOLUSDT", _setup(), mark_price=100, custom_quantity=1)
    qty = float(pos.remaining_quantity)
    # Venue still holds the position, but the SL order is gone from open orders.
    eng._venue_positions = [{"symbol": "SOLUSDT", "quantity": qty, "side": "LONG", "entry_price": 100.0}]
    eng._open_orders = []
    risk = RiskManager()
    rec = Reconciler(w, eng, risk)
    stops_before = len(_stop_orders(eng))
    mismatches = rec.reconcile("SOLUSDT")
    kinds = {m.kind for m in mismatches}
    assert "missing_protective_stop" in kinds
    assert all(m.repaired for m in mismatches if m.kind == "missing_protective_stop")
    assert len(_stop_orders(eng)) == stops_before + 1   # re-placed
    assert not risk.kill_switch
