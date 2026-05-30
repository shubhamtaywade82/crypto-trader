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

    def fetch_symbol_leverage(self, symbol):
        return self.leverage

    def get_positions(self):
        return self._positions

    def get_balances(self):
        return {"USDT": 100.0}

    def get_open_orders(self, symbol=None):
        return []

    def get_fills(self, symbol=None):
        return []

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
    # In paper mode, wallet opens a position marked mode='paper'
    w = EnhancedFuturesWallet(symbol="SOLUSDT", state_namespace="test_modes_paper", leverage=5)
    setup = {
        "entry_price": 100.0, "side": PositionSide.LONG, "playbook": Playbook.INTRADAY,
        "sl_price": 95.0, "tp_price": 105.0,
    }
    pos_paper = w.open_position("SOLUSDT", setup, mark_price=100.0, custom_quantity=1.0)
    assert pos_paper is not None
    assert pos_paper.mode == "paper"

    # In live mode, wallet opens a position marked mode='live'
    w_live = EnhancedFuturesWallet(symbol="SOLUSDT", state_namespace="test_modes_live", leverage=5)
    w_live.attach_execution_engine(_MockEngine(leverage=5), live=True)
    pos_live = w_live.open_position("SOLUSDT", setup, mark_price=100.0, custom_quantity=1.0)
    assert pos_live is not None
    assert pos_live.mode == "live"

def test_adopt_venue_position_mode():
    w = EnhancedFuturesWallet(symbol="SOLUSDT", state_namespace="test_adopt_mode", leverage=5)
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
    # Sync from initialization / attachment
    w = EnhancedFuturesWallet(symbol="SOLUSDT", state_namespace="test_lev_sync", leverage=5)
    assert w.get_leverage("SOLUSDT") == 5

    engine = _MockEngine(leverage=12)
    w.attach_execution_engine(engine, live=True)
    
    # Verify leverage was automatically updated to 12
    assert w.get_leverage("SOLUSDT") == 12
    assert w.leverage == 12

def test_reconciler_ignores_paper_positions():
    w = EnhancedFuturesWallet(symbol="SOLUSDT", state_namespace="test_recon_ignore", leverage=5)
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
