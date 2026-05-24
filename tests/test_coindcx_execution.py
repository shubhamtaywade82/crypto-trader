"""Unit tests for the canonical CoinDCX execution adapter + signing (mocked HTTP)."""
import hashlib
import hmac
import json
from decimal import Decimal

import pytest

from crypto_trader import safe_mode
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
    client = CoinDCXClient()
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
    assert "min_notional" in s.validate_order(Decimal("0.01"), Decimal("100"))
    assert s.validate_order(Decimal("0.1"), Decimal("100")) is None
    assert "min_quantity" in s.validate_order(Decimal("0.001"), Decimal("100"))


# ── execution adapter with mocked client ─────────────────────────────────────
class _FakeClient:
    def __init__(self, order_resp=None, positions=None, balances=None, equity=None):
        self.order_resp = order_resp or {"id": "ex-1", "status": "filled",
                                         "avg_price": "100.5", "filled_quantity": "1.0"}
        self.positions = positions if positions is not None else []
        self.balances = balances if balances is not None else [{"currency_short_name": "USDT", "balance": 250.0}]
        self.equity = equity
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
        if endpoint.endswith("cross_margin_details"):
            return {"total_account_equity": self.equity} if self.equity is not None else {}
        if endpoint.endswith("balances"):
            return self.balances
        return {}


@pytest.fixture
def gate_open(monkeypatch):
    """Open the safe_mode live gate for the duration of a test."""
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_TRADING_ACK", safe_mode.ACK_PHRASE)
    existed = safe_mode.HALT_FILE.exists()
    if existed:
        safe_mode.HALT_FILE.unlink()
    yield


def _engine(fake, ack=True):
    return CoinDCXExecutionEngine(client=fake, mapper=InstrumentMapper(fake), leverage=2,
                                 i_understand_real_money=ack)


def test_place_order_builds_payload_and_parses_fill(gate_open):
    fake = _FakeClient()
    eng = _engine(fake)
    order = eng.place_order("SOLUSDT", PositionSide.LONG, Decimal("1.0"), OrderType.MARKET,
                            client_order_id="ct-test")
    endpoint, payload = fake.calls[-1]
    assert endpoint.endswith("orders/create")
    o = payload["order"]
    assert o["pair"] == "B-SOL_USDT"
    assert o["side"] == "buy"
    assert o["order_type"] == "market_order"
    assert o["leverage"] == 2.0
    assert o["client_order_id"] == "ct-test"
    assert order.status.value == "FILLED"
    assert order.avg_fill_price == Decimal("100.5")


def test_reduce_only_exit_payload(gate_open):
    fake = _FakeClient()
    eng = _engine(fake)
    eng.place_order("SOLUSDT", PositionSide.SHORT, Decimal("1.0"), OrderType.MARKET, reduce_only=True)
    _, payload = fake.calls[-1]
    assert payload["order"]["reduce_only"] is True
    assert payload["order"]["side"] == "sell"


def test_place_order_blocked_without_gate(monkeypatch):
    # Gate closed (no env, no ack) -> safe_mode must block the order.
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.delenv("LIVE_TRADING_ACK", raising=False)
    fake = _FakeClient()
    eng = _engine(fake, ack=False)
    with pytest.raises(safe_mode.LiveTradingBlocked):
        eng.place_order("SOLUSDT", PositionSide.LONG, Decimal("1.0"), OrderType.MARKET)
    assert not any(ep.endswith("orders/create") for ep, _ in fake.calls)  # no order sent


def test_get_balances_and_positions_normalize():
    fake = _FakeClient(positions=[{"pair": "B-SOL_USDT", "active_pos": 2.0, "avg_price": 99.0, "leverage": 2}])
    eng = _engine(fake)
    assert eng.get_balances()["USDT"] == 250.0
    syncs = eng.sync_positions()
    assert "SOLUSDT" in syncs
    assert syncs["SOLUSDT"]["quantity"] == 2.0


def test_sync_balance_prefers_cross_margin_equity():
    fake = _FakeClient(equity=1045.25)
    eng = _engine(fake)
    assert eng.sync_balance() == 1045.25


def test_sync_balance_falls_back_to_unified_usdt():
    fake = _FakeClient(equity=None, balances=[{"currency_short_name": "USDT", "balance": 77.0}])
    eng = _engine(fake)
    assert eng.sync_balance() == 77.0
