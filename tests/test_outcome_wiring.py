"""
Phase 1 — Trade-outcome substrate wiring.

The substrate (TradeOutcomeRecord/Journal + TradeJournal.log_close) existed but
was wired to nothing. WebSocketTradingEngine._persist_trade_outcome now joins
the cached open-side context with the TradeClosedEvent to emit BOTH a log_close
JSONL record (so analyze_llm_contribution sees closed trades) and a unified
TradeOutcomeRecord.

We exercise the method against a lightweight shim so we don't boot the whole
engine (data feeds, wallet, WS threads).
"""
from types import SimpleNamespace

import pytest

from crypto_trader.engine_ws import WebSocketTradingEngine
from crypto_trader.journal import TradeJournal, TradeOutcomeJournal
from crypto_trader.events import TradeClosedEvent


def _make_shim(tmp_path, open_features):
    """A minimal stand-in carrying only what _persist_trade_outcome touches."""
    return SimpleNamespace(
        symbol="SOLUSDT",
        journal=TradeJournal(data_dir=tmp_path / "journal"),
        outcome_journal=TradeOutcomeJournal(data_dir=tmp_path / "outcomes"),
        _open_features=open_features,
    )


def _close_event():
    return TradeClosedEvent(
        symbol="SOLUSDT", side="long", realized_pnl=42.5, pnl_percent=0.021,
        exit_price=155.0, reason="tp_hit", duration_minutes=90.0,
        mfe=0.03, mae=-0.01, fees=0.2, slippage=0.05, tds=0.1,
    )


def _full_open_ctx():
    return {
        "SOLUSDT": {
            "trade_id": "abc123",
            "side": "long",
            "opened_at": 1_000_000,
            "entry_price": 150.0,
            "quantity": 2.0,
            "stop_loss_price": 145.0,   # initial_risk = |150-145|*2 = 10
            "regime": "trend_expansion",
            "regime_score": 0.8,
            "vol_regime": "TREND_EXPANSION",
            "funding_rate": 0.0001,
            "oi_delta": 0.05,
            "session": "NY",
            "entry_reason": "supertrend2",
            "llm_action": "bullish",
            "llm_confidence": 0.72,
            "llm_rationale": "BOS up; bullish OB retest",
            "entry_band": 0.015,
            "stop_loss_pct": 0.015,
            "risk_per_trade_pct": 0.005,
            "leverage": 10,
        }
    }


def test_outcome_record_persisted_with_full_join(tmp_path):
    shim = _make_shim(tmp_path, _full_open_ctx())
    WebSocketTradingEngine._persist_trade_outcome(shim, _close_event())

    records = list(shim.outcome_journal.load_all())
    assert len(records) == 1
    r = records[0]
    assert r.trade_id == "abc123"
    assert r.symbol == "SOLUSDT"
    assert r.side == "long"
    assert r.entry_price == 150.0
    assert r.exit_price == 155.0
    assert r.quantity == 2.0
    assert r.realized_pnl == pytest.approx(42.5)
    assert r.holding_time_s == pytest.approx(90.0 * 60.0)
    assert r.exit_reason == "tp_hit"
    assert r.regime == "trend_expansion"
    assert r.llm_confidence == pytest.approx(0.72)
    assert r.mfe == pytest.approx(0.03)
    assert r.mae == pytest.approx(-0.01)
    # newly-threaded entry features
    assert r.vol_regime == "TREND_EXPANSION"
    assert r.session == "NY"
    assert r.entry_band == pytest.approx(0.015)
    assert r.llm_rationale == "BOS up; bullish OB retest"
    assert r.entry_reason == "supertrend2"
    # pnl_r = realized_pnl / (|entry - stop| * qty) = 42.5 / 10.0 = 4.25
    assert r.pnl_r == pytest.approx(4.25)
    # open context consumed (popped) so a stale join can't reattach
    assert "SOLUSDT" not in shim._open_features


def test_pnl_r_none_without_stop_loss(tmp_path):
    """pnl_r must be None (not a crash, not a bogus 0) when the open-side stop
    wasn't captured — initial_risk is unknown."""
    ctx = _full_open_ctx()
    ctx["SOLUSDT"].pop("stop_loss_price")
    shim = _make_shim(tmp_path, ctx)
    WebSocketTradingEngine._persist_trade_outcome(shim, _close_event())

    r = list(shim.outcome_journal.load_all())[0]
    assert r.pnl_r is None


def test_log_close_enables_llm_attribution(tmp_path):
    """After wiring, a logged open + close must make analyze_llm_contribution
    report a closed trade (previously starved because log_close was never called)."""
    open_ctx = {
        "SOLUSDT": {
            "trade_id": "xyz789", "side": "long", "opened_at": 1_000_000,
            "entry_price": 150.0, "quantity": 1.0, "regime": "trend",
            "llm_action": "bullish", "llm_confidence": 0.6,
        }
    }
    shim = _make_shim(tmp_path, open_ctx)
    # Seed a matching open record so the close has something to join to.
    shim.journal.log_open(
        trade_id="xyz789", symbol="SOLUSDT", regime="trend", regime_score=0.7,
        setup={"side": "long"}, technical_score=0.65,
        llm_advice={"bias": "bullish", "confidence": 0.6}, llm_weight=0.2,
        final_score=0.7, margin=15.0, notional=150.0, entry_price=150.0,
        funding_rate=0.0, oi_delta=0.0, taker_ratio=0.5,
    )

    WebSocketTradingEngine._persist_trade_outcome(shim, _close_event())

    stats = shim.journal.analyze_llm_contribution(days=30)
    assert stats["total_trades"] == 1
    assert stats["with_llm_count"] == 1  # llm_weight > 0


def test_missing_open_context_still_records(tmp_path):
    """If open context was lost (e.g. restart between open and close), still emit
    a record using close-event fallbacks rather than dropping the trade."""
    shim = _make_shim(tmp_path, {})  # no cached open
    WebSocketTradingEngine._persist_trade_outcome(shim, _close_event())

    records = list(shim.outcome_journal.load_all())
    assert len(records) == 1
    r = records[0]
    assert r.symbol == "SOLUSDT"
    assert r.side == "long"          # fell back to event.side
    assert r.realized_pnl == pytest.approx(42.5)
    assert r.trade_id.startswith("SOLUSDT-")
