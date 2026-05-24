import hmac
import hashlib
import json
import os
from decimal import Decimal
from unittest.mock import patch, MagicMock
import pytest

from crypto_trader.wallet import (
    CoinDCXExecutionEngine,
    EnhancedFuturesWallet,
    PositionSide,
    OrderType,
    OrderStatus
)

@pytest.fixture
def execution_engine():
    # Instantiate execution engine with mock credentials and construction acknowledgment
    return CoinDCXExecutionEngine(
        api_key="mock_api_key",
        api_secret="mock_api_secret",
        i_understand_real_money=True
    )

def test_symbol_mapping(execution_engine):
    # Test mapping Binance symbol to CoinDCX
    assert execution_engine._map_symbol("SOLUSDT") == "B-SOL_USDT"
    assert execution_engine._map_symbol("BTCUSDT") == "B-BTC_USDT"
    assert execution_engine._map_symbol("B-SOL_USDT") == "B-SOL_USDT"

    # Test mapping CoinDCX symbol back to Binance
    assert execution_engine._map_symbol_back("B-SOL_USDT") == "SOLUSDT"
    assert execution_engine._map_symbol_back("B-BTC_USDT") == "BTCUSDT"

@patch("requests.post")
def test_signature_headers(mock_post, execution_engine, monkeypatch):
    # Set safe mode to allowed for testing
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_TRADING_ACK", "I_UNDERSTAND_REAL_MONEY_WILL_BE_LOST")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "order": {
            "id": "mock-order-123",
            "status": "filled",
            "price": 12.5,
            "total_quantity": 10.0
        }
    }
    mock_post.return_value = mock_response

    # Place order to trigger request signature check
    execution_engine.place_order(
        symbol="SOLUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("10.0"),
        order_type=OrderType.MARKET
    )

    # Assert request was made to correct URL
    mock_post.assert_called_once()
    call_args, call_kwargs = mock_post.call_args
    assert call_args[0] == "https://api.coindcx.com/exchange/v1/derivatives/futures/orders/create"

    # Check HMAC-SHA256 signature in headers
    headers = call_kwargs["headers"]
    assert headers["X-AUTH-APIKEY"] == "mock_api_key"
    assert "X-AUTH-SIGNATURE" in headers

    # Verify signature math
    payload_str = call_kwargs["data"]
    expected_signature = hmac.new(
        b"mock_api_secret",
        payload_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    assert headers["X-AUTH-SIGNATURE"] == expected_signature

@patch("requests.post")
def test_place_order_market(mock_post, execution_engine, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_TRADING_ACK", "I_UNDERSTAND_REAL_MONEY_WILL_BE_LOST")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "order": {
            "id": "order-xyz",
            "status": "filled",
            "price": 100.5,
            "total_quantity": 2.5,
            "created_at": 1716496800000
        }
    }
    mock_post.return_value = mock_response

    order = execution_engine.place_order(
        symbol="SOLUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("2.5"),
        order_type=OrderType.MARKET
    )

    assert order.id == "order-xyz"
    assert order.symbol == "SOLUSDT"
    assert order.status == OrderStatus.FILLED
    assert order.avg_fill_price == Decimal("100.5")
    assert order.filled_quantity == Decimal("2.5")

@patch("requests.post")
def test_cancel_order(mock_post, execution_engine, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_TRADING_ACK", "I_UNDERSTAND_REAL_MONEY_WILL_BE_LOST")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"status": "cancelled"}
    mock_post.return_value = mock_response

    success = execution_engine.cancel_order("order-xyz")
    assert success is True

@patch("requests.post")
def test_sync_positions(mock_post, execution_engine):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "positions": [
            {
                "pair": "B-SOL_USDT",
                "active_pos": 5.5,
                "avg_price": 120.2,
                "leverage": 10
            },
            {
                "pair": "B-BTC_USDT",
                "active_pos": -0.015,
                "avg_price": 65000.0,
                "leverage": 10
            },
            {
                "pair": "B-ETH_USDT",
                "active_pos": 0.0,
                "avg_price": 3000.0,
                "leverage": 10
            }
        ]
    }
    mock_post.return_value = mock_response

    positions = execution_engine.sync_positions()
    
    assert len(positions) == 2
    assert "SOLUSDT" in positions
    assert "BTCUSDT" in positions
    assert "ETHUSDT" not in positions  # Should skip 0.0 positions

    assert positions["SOLUSDT"]["side"] == "LONG"
    assert positions["SOLUSDT"]["quantity"] == Decimal("5.5")
    assert positions["SOLUSDT"]["entry_price"] == Decimal("120.2")

    assert positions["BTCUSDT"]["side"] == "SHORT"
    assert positions["BTCUSDT"]["quantity"] == Decimal("0.015")
    assert positions["BTCUSDT"]["entry_price"] == Decimal("65000.0")

@patch("requests.post")
def test_sync_balance(mock_post, execution_engine):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "total_account_equity": 1045.25,
        "total_wallet_balance": 1000.00
    }
    mock_post.return_value = mock_response

    balance = execution_engine.sync_balance()
    assert balance == Decimal("1045.25")

def test_precision_rounding(execution_engine):
    # Test _get_precision mapping
    assert execution_engine._get_precision("BTCUSDT") == (3, 2)
    assert execution_engine._get_precision("ETHUSDT") == (2, 2)
    assert execution_engine._get_precision("SOLUSDT") == (2, 2)
    assert execution_engine._get_precision("UNKNOWN") == (2, 2)

@patch("requests.post")
def test_place_order_precision_rounding(mock_post, execution_engine, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_TRADING_ACK", "I_UNDERSTAND_REAL_MONEY_WILL_BE_LOST")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "order": {
            "id": "order-rounded",
            "status": "open",
            "price": 100.55,
            "total_quantity": 2.55,
            "created_at": 1716496800000
        }
    }
    mock_post.return_value = mock_response

    # Pass excessive decimals for BTCUSDT (qty 3, price 2)
    execution_engine.place_order(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("2.554321"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("100.55999")
    )

    mock_post.assert_called_once()
    _, call_kwargs = mock_post.call_args
    payload = json.loads(call_kwargs["data"])
    order_payload = payload["order"]

    assert order_payload["total_quantity"] == 2.554
    assert order_payload["price"] == 100.56
