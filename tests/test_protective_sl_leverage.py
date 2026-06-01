"""
Regression: protective SL/TP must be placed at the POSITION's leverage, not the
engine's configured leverage. CoinDCX rejects (422 "Order leverage must be equal
to position leverage") otherwise — hit when adopting a venue position opened at a
different leverage (e.g. venue SOL @ 10x while engine config is 2x).
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from crypto_trader.wallet import EnhancedFuturesWallet, PositionSide, Playbook, OrderType


class _RecordingEngine:
    """Captures the leverage passed to place_order for protective orders."""
    def __init__(self):
        self.calls = []
        self.leverage = 2  # engine config — deliberately != position leverage

    def place_order(self, symbol, side, quantity, order_type, *,
                    trigger_price=None, limit_price=None, reduce_only=False,
                    expires_at=None, client_order_id=None, leverage=None):
        self.calls.append({
            "symbol": symbol, "order_type": order_type,
            "reduce_only": reduce_only, "leverage": leverage,
        })
        return SimpleNamespace(id=f"ord-{len(self.calls)}")


def _wallet():
    w = EnhancedFuturesWallet(symbol="SOLUSDT", state_namespace="test_sl_lev")
    w.execution_engine = _RecordingEngine()
    w.live_execution = True
    w.venue_sl_enabled = True
    w.venue_tp_enabled = False
    w.require_venue_sl = False
    return w


def _pos(leverage=10):
    from crypto_trader.wallet import EnhancedPosition
    return EnhancedPosition(
        symbol="SOLUSDT", side=PositionSide.LONG, playbook=Playbook.INTRADAY,
        entry_price=Decimal("82.2"), original_quantity=Decimal("3.02"),
        remaining_quantity=Decimal("3.02"), notional=Decimal("248.2"),
        margin_used=Decimal("24.8"), leverage=leverage, open_time=0,
        sl_price=Decimal("80.5"), tp_levels=[],
    )


def test_protective_sl_uses_position_leverage_not_engine():
    w = _wallet()
    pos = _pos(leverage=10)
    placed = w._place_protective_orders(pos)
    assert placed.get("sl")
    sl_call = next(c for c in w.execution_engine.calls
                   if c["order_type"] == OrderType.STOP_MARKET)
    # Must use the POSITION's leverage (10), NOT the engine's config (2).
    assert sl_call["leverage"] == 10
    assert sl_call["reduce_only"] is True


def test_protective_tp_also_uses_position_leverage():
    w = _wallet()
    w.venue_tp_enabled = True
    pos = _pos(leverage=10)
    pos.tp_levels = [{"price": Decimal("85.0"), "pct": 1.0, "hit": False, "label": "TP"}]
    w._place_protective_orders(pos)
    tp_call = next(c for c in w.execution_engine.calls
                   if c["order_type"] == OrderType.TAKE_PROFIT)
    assert tp_call["leverage"] == 10


# ── position-attached TP/SL (create_tpsl) — preferred CoinDCX path ───────────

class _TpslEngine:
    """Engine exposing create_position_tpsl (real CoinDCX path). No separate
    margin-locking orders — attaches SL/TP to the position by id."""
    def __init__(self, position_id="pos-1"):
        self.leverage = 2
        self._pid = position_id
        self.tpsl_calls = []
        self.place_order_calls = []

    def get_positions(self):
        return [{"symbol": "SOLUSDT", "position_id": self._pid,
                 "quantity": 3.02, "side": "LONG", "leverage": 10}]

    def create_position_tpsl(self, position_id, *, symbol="", stop_loss_price=None, take_profit_price=None):
        self.tpsl_calls.append({
            "position_id": position_id, "symbol": symbol,
            "sl": stop_loss_price, "tp": take_profit_price,
        })
        return {"stop_loss": {"id": "sl-1", "status": "untriggered"}}

    def place_order(self, *a, **k):
        self.place_order_calls.append((a, k))
        return SimpleNamespace(id="should-not-be-used")


def _wallet_tpsl(engine):
    w = EnhancedFuturesWallet(symbol="SOLUSDT", state_namespace="test_tpsl")
    w.execution_engine = engine
    w.live_execution = True
    w.venue_sl_enabled = True
    w.venue_tp_enabled = False
    w.require_venue_sl = False
    return w


def test_uses_create_tpsl_when_available_not_separate_order():
    eng = _TpslEngine()
    w = _wallet_tpsl(eng)
    placed = w._place_protective_orders(_pos(leverage=10))
    # create_tpsl used; NO separate margin-locking order placed
    assert len(eng.tpsl_calls) == 1
    assert eng.place_order_calls == []
    assert placed.get("sl") == "sl-1"
    call = eng.tpsl_calls[0]
    assert call["position_id"] == "pos-1"
    assert call["sl"] == Decimal("80.5")


def test_create_tpsl_halts_when_position_id_missing_and_required():
    eng = _TpslEngine()
    eng.get_positions = lambda: []  # no venue position id resolvable
    w = _wallet_tpsl(eng)
    w.require_venue_sl = True
    import crypto_trader.safe_mode as sm
    sm.clear_halt()
    with pytest.raises(RuntimeError):
        w._place_protective_orders(_pos(leverage=10))
    sm.clear_halt()


def test_create_tpsl_missing_id_non_required_returns_empty():
    eng = _TpslEngine()
    eng.get_positions = lambda: []
    w = _wallet_tpsl(eng)
    w.require_venue_sl = False
    placed = w._place_protective_orders(_pos(leverage=10))
    assert placed == {}  # software SL backup covers it; no halt
