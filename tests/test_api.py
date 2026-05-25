"""Dashboard API tests — endpoints + mode filter + SSE (no Postgres/Redis needed)."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from crypto_trader.config import TradingConfig
from crypto_trader.api.app import create_app


class FakeRepo:
    def __init__(self):
        self._positions = [
            {"symbol": "SOLUSDT", "mode": "live", "side": "LONG", "qty": 1.0, "avg_price": 100.0,
             "status": "OPEN", "last_event_ts": 1000},
            {"symbol": "BTCUSDT", "mode": "paper", "side": "SHORT", "qty": 0.1, "avg_price": 50000.0,
             "status": "OPEN", "last_event_ts": 1100},
        ]

    def positions(self, mode=None):
        return [p for p in self._positions if mode is None or p["mode"] == mode]

    def orders(self, mode=None, limit=50):
        return [{"exchange_order_id": "o1", "mode": "live", "symbol": "SOLUSDT", "side": "LONG",
                 "order_type": "MARKET", "qty": 1.0, "filled_qty": 1.0, "avg_fill_price": 100.0,
                 "status": "FILLED", "created_at": 1000}][:limit if mode in (None, "live") else 0]

    def fills(self, mode=None, limit=50):
        return [{"exchange_order_id": "o1", "symbol": "SOLUSDT", "side": "LONG", "mode": "live",
                 "price": 100.0, "qty": 1.0, "fee": 0.05, "ts": 1000}]

    def pnl(self, mode=None):
        return {"live": {"mode": "live", "fills": 1, "total_fees": 0.05, "total_qty": 1.0}}

    def health(self):
        return {"db": "ok", "last_event_ts": 1100, "last_event_age_ms": 5}


async def _fake_source():
    yield {"event_type": "POSITION_OPENED", "payload": {"symbol": "SOLUSDT", "side": "LONG"}}
    yield {"event_type": "POSITION_CLOSED", "payload": {"symbol": "SOLUSDT"}}


def _client(event_source=None):
    cfg = TradingConfig()  # no DATABASE_URL -> won't build a PostgresRepo
    app = create_app(repo=FakeRepo(), event_source=event_source, cfg=cfg)
    return TestClient(app)


def test_health():
    r = _client().get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["db"] == "ok"


def test_positions_all_and_mode_filter():
    c = _client()
    assert len(c.get("/api/positions").json()) == 2
    live = c.get("/api/positions?mode=live").json()
    assert len(live) == 1 and live[0]["symbol"] == "SOLUSDT"
    paper = c.get("/api/positions?mode=paper").json()
    assert len(paper) == 1 and paper[0]["mode"] == "paper"


def test_positions_rejects_bad_mode():
    assert _client().get("/api/positions?mode=bogus").status_code == 422


def test_pnl_and_fills_and_orders():
    c = _client()
    assert "live" in c.get("/api/pnl").json()
    assert c.get("/api/fills").json()[0]["fee"] == 0.05
    assert c.get("/api/orders").json()[0]["status"] == "FILLED"


def test_index_serves_ui_or_placeholder():
    # When ui/dist exists -> built HTML; otherwise a JSON placeholder listing endpoints.
    r = _client().get("/")
    assert r.status_code == 200
    ctype = r.headers.get("content-type", "")
    if "text/html" in ctype:
        assert "<div id=\"root\">" in r.text   # built SolidJS bundle
    else:
        assert "/api/stream" in r.json()["endpoints"]


def test_sse_stream_relays_injected_events():
    c = _client(event_source=lambda: _fake_source())
    with c.stream("GET", "/api/stream") as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        body = "".join(chunk for chunk in r.iter_text())
    assert "POSITION_OPENED" in body
    assert "POSITION_CLOSED" in body
    assert "SOLUSDT" in body
