import pytest
from decimal import Decimal
from types import SimpleNamespace
from crypto_trader.wallet import EnhancedFuturesWallet, PositionSide, Playbook, EnhancedPosition, OrderType, OrderStatus
from crypto_trader.execution.reconciler import Reconciler
from crypto_trader.execution.account_sync import AccountSnapshot

class _MockEngine:
    def __init__(self, leverage=5, positions=None):
        self.leverage = leverage
        self._positions = positions or []
        self.update_calls = []
        self.margin_currency = "USDT"

    def fetch_symbol_leverage(self, symbol):
        return self.leverage

    def get_positions(self):
        return self._positions

    def get_balances(self):
        return {"USDT": 100.0}

    def sync_balance(self):
        return 100.0

    def get_open_orders(self, symbol=None):
        return []

    def get_fills(self, symbol=None):
        return []

    def place_order(self, symbol, side, qty, otype, *, trigger_price=None, limit_price=None,
                    reduce_only=False, expires_at=None, client_order_id=None, leverage=None):
        return SimpleNamespace(
            id="mock-ord-1",
            avg_fill_price=Decimal("100.0"),
        )

def test_position_mode_serialization():
    # 1. Check default mode is paper
    pos = EnhancedPosition(
        symbol="SOLUSDT", side=PositionSide.LONG, playbook=Playbook.INTRADAY,
        entry_price=Decimal("100"), original_quantity=Decimal("1"),
        remaining_quantity=Decimal("1"), notional=Decimal("100"),
        margin_used=Decimal("10"), leverage=10, open_time=12345,
        sl_price=Decimal("95")
    )
    assert pos.mode == "paper"

    # 2. Check serialization / deserialization preserves mode
    pos.mode = "live"
    d = pos.to_dict()
    assert d["mode"] == "live"
    
    pos2 = EnhancedPosition.from_dict(d)
    assert pos2.mode == "live"

    # Backward compatibility: default to paper if mode missing from data
    del d["mode"]
    pos3 = EnhancedPosition.from_dict(d)
    assert pos3.mode == "paper"

def test_wallet_open_position_modes():
    import uuid
    # In paper mode, wallet opens a position marked mode='paper'
    w = EnhancedFuturesWallet(symbol="SOLUSDT", state_namespace=f"test_{uuid.uuid4().hex}", leverage=5)
    setup = {
        "entry_price": 100.0, "side": PositionSide.LONG, "playbook": Playbook.INTRADAY,
        "sl_price": 95.0, "tp_price": 105.0,
    }
    pos_paper = w.open_position("SOLUSDT", setup, mark_price=100.0, custom_quantity=1.0)
    assert pos_paper is not None
    assert pos_paper.mode == "paper"

    # In live mode, wallet opens a position marked mode='live'
    w_live = EnhancedFuturesWallet(symbol="SOLUSDT", state_namespace=f"test_{uuid.uuid4().hex}", leverage=5)
    w_live.attach_execution_engine(_MockEngine(leverage=5), live=True)
    pos_live = w_live.open_position("SOLUSDT", setup, mark_price=100.0, custom_quantity=1.0)
    assert pos_live is not None
    assert pos_live.mode == "live"

def test_adopt_venue_position_mode():
    import uuid
    w = EnhancedFuturesWallet(symbol="SOLUSDT", state_namespace=f"test_{uuid.uuid4().hex}", leverage=5)
    pos = w.adopt_venue_position(
        symbol="SOLUSDT",
        side=PositionSide.SHORT,
        quantity=2.0,
        entry_price=100.0,
        leverage=10,
    )
    assert pos is not None
    assert pos.mode == "live"
    assert pos.leverage == 10

def test_leverage_syncing_from_engine():
    import uuid
    # Sync from initialization / attachment
    w = EnhancedFuturesWallet(symbol="SOLUSDT", state_namespace=f"test_{uuid.uuid4().hex}", leverage=5)
    assert w.get_leverage("SOLUSDT") == 5

    engine = _MockEngine(leverage=12)
    w.attach_execution_engine(engine, live=True)
    
    # Verify leverage was automatically updated to 12
    assert w.get_leverage("SOLUSDT") == 12
    assert w.leverage == 12

def test_reconciler_ignores_paper_positions():
    import uuid
    w = EnhancedFuturesWallet(symbol="SOLUSDT", state_namespace=f"test_{uuid.uuid4().hex}", leverage=5)
    setup = {
        "entry_price": 100.0, "side": PositionSide.LONG, "playbook": Playbook.INTRADAY,
        "sl_price": 95.0, "tp_price": 105.0,
    }
    # Open paper position
    pos_paper = w.open_position("SOLUSDT", setup, mark_price=100.0, custom_quantity=1.0)
    assert pos_paper.mode == "paper"

    # Reconciler check: internal live positions should be flat (empty), venue flat
    engine = _MockEngine(positions=[])
    rec = Reconciler(w, engine, risk_manager=None, adopt_venue=False)
    mismatches = rec.reconcile("SOLUSDT")
    # Because pos_paper is mode='paper', it's excluded from live check -> no desync or ghost position mismatch
    assert mismatches == []


def test_reconciler_quantity_drift_auto_align():
    import uuid
    # Set up wallet with a live position of quantity 3.02
    w = EnhancedFuturesWallet(symbol="SOLUSDT", state_namespace=f"test_{uuid.uuid4().hex}", leverage=5)
    setup = {
        "entry_price": 100.0, "side": PositionSide.LONG, "playbook": Playbook.INTRADAY,
        "sl_price": 95.0, "tp_price": 105.0,
    }
    # attach live execution engine
    w.attach_execution_engine(_MockEngine(leverage=5), live=True)
    pos = w.open_position("SOLUSDT", setup, mark_price=100.0, custom_quantity=3.02)
    assert pos.mode == "live"
    assert pos.remaining_quantity == Decimal("3.02")

    # Venue has quantity 6.04
    engine = _MockEngine(positions=[{"symbol": "SOLUSDT", "quantity": 6.04, "side": "LONG", "leverage": 5}])
    
    # Reconciler with adopt_venue=False -> should NOT auto-align and should report mismatch
    rec_no_adopt = Reconciler(w, engine, risk_manager=None, adopt_venue=False)
    mismatches = rec_no_adopt.reconcile("SOLUSDT")
    qty_mismatch = next(m for m in mismatches if m.kind == "position_qty")
    assert qty_mismatch.repaired is False
    assert pos.remaining_quantity == Decimal("3.02")

    # Reconciler with adopt_venue=True -> should auto-align
    rec_adopt = Reconciler(w, engine, risk_manager=None, adopt_venue=True)
    mismatches_adopt = rec_adopt.reconcile("SOLUSDT")
    qty_mismatch_adopt = next(m for m in mismatches_adopt if m.kind == "position_qty")
    assert qty_mismatch_adopt.repaired is True
    assert pos.remaining_quantity == Decimal("6.04")
    assert pos.original_quantity == Decimal("6.04")


def test_exchange_state_reconciler_quantity_drift_auto_align():
    import uuid
    from crypto_trader.reconciliation import ExchangeStateReconciler

    class _MockRisk:
        def __init__(self):
            self.kill_switch = False
            self.kill_switch_reason = None
        def trigger_kill_switch(self, reason):
            self.kill_switch = True
            self.kill_switch_reason = reason

    w = EnhancedFuturesWallet(symbol="SOLUSDT", state_namespace=f"test_{uuid.uuid4().hex}", leverage=5)
    setup = {
        "entry_price": 100.0, "side": PositionSide.LONG, "playbook": Playbook.INTRADAY,
        "sl_price": 95.0, "tp_price": 105.0,
    }
    
    # 1. Non-live mode -> position remains paper, reconciler ignores it because mode != 'live'
    engine = _MockEngine(positions=[])
    risk = _MockRisk()
    
    pos = w.open_position("SOLUSDT", setup, mark_price=100.0, custom_quantity=3.02)
    assert pos.mode == "paper"

    recon = ExchangeStateReconciler(engine, w, risk)
    # reconcile_symbol returns True because it filters out paper positions
    assert recon.reconcile_symbol("SOLUSDT") is True
    assert risk.kill_switch is False
    assert pos.remaining_quantity == Decimal("3.02")

    # 2. Live mode -> should auto-align and NOT trigger kill switch
    engine_live = _MockEngine(positions=[{"symbol": "SOLUSDT", "quantity": 6.04, "side": "LONG", "leverage": 5}])
    w_live = EnhancedFuturesWallet(symbol="SOLUSDT", state_namespace=f"test_{uuid.uuid4().hex}", leverage=5)
    w_live.attach_execution_engine(engine_live, live=True)
    pos_live = w_live.open_position("SOLUSDT", setup, mark_price=100.0, custom_quantity=3.02)
    assert pos_live.mode == "live"
    assert pos_live.remaining_quantity == Decimal("3.02")

    recon_live = ExchangeStateReconciler(engine_live, w_live, risk)
    # reconcile_symbol should align position quantity and return True (no kill switch trigger)
    assert recon_live.reconcile_symbol("SOLUSDT") is True
    assert risk.kill_switch is False
    assert pos_live.remaining_quantity == Decimal("6.04")
    assert pos_live.original_quantity == Decimal("6.04")
