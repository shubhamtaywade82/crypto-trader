"""Phase 1 — backfill closed-trade outcomes from the wallet event log."""
import json

from crypto_trader.analytics.backfill import rebuild_outcomes_from_events
from crypto_trader.journal import TradeOutcomeJournal


def _row(event_type, symbol, payload):
    # (ts, event_type, symbol, namespace, payload_json)
    return (payload.get("exit_ts", 0), event_type, symbol, "default", json.dumps(payload))


def _close_payload(**kw):
    base = {
        "symbol": "SOLUSDT", "side": "long", "exit_price": 155.0,
        "remaining_pnl": 42.5, "quantity": 2.0, "entry_price": 150.0,
        "reason": "tp_hit", "exit_reason": "tp_hit",
        "entry_ts": 1_000_000, "exit_ts": 1_000_000 + 90 * 60_000,
        "regime": "trend", "mfe": 0.03, "mae": -0.01,
    }
    base.update(kw)
    return base


def test_backfill_writes_one_record_per_close(tmp_path):
    rows = [
        _row("POSITION_OPENED", "SOLUSDT", {"entry_price": 150.0}),  # ignored
        _row("POSITION_CLOSED", "SOLUSDT", _close_payload()),
        _row("ORDER_FILLED", "SOLUSDT", {"fee": 0.1}),               # ignored
        _row("LIQUIDATION", "ETHUSDT", _close_payload(symbol="ETHUSDT", side="short")),
    ]
    sink = TradeOutcomeJournal(data_dir=tmp_path / "outcomes")
    n = rebuild_outcomes_from_events(rows, sink)
    assert n == 2
    recs = list(sink.load_all())
    assert {r.symbol for r in recs} == {"SOLUSDT", "ETHUSDT"}


def test_backfill_field_mapping(tmp_path):
    sink = TradeOutcomeJournal(data_dir=tmp_path / "outcomes")
    rebuild_outcomes_from_events([_row("POSITION_CLOSED", "SOLUSDT", _close_payload())], sink)
    r = list(sink.load_all())[0]
    assert r.entry_price == 150.0
    assert r.exit_price == 155.0
    assert r.realized_pnl == 42.5
    assert r.quantity == 2.0
    assert r.holding_time_s == 90 * 60.0
    assert r.regime == "trend"
    assert r.mfe == 0.03
    assert r.exit_reason == "tp_hit"


def test_backfill_tolerates_bad_rows(tmp_path):
    rows = [
        _row("POSITION_CLOSED", "SOLUSDT", _close_payload()),
        (0, "POSITION_CLOSED", "BTCUSDT", "default", "{not valid json"),  # bad payload
        (0, "POSITION_CLOSED", "BTCUSDT", "default", "{}"),               # empty payload skipped
    ]
    sink = TradeOutcomeJournal(data_dir=tmp_path / "outcomes")
    n = rebuild_outcomes_from_events(rows, sink)
    assert n == 1  # only the good row
