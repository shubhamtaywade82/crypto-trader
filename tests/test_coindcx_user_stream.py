"""F5 — CoinDCX private/user stream: auth signing, fill normalization, dedupe."""
import hashlib
import hmac
import json

from crypto_trader.events import EventBus, OrderFilledEvent
from crypto_trader.exchanges.coindcx_user_stream import CoinDCXUserStream


def _stream(bus=None, on_fill=None):
    return CoinDCXUserStream(api_key="k", api_secret="secret123", bus=bus,
                             channel="coindcx", on_fill=on_fill)


def test_auth_payload_signature_matches_reference():
    s = _stream()
    payload = s._auth_payload()
    # Auth body now includes timestamp to prevent replay attacks (M-10 fix).
    ts = payload["timestamp"]
    body = json.dumps({"channel": "coindcx", "timestamp": ts}, separators=(",", ":"))
    expected = hmac.new(b"secret123", body.encode(), hashlib.sha256).hexdigest()
    assert payload["authSignature"] == expected
    assert payload["apiKey"] == "k"
    assert payload["channelName"] == "coindcx"
    assert isinstance(ts, int) and ts > 0


def test_trade_event_publishes_normalized_order_filled():
    bus = EventBus()
    received = []
    bus.subscribe(OrderFilledEvent, lambda e: received.append(e))
    booked = []
    s = _stream(bus=bus, on_fill=lambda f: booked.append(f))

    s._on_trade({
        "pair": "B-SOL_USDT", "order_id": "ord-1", "price": "101.5",
        "quantity": "2", "fee_amount": "0.05", "side": "sell", "timestamp": 1000,
    })

    assert len(received) == 1
    ev = received[0]
    assert ev.symbol == "SOLUSDT"
    assert ev.exchange_order_id == "ord-1"
    assert ev.fill_price == 101.5
    assert ev.fill_quantity == 2.0
    assert len(booked) == 1
    assert booked[0]["symbol"] == "SOLUSDT"


def test_trade_event_deduplicated():
    bus = EventBus()
    received = []
    bus.subscribe(OrderFilledEvent, lambda e: received.append(e))
    s = _stream(bus=bus)
    fill = {"pair": "B-SOL_USDT", "order_id": "ord-9", "price": "100",
            "quantity": "1", "side": "buy", "timestamp": 2000}
    s._on_trade(dict(fill))
    s._on_trade(dict(fill))   # identical (order_id, timestamp) -> ignored
    assert len(received) == 1


def test_trade_event_accepts_json_string():
    bus = EventBus()
    received = []
    bus.subscribe(OrderFilledEvent, lambda e: received.append(e))
    s = _stream(bus=bus)
    import json as _j
    s._on_trade(_j.dumps({"pair": "B-BTC_USDT", "order_id": "x", "price": "5",
                          "quantity": "1", "side": "sell", "timestamp": 3000}))
    assert len(received) == 1
    assert received[0].symbol == "BTCUSDT"


def test_start_without_socketio_returns_false(monkeypatch):
    # Simulate python-socketio not being installed: start() must degrade to REST.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "socketio":
            raise ImportError("no socketio")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    s = _stream()
    assert s.start() is False


def test_balance_update_parses_list_correctly():
    balances = []
    s = CoinDCXUserStream(
        api_key="k", api_secret="secret123",
        on_balance=lambda b: balances.append(b)
    )
    
    # Payload matches the CoinDCX community balance update response shape
    payload = [
       {
          "id":"026ef0f2-b5d8-11ee-b182-570ad79469a2",
          "balance":"1.0221449",
          "locked_balance":"0.99478995",
          "currency_id":"c19c38d1-3ebb-47ab-9207-62d043be7447",
          "currency_short_name":"USDT"
       }
    ]
    
    s._on_balance_update(payload)
    
    assert len(balances) == 1
    assert balances[0]["currency_short_name"] == "USDT"
    assert float(balances[0]["balance"]) == 1.0221449


# ── live-safety hardening (ping keepalive + position-update routing) ──

def test_ping_loop_emits_when_connected():
    import time as _t
    s = _stream()
    s.ping_interval_s = 0.01
    s._running = True
    s._connected = True
    sent = []
    class _Sio:
        def emit(self, ev, data=None):
            sent.append(ev)
    s.sio = _Sio()
    import threading
    th = threading.Thread(target=s._ping_loop, daemon=True)
    th.start()
    _t.sleep(0.05)
    s._running = False
    th.join(timeout=1)
    assert "ping" in sent


def test_ping_loop_silent_when_disconnected():
    import time as _t, threading
    s = _stream()
    s.ping_interval_s = 0.01
    s._running = True
    s._connected = False   # not connected → no ping
    sent = []
    class _Sio:
        def emit(self, ev, data=None):
            sent.append(ev)
    s.sio = _Sio()
    th = threading.Thread(target=s._ping_loop, daemon=True)
    th.start()
    _t.sleep(0.05)
    s._running = False
    th.join(timeout=1)
    assert sent == []


def test_on_position_callback_invoked():
    got = {}
    s = _stream()
    s.on_position = lambda d: got.update(d)
    s._on_position_update({"pair": "B-SOL_USDT", "active_pos": 0.0})
    assert got.get("pair") == "B-SOL_USDT"
