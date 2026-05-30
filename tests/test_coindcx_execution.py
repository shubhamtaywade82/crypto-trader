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
                                         "avg_price": "100.5", "total_quantity": "1.0"}
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

    def post_signed(self, endpoint, payload=None, **kwargs):
        self.calls.append((endpoint, payload))
        if endpoint.endswith("orders/create"):
            return self.order_resp
        if endpoint.endswith("positions"):
            return self.positions
        return {}

    def get_signed(self, endpoint, payload=None, **kwargs):
        self.calls.append((endpoint, payload))
        if endpoint.endswith("cross_margin_details"):
            return {"total_account_equity": self.equity} if self.equity is not None else {}
        if endpoint.endswith("wallets"):
            return self.balances
        return {}


@pytest.fixture
def gate_open(monkeypatch):
    """Open the safe_mode live gate for the duration of a test."""
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_TRADING_ACK", safe_mode.ACK_PHRASE)
    monkeypatch.setenv("PLACE_ORDER", "true")  # not read-only for these tests
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
    order = eng.place_order("SOLUSDT", PositionSide.LONG, Decimal("1.0"), OrderType.MARKET)
    endpoint, payload = fake.calls[-1]
    assert endpoint.endswith("orders/create")
    o = payload["order"]
    assert o["pair"] == "B-SOL_USDT"
    assert o["side"] == "buy"
    assert o["order_type"] == "market_order"
    assert o["leverage"] == 2.0
    assert o["notification"] == "no_notification"
    assert o["position_margin_type"] == "isolated"
    assert o["margin_currency_short_name"] == "USDT"
    # CoinDCX create order supports neither reduce_only nor client_order_id
    assert "reduce_only" not in o
    assert "client_order_id" not in o
    assert order.status.value == "FILLED"
    assert order.avg_fill_price == Decimal("100.5")


def test_exit_uses_opposite_side_no_reduce_only(gate_open):
    fake = _FakeClient()
    eng = _engine(fake)
    # closing a LONG => opposite-side (sell) market order; nets the position
    eng.place_order("SOLUSDT", PositionSide.SHORT, Decimal("1.0"), OrderType.MARKET, reduce_only=True)
    _, payload = fake.calls[-1]
    assert payload["order"]["side"] == "sell"
    assert "reduce_only" not in payload["order"]


def test_place_order_blocked_without_gate(monkeypatch):
    # Gate closed (no env, no ack) -> safe_mode must block the order.
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.delenv("LIVE_TRADING_ACK", raising=False)
    fake = _FakeClient()
    eng = _engine(fake, ack=False)
    with pytest.raises(safe_mode.LiveTradingBlocked):
        eng.place_order("SOLUSDT", PositionSide.LONG, Decimal("1.0"), OrderType.MARKET)
    assert not any(ep.endswith("orders/create") for ep, _ in fake.calls)  # no order sent


def test_place_order_blocked_when_read_only(gate_open, monkeypatch):
    # Gate fully open but PLACE_ORDER=false -> read-only live mode blocks execution.
    monkeypatch.setenv("PLACE_ORDER", "false")
    fake = _FakeClient()
    eng = _engine(fake)
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


# ── safety regressions ───────────────────────────────────────────────────────
def test_order_create_does_not_retry_on_timeout(monkeypatch):
    """C1: a non-idempotent order create must NOT be retried on timeout — a
    duplicate could double the position (no client_order_id idempotency)."""
    import requests
    from crypto_trader.exchanges import coindcx_client as cc

    client = CoinDCXClient(api_key="k", api_secret="s", max_retries=3, backoff_base=0)
    attempts = {"n": 0}

    class _Sess:
        headers = {}
        def request(self, *a, **k):
            attempts["n"] += 1
            raise requests.Timeout("boom")
    client.session = _Sess()
    monkeypatch.setattr(cc.time, "sleep", lambda *_: None)

    with pytest.raises(CoinDCXError):
        client.post_signed("exchange/v1/derivatives/futures/orders/create",
                           {"order": {}}, retry_safe=False)
    assert attempts["n"] == 1  # exactly one attempt, no retry


def test_read_call_still_retries_on_timeout(monkeypatch):
    """Idempotent reads keep retrying (regression guard for retry_safe default)."""
    import requests
    from crypto_trader.exchanges import coindcx_client as cc

    client = CoinDCXClient(api_key="k", api_secret="s", max_retries=3, backoff_base=0)
    attempts = {"n": 0}

    class _Sess:
        headers = {}
        def request(self, *a, **k):
            attempts["n"] += 1
            raise requests.Timeout("boom")
    client.session = _Sess()
    monkeypatch.setattr(cc.time, "sleep", lambda *_: None)

    with pytest.raises(requests.Timeout):
        client.get_signed("exchange/v1/derivatives/futures/wallets", {})
    assert attempts["n"] == 3  # full retry budget for safe reads


def test_market_fill_unconfirmed_raises(gate_open, monkeypatch):
    """C2: if the venue never confirms the market fill, refuse to book a
    zero-price position — raise instead."""
    # create returns no avg price (async fill) and fills never resolve.
    fake = _FakeClient(order_resp={"id": "ex-9", "status": "open"})
    eng = _engine(fake)
    monkeypatch.setattr(eng, "get_fills", lambda *a, **k: [])
    monkeypatch.setattr(eng, "get_order_status",
                        lambda *a, **k: {"status": "open", "filled_quantity": 0.0})
    import crypto_trader.exchanges.coindcx_execution as ce
    monkeypatch.setattr(ce, "_as_list", ce._as_list)  # no-op guard
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda *_: None)
    with pytest.raises(CoinDCXError):
        eng.place_order("SOLUSDT", PositionSide.LONG, Decimal("1.0"), OrderType.MARKET)


# ── leverage cap sourced from dynamic tier table (not max_leverage_long) ──

class _TierClient:
    """Public-only client returning an instrument with the real dynamic tier
    table. max_leverage_long is the docs' 'ignore this' field (5x); the tiers
    permit far higher leverage for small positions."""
    def get_public(self, endpoint, params=None):
        return {"instrument": {
            "pair": params["pair"], "price_increment": 0.01, "quantity_increment": 0.01,
            "min_quantity": 0.01, "min_notional": 6.0,
            "max_leverage_long": 5.0, "max_leverage_short": 5.0,
            "dynamic_position_leverage_details": {
                "2": 52000000, "5": 50000001, "10": 50000000,
                "15": 20000000, "20": 15000000, "50": 1000000, "100": 10000,
            },
            "maker_fee": 0.0236, "taker_fee": 0.059, "status": "active",
        }}


def test_spec_max_leverage_from_dynamic_tiers_clamped_to_system_cap():
    spec = InstrumentMapper(_TierClient()).get_spec("SOLUSDT")
    # tier table allows up to 100x, clamped to system cap 20x — and crucially
    # NOT the bogus 5x max_leverage_long 'ignore this' field.
    assert spec.max_leverage == 20
    assert spec.max_leverage != 5


def test_spec_falls_back_to_legacy_when_no_tier_table():
    class _NoTier:
        def get_public(self, endpoint, params=None):
            return {"instrument": {
                "pair": params["pair"], "price_increment": 0.01, "quantity_increment": 0.01,
                "min_quantity": 0.01, "min_notional": 6.0, "max_leverage_long": 7.0,
                "maker_fee": 0.0236, "taker_fee": 0.059, "status": "active",
            }}
    spec = InstrumentMapper(_NoTier()).get_spec("BTCUSDT")
    assert spec.max_leverage == 7  # fell back to max_leverage_long (< system cap)


def test_spec_legacy_above_cap_is_clamped():
    class _BigLegacy:
        def get_public(self, endpoint, params=None):
            return {"instrument": {
                "pair": params["pair"], "price_increment": 0.01, "quantity_increment": 0.01,
                "min_quantity": 0.01, "min_notional": 6.0, "max_leverage_long": 50.0,
                "maker_fee": 0.0236, "taker_fee": 0.059, "status": "active",
            }}
    spec = InstrumentMapper(_BigLegacy()).get_spec("BTCUSDT")
    assert spec.max_leverage == 20  # clamped to system cap
