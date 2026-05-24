"""Unit tests for the CoinDCX execution adapter + signing (mocked HTTP)."""
import hashlib
import hmac
import json
from decimal import Decimal

import pytest

from crypto_trader.exchanges.coindcx_client import CoinDCXClient, CoinDCXError
from crypto_trader.exchanges.instrument_mapper import (
    InstrumentMapper,
    InstrumentSpec,
    coindcx_to_internal,
    internal_to_coindcx,
)
from crypto_trader.exchanges.coindcx_execution import CoinDCXExecutionEngine
from crypto_trader.wallet import OrderType, PositionSide


# ── symbol mapping ──────────────────────────────────────────────────────────
def test_symbol_mapping_roundtrip():
    assert internal_to_coindcx("SOLUSDT") == "B-SOL_USDT"
    assert internal_to_coindcx("BTCUSDT") == "B-BTC_USDT"
    assert coindcx_to_internal("B-SOL_USDT") == "SOLUSDT"
    # idempotent on already-coindcx symbols
    assert internal_to_coindcx("B-SOL_USDT") == "B-SOL_USDT"


def test_symbol_mapping_rejects_unknown():
    with pytest.raises(ValueError):
        internal_to_coindcx("SOLBUSD")


# ── signing ─────────────────────────────────────────────────────────────────
def test_hmac_signature_matches_reference():
    client = CoinDCXClient(api_key="k", api_secret="secret123")
    body = json.dumps({"a": 1, "timestamp": 42}, separators=(",", ":"))
    expected = hmac.new(b"secret123", body.encode(), hashlib.sha256).hexdigest()
    assert client._sign(body) == expected


def test_signed_headers_require_credentials():
    client = CoinDCXClient()  # no creds
    with pytest.raises(CoinDCXError):
        client._signed_headers("{}")


# ── instrument spec rounding / validation ────────────────────────────────────
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


# ── execution adapter with mocked client ─────────────────────────────────────
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
