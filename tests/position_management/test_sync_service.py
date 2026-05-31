"""Tests for PositionSyncService — position discovery and state management."""

import threading
import time

import pytest

from crypto_trader.position_management.sync_service import (
    ExternalPosition,
    PositionSource,
    PositionSyncService,
    PositionSyncState,
)


def _make_raw(
    exchange_id="EX001",
    symbol="BTCUSDT",
    side="LONG",
    qty=0.01,
    entry=105000.0,
    leverage=10,
    ltp=106000.0,
    sl=None,
    tp=None,
) -> dict:
    return {
        "id": exchange_id,
        "symbol": symbol,
        "positionSide": side,
        "positionAmt": qty,
        "entryPrice": entry,
        "leverage": leverage,
        "markPrice": ltp,
        "unRealizedProfit": (ltp - entry) * qty if side == "LONG" else (entry - ltp) * qty,
        "stopLossPrice": sl,
        "takeProfitPrice": tp,
        "initialMargin": entry * qty / leverage,
    }


class FakeRest:
    def __init__(self, positions):
        self._positions = positions

    def get_futures_positions(self):
        return self._positions


class TestPositionSyncService:
    def test_bootstrap_discovers_positions(self):
        raw = [_make_raw(exchange_id="EX001", symbol="BTCUSDT")]
        svc = PositionSyncService(rest_client=FakeRest(raw))
        svc._bootstrap_rest()
        assert svc.count() == 1
        pos = svc.get("EX001")
        assert pos is not None
        assert pos.symbol == "BTCUSDT"
        assert pos.side == "LONG"

    def test_new_position_callback_fired(self):
        events = []
        svc = PositionSyncService(
            rest_client=FakeRest([]),
            on_new_position=lambda p: events.append(p),
        )
        raw = _make_raw(exchange_id="EX002", symbol="ETHUSDT")
        svc.on_ws_position_update(raw)
        assert len(events) == 1
        assert events[0].symbol == "ETHUSDT"

    def test_protection_required_when_no_sl(self):
        missing_sl = []
        svc = PositionSyncService(
            rest_client=FakeRest([]),
            on_protection_required=lambda p: missing_sl.append(p.symbol),
        )
        raw = _make_raw(symbol="SOLUSDT", sl=None)
        svc.on_ws_position_update(raw)
        assert "SOLUSDT" in missing_sl

    def test_no_protection_callback_when_sl_present(self):
        missing_sl = []
        svc = PositionSyncService(
            rest_client=FakeRest([]),
            on_protection_required=lambda p: missing_sl.append(p.symbol),
        )
        raw = _make_raw(symbol="BNBUSDT", sl=104000.0, tp=110000.0)
        svc.on_ws_position_update(raw)
        assert "BNBUSDT" not in missing_sl

    def test_ws_close_removes_position(self):
        closed = []
        svc = PositionSyncService(
            rest_client=FakeRest([]),
            on_position_closed=lambda eid: closed.append(eid),
        )
        # First add
        svc.on_ws_position_update(_make_raw(exchange_id="EX010"))
        assert svc.count() == 1
        # Now close (qty = 0)
        raw_closed = _make_raw(exchange_id="EX010", qty=0.0)
        svc.on_ws_position_update(raw_closed)
        assert svc.count() == 0
        assert "EX010" in closed

    def test_ltp_update_recalculates_pnl(self):
        svc = PositionSyncService(rest_client=FakeRest([]))
        svc.on_ws_position_update(_make_raw(symbol="BTCUSDT", exchange_id="EX001", entry=100000.0, ltp=100000.0, qty=0.01))
        svc.on_ws_ltp_update("BTCUSDT", 105000.0)
        pos = svc.get("EX001")
        assert pos.ltp == 105000.0
        assert abs(pos.unrealized_pnl - 50.0) < 0.01  # (105000-100000)*0.01

    def test_mark_protected_updates_state(self):
        svc = PositionSyncService(rest_client=FakeRest([]))
        svc.on_ws_position_update(_make_raw(exchange_id="EX020"))
        svc.mark_protected("EX020")
        pos = svc.get("EX020")
        assert pos.sync_state == PositionSyncState.PROTECTED

    def test_get_by_symbol(self):
        svc = PositionSyncService(rest_client=FakeRest([]))
        svc.on_ws_position_update(_make_raw(exchange_id="EX030", symbol="AVAXUSDT"))
        pos = svc.get_by_symbol("AVAXUSDT")
        assert pos is not None
        assert pos.exchange_id == "EX030"

    def test_manual_source_assigned_for_unknown(self):
        svc = PositionSyncService(rest_client=FakeRest([]), wallet=None)
        svc.on_ws_position_update(_make_raw(exchange_id="EX040", symbol="DOGEUSDT"))
        pos = svc.get("EX040")
        assert pos.source == PositionSource.MANUAL

    def test_external_position_has_stop_loss_false(self):
        pos = ExternalPosition(
            exchange_id="X", symbol="Y", side="LONG",
            qty=1.0, entry_price=100.0, leverage=1,
        )
        assert not pos.has_stop_loss

    def test_external_position_has_stop_loss_true(self):
        pos = ExternalPosition(
            exchange_id="X", symbol="Y", side="LONG",
            qty=1.0, entry_price=100.0, leverage=1,
            stop_loss=98.0,
        )
        assert pos.has_stop_loss
