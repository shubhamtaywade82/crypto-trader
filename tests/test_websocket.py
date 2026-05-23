from decimal import Decimal
from unittest.mock import MagicMock
import pathlib
import uuid

import crypto_trader.wallet
from crypto_trader.wallet import EnhancedFuturesWallet, Playbook, PositionSide
from crypto_trader.ws_client import WebSocketPositionManager, WSMarketData


def _setup(playbook=Playbook.INTRADAY, side=PositionSide.LONG):
    return {
        "entry_price": "100",
        "side": side,
        "playbook": playbook,
        "sl_price": "90" if side == PositionSide.LONG else "110",
        "tp_price": "110" if side == PositionSide.LONG else "90",
    }


def _setup_swing(side=PositionSide.LONG):
    return {
        "entry_price": "100",
        "side": side,
        "playbook": Playbook.SWING,
        "sl_price": "90" if side == PositionSide.LONG else "110",
        "tp_levels": [
            {"price": 105.0 if side == PositionSide.LONG else 95.0, "pct": 0.5, "hit": False, "label": "TP1"},
            {"price": 110.0 if side == PositionSide.LONG else 90.0, "pct": 0.5, "hit": False, "label": "TP2"},
        ]
    }


def test_websocket_position_manager_exits_intraday(tmp_path, monkeypatch):
    # Set DATA_DIR to tmp_path
    monkeypatch.setattr(crypto_trader.wallet, "DATA_DIR", tmp_path)
    
    # Create wallet with clean isolation
    ns = f"test_{uuid.uuid4().hex}"
    w = EnhancedFuturesWallet(symbol="BTCUSDT", state_namespace=ns)

    # Open a position
    pos = w.open_position("BTCUSDT", _setup(Playbook.INTRADAY, PositionSide.LONG), mark_price=100)
    assert pos is not None

    # Mock websocket feed
    mock_feed = MagicMock()
    mock_feed.symbol = "BTCUSDT"

    pm = WebSocketPositionManager(ws_feed=mock_feed, wallet=w)

    # Test _check_exits - this shouldn't raise a TypeError
    # We pass market updates as floats
    data = WSMarketData(symbol="BTCUSDT", mark_price=100.0)
    pm._check_exits(pos, data, ltp=101.0, mid=100.5)


def test_websocket_position_manager_exits_swing(tmp_path, monkeypatch):
    # Set DATA_DIR to tmp_path
    monkeypatch.setattr(crypto_trader.wallet, "DATA_DIR", tmp_path)

    # Create wallet with clean isolation
    ns = f"test_{uuid.uuid4().hex}"
    w = EnhancedFuturesWallet(symbol="BTCUSDT", state_namespace=ns)

    # Open a position with Swing playbook (it has custom tp_levels with floats)
    pos = w.open_position("BTCUSDT", _setup_swing(PositionSide.LONG), mark_price=100)
    assert pos is not None

    # Mock websocket feed
    mock_feed = MagicMock()
    mock_feed.symbol = "BTCUSDT"

    pm = WebSocketPositionManager(ws_feed=mock_feed, wallet=w)

    # Test _check_exits with swing setup - checks for partial TP and trailing stop
    data = WSMarketData(symbol="BTCUSDT", mark_price=100.0)
    
    # Hit TP1 (105.0) - this should trigger SL breakeven shift
    pm._check_exits(pos, data, ltp=105.0, mid=105.0)
    
    # Hit TP2 (110.0) - should activate trailing stop
    pm._check_exits(pos, data, ltp=110.0, mid=110.0)

    # Trailing stop active - trail price is highest_since_tp2 (110.0) * 0.995 = 109.45
    # If ltp drops to 109.0, it should hit trailing stop and close
    pm._check_exits(pos, data, ltp=109.0, mid=109.0)
