"""Unit tests for the CoinDCX execution adapter + signing (mocked HTTP)."""
import hashlib
import hmac
import json
import os
from decimal import Decimal
from unittest.mock import patch, MagicMock

import pytest

from crypto_trader.exchanges.coindcx_client import CoinDCXClient, CoinDCXError
from crypto_trader.exchanges.instrument_mapper import (
    InstrumentMapper,
    InstrumentSpec,
    coindcx_to_internal,
    internal_to_coindcx,
)
from crypto_trader.exchanges.coindcx_execution import CoinDCXExecutionEngine
from crypto_trader.wallet import (
    CoinDCXExecutionEngine as WalletCoinDCXExecutionEngine,
    EnhancedFuturesWallet,
    OrderType,
    PositionSide,
    OrderStatus,
)


# ── symbol mapping (exchanges.coindcx_execution) ─────────────────────────────
def test_symbol_mapping_roundtrip():
    assert internal_to_coindcx("SOLUSDT") == "B-SOL_USDT"
    assert internal_to_coindcx("BTCUSDT") == "B-BTC_USDT"
    assert coindcx_to_internal("B-SOL_USDT") == "SOLUSDT"
    # idempotent on already-coindcx symbols
    assert internal_to_coindcx("B-SOL_USDT") == "B-SOL_USDT"


def test_symbol_mapping_rejects_unknown():
    with pytest.raises(ValueError):
        internal_to_coindcx("SOLBUSD")


# ── signing (exchanges.coindcx_client) ───────────────────────────────────────
def test_hmac_signature_matches_reference():
    client = CoinDCXClient(api_key="k", api_secret="secret123")
    body = json.dumps({"a": 1, "timestamp": 42}, separators=(",", ":"))
    expected = hmac.new(b"secret123", body.encode(), hashlib.sha256).hexdigest()
    assert client._sign(body) == expected


def test_signed_headers_require_credentials():
    client = CoinDCXClient()  # no creds
    with pytest.raises(CoinDCXError):
        client._signed_headers("{}")


# ── instrument spec rounding / validation (exchanges.coindcx_execution) ──────
def _spec():
    return InstrumentSpec(
        internal_symbol="SOLUSDT", pair="B-SOL_USDT",
        price_increment=Decimal("0.01"), quantity_increment=Decimal("0.01"),
        min_quantity=Decimal("0.01"), min_notional=Decimal("6.0"),
        max_leverage=5, maker_fee_rate=0.000236, taker_fee_rate=0.00059,
    )


def test_round_to_increment():
    s = _spec()
    assert s.round_price(Decimal("12.3456")) == Decimal("12.34")
    assert s.round_qty(Decimal("1.2399")) == Decimal("1.23")


def test_validate_order_min_notional():
    s = _spec()
    # 0.01 * 100 = 1.0 notional, below min_notional 6.0 -> error
    assert "min_notional" in s.validate_order(Decimal("0.01"), Decimal("100"))
    # 0.1 * 100 = 10.0 notional, qty >= min_quantity -> ok
    assert s.validate_order(Decimal("0.1"), Decimal("100")) is None
    # below min_quantity -> error
    assert "min_quantity" in s.validate_order(Decimal("0.001"), Decimal("100"))


# ── execution adapter with mocked client (exchanges.coindcx_execution) ───────
class _FakeClient:
    def __init__(self, order_resp=None, positions=None, balances=None):
        self.order_resp = order_resp or {"id": "ex-1", "status": "filled",
                                         "avg_price": "100.5", "filled_quantity": "1.0"}
        self.positions = positions if positions is not None else []
        self.balances = balances if balances is not None else [{"currency_short_name": "USDT", "balance": 250.0}]
        self.calls = []

    def get_public(self, endpoint, params=None):
        return {"instrument": {
            "pair": params["pair"], "price_increment": 0.01, "quantity_increment": 0.01,
            "min_quantity": 0.01, "min_notional": 6.0, "max_leverage_long": 5.0,
            "maker_fee": 0.0236, "taker_fee": 0.059, "status": "active",
        }}

    def post_signed(self, endpoint, payload=None):
        self.calls.append((endpoint, payload))
        if endpoint.endswith("orders/create"):
            return self.order_resp
        if endpoint.endswith("positions"):
            return self.positions
        if endpoint.endswith("balances"):
            return self.balances
        return {}


def _engine(fake):
    return CoinDCXExecutionEngine(client=fake, mapper=InstrumentMapper(fake), leverage=2)


def test_place_order_builds_payload_and_parses_fill():
    fake = _FakeClient()
    eng = _engine(fake)
    order = eng.place_order("SOLUSDT", PositionSide.LONG, Decimal("1.0"), OrderType.MARKET,
                            client_order_id="ct-test")
    # payload correctness
    endpoint, payload = fake.calls[-1]
    assert endpoint.endswith("orders/create")
    o = payload["order"]
    assert o["pair"] == "B-SOL_USDT"
    assert o["side"] == "buy"
    assert o["order_type"] == "market_order"
    assert o["leverage"] == 2.0
    assert o["client_order_id"] == "ct-test"
    # parsed fill
    assert order.status.value == "FILLED"
    assert order.avg_fill_price == Decimal("100.5")


def test_reduce_only_exit_payload():
    fake = _FakeClient()
    eng = _engine(fake)
    eng.place_order("SOLUSDT", PositionSide.SHORT, Decimal("1.0"), OrderType.MARKET, reduce_only=True)
    _, payload = fake.calls[-1]
    assert payload["order"]["reduce_only"] is True
    assert payload["order"]["side"] == "sell"


def test_get_balances_and_positions_normalize():
    fake = _FakeClient(positions=[{"pair": "B-SOL_USDT", "active_pos": 2.0, "avg_price": 99.0, "leverage": 2}])
    eng = _engine(fake)
    assert eng.get_balances()["USDT"] == 250.0
    syncs = eng.sync_positions()
    assert "SOLUSDT" in syncs
    assert syncs["SOLUSDT"]["quantity"] == 2.0


# ── Wallet execution engine tests (wallet.CoinDCXExecutionEngine) ────────────

@pytest.fixture
def wallet_execution_engine():
    # Instantiate execution engine with mock credentials and construction acknowledgment
    return WalletCoinDCXExecutionEngine(
        api_key="mock_api_key",
        api_secret="mock_api_secret",
        i_understand_real_money=True
    )


def test_symbol_mapping(wallet_execution_engine):
    # Test mapping Binance symbol to CoinDCX
    assert wallet_execution_engine._map_symbol("SOLUSDT") == "B-SOL_USDT"
    assert wallet_execution_engine._map_symbol("BTCUSDT") == "B-BTC_USDT"
    assert wallet_execution_engine._map_symbol("B-SOL_USDT") == "B-SOL_USDT"

    # Test mapping CoinDCX symbol back to Binance
    assert wallet_execution_engine._map_symbol_back("B-SOL_USDT") == "SOLUSDT"
    assert wallet_execution_engine._map_symbol_back("B-BTC_USDT") == "BTCUSDT"


@patch("requests.post")
def test_signature_headers(mock_post, wallet_execution_engine, monkeypatch):
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
    wallet_execution_engine.place_order(
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
def test_place_order_market(mock_post, wallet_execution_engine, monkeypatch):
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

    order = wallet_execution_engine.place_order(
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
def test_cancel_order(mock_post, wallet_execution_engine, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_TRADING_ACK", "I_UNDERSTAND_REAL_MONEY_WILL_BE_LOST")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"status": "cancelled"}
    mock_post.return_value = mock_response

    success = wallet_execution_engine.cancel_order("order-xyz")
    assert success is True


@patch("requests.post")
def test_sync_positions(mock_post, wallet_execution_engine):
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

    positions = wallet_execution_engine.sync_positions()

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
def test_sync_balance(mock_post, wallet_execution_engine):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "total_account_equity": 1045.25,
        "total_wallet_balance": 1000.00
    }
    mock_post.return_value = mock_response

    balance = wallet_execution_engine.sync_balance()
    assert balance == Decimal("1045.25")


def test_precision_rounding(wallet_execution_engine):
    # Test _get_precision mapping
    assert wallet_execution_engine._get_precision("BTCUSDT") == (3, 2)
    assert wallet_execution_engine._get_precision("ETHUSDT") == (2, 2)
    assert wallet_execution_engine._get_precision("SOLUSDT") == (2, 2)
    assert wallet_execution_engine._get_precision("UNKNOWN") == (2, 2)


@patch("requests.post")
def test_place_order_precision_rounding(mock_post, wallet_execution_engine, monkeypatch):
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
    wallet_execution_engine.place_order(
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
