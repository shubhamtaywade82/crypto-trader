"""Regression tests for data-staleness guards (A1/A2) and per-symbol risk
budget (B2). Pure/in-memory — no network, no WebSocket started."""
import time

from crypto_trader.ws_client import BinanceWebSocketFeed


def _feed():
    return BinanceWebSocketFeed(symbol="SOLUSDT")


# ── A2: is_connected grace window ─────────────────────────────────────────────
def test_is_connected_false_when_socket_down():
    f = _feed()
    f.data.is_connected = False
    assert f.is_connected() is False


def test_is_connected_grace_then_expires_without_data():
    f = _feed()
    now = int(time.time() * 1000)
    f.data.is_connected = True
    f.data.connected_at_ms = now          # just opened
    f.data.last_update_ms = 0             # no data yet
    assert f.is_connected() is True       # within grace window

    # Grace window elapsed, still no data -> NOT connected (prior bug: True forever)
    f.data.connected_at_ms = now - 30_000
    assert f.is_connected() is False


def test_is_connected_true_on_recent_data():
    f = _feed()
    now = int(time.time() * 1000)
    f.data.is_connected = True
    f.data.connected_at_ms = now - 60_000  # opened long ago
    f.data.last_update_ms = now - 1_000    # but data 1s ago
    assert f.is_connected() is True


# ── A1: is_fresh / data_age_ms ────────────────────────────────────────────────
def test_is_fresh_requires_recent_price_source():
    f = _feed()
    assert f.data_age_ms() is None          # nothing received
    assert f.is_fresh(15_000) is False

    now = int(time.time() * 1000)
    f.data.last_price_time_ms = now - 2_000
    assert f.is_fresh(15_000) is True
    assert f.is_fresh(1_000) is False       # older than 1s budget

    # A stale book/mark with an old last_price -> not fresh
    f.data.last_price_time_ms = now - 60_000
    f.data.book_ticker_time_ms = now - 60_000
    f.data.mark_price_time_ms = now - 60_000
    assert f.is_fresh(15_000) is False


# ── B2: per-symbol risk budget fraction ───────────────────────────────────────
def test_risk_budget_fraction_clamped():
    from crypto_trader.engine_ws import WebSocketTradingEngine
    e = WebSocketTradingEngine(symbol="SOLUSDT", use_llm=False, risk_budget_fraction=0.25)
    assert e.risk_budget_fraction == 0.25
    e2 = WebSocketTradingEngine(symbol="SOLUSDT", use_llm=False, risk_budget_fraction=5.0)
    assert e2.risk_budget_fraction == 1.0   # clamped to [0,1]
    e3 = WebSocketTradingEngine(symbol="SOLUSDT", use_llm=False, risk_budget_fraction=-1.0)
    assert e3.risk_budget_fraction == 0.0
