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


# ─────────────────────────────────────────────────────────────────────────────
# /api/metrics + /api/equity (FIX B) — file-backed outcome substrate
# ─────────────────────────────────────────────────────────────────────────────

from crypto_trader.journal import TradeOutcomeJournal, TradeOutcomeRecord


def _seed_outcomes(monkeypatch, tmp_path):
    """Point the outcome journal at tmp_path and write a known record set."""
    monkeypatch.setattr("crypto_trader.journal._OUTCOME_DATA_DIR", tmp_path)
    j = TradeOutcomeJournal()  # picks up patched _OUTCOME_DATA_DIR

    def rec(tid, pnl, t, pnl_r, regime="trend"):
        return TradeOutcomeRecord(
            trade_id=tid, symbol="SOLUSDT", side="long",
            opened_at=0, closed_at=t, entry_price=100.0, exit_price=100.0 + pnl,
            quantity=1.0, realized_pnl=pnl, holding_time_s=60.0, exit_reason="tp",
            regime=regime, pnl_r=pnl_r,
        )

    # cum: 10, 6, 12, -8 -> max drawdown 20 ; 2 wins (10,6) / 2 losses (-4,-20) -> win_rate 0.5
    for r in [rec("a", 10, 1, 2.0), rec("b", -4, 2, -1.0),
              rec("c", 6, 3, 1.0), rec("d", -20, 4, -5.0, regime="chop")]:
        j.record(r)


def test_metrics_endpoint_values(monkeypatch, tmp_path):
    _seed_outcomes(monkeypatch, tmp_path)
    m = _client().get("/api/metrics").json()
    assert m["total_trades"] == 4
    assert m["win_rate"] == 0.5            # 2 wins (10, 6) / 4
    assert m["max_drawdown"] == 20.0
    assert m["profit_factor"] == round(16 / 24, 4)  # gross win 16 / gross loss 24
    assert "per_regime" in m and "trend" in m["per_regime"]


def test_metrics_endpoint_empty_is_200(monkeypatch, tmp_path):
    monkeypatch.setattr("crypto_trader.journal._OUTCOME_DATA_DIR", tmp_path)
    r = _client().get("/api/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["total_trades"] == 0
    assert body["profit_factor"] is None


def test_equity_endpoint_curve(monkeypatch, tmp_path):
    _seed_outcomes(monkeypatch, tmp_path)
    curve = _client().get("/api/equity").json()
    assert [p["ts"] for p in curve] == [1, 2, 3, 4]
    assert [p["equity"] for p in curve] == [10.0, 6.0, 12.0, -8.0]


def test_equity_endpoint_empty(monkeypatch, tmp_path):
    monkeypatch.setattr("crypto_trader.journal._OUTCOME_DATA_DIR", tmp_path)
    r = _client().get("/api/equity")
    assert r.status_code == 200
    assert r.json() == []


def _auth_client():
    """Client whose app requires a bearer token (API_TOKEN set)."""
    monkey_token = "secret-token"
    import crypto_trader.api.app as appmod
    appmod.API_TOKEN = monkey_token
    cfg = TradingConfig()
    app = create_app(repo=FakeRepo(), cfg=cfg)
    return TestClient(app), monkey_token


def test_metrics_and_equity_reject_unauthenticated():
    import crypto_trader.api.app as appmod
    orig = appmod.API_TOKEN
    try:
        c, token = _auth_client()
        # No bearer header -> rejected like every other dashboard endpoint.
        assert c.get("/api/metrics").status_code == 401
        assert c.get("/api/equity").status_code == 401
        # Wrong token -> rejected.
        assert c.get("/api/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401
        # Correct token -> allowed.
        assert c.get("/api/metrics", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        assert c.get("/api/equity", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    finally:
        appmod.API_TOKEN = orig
