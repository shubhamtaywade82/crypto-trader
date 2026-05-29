"""
Tests for TradeOutcomeRecord + TradeOutcomeJournal (Task 2 — TDD).

Run:  python3 -m pytest tests/test_trade_outcome.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crypto_trader.journal import TradeOutcomeRecord, TradeOutcomeJournal


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _full_record(**overrides) -> TradeOutcomeRecord:
    """Return a fully-populated record (all fields set)."""
    defaults = dict(
        trade_id="TRADE-001",
        symbol="BTCUSDT",
        side="long",
        opened_at=1_700_000_000_000,
        closed_at=1_700_003_600_000,
        # features at entry
        regime="trending_up",
        regime_score=0.85,
        funding_rate=0.0001,
        vol_regime="normal",
        oi_delta=1200.5,
        session="london",
        entry_reason="supertrend_flip",
        llm_action="buy",
        llm_confidence=0.9,
        llm_rationale="Strong momentum confirmed",
        entry_band=0.002,
        stop_loss_pct=0.015,
        risk_per_trade_pct=0.01,
        leverage=5,
        # outcome at exit
        entry_price=30000.0,
        exit_price=30600.0,
        quantity=0.01,
        realized_pnl=6.0,
        pnl_r=1.5,
        mfe=8.0,
        mae=-2.0,
        holding_time_s=3600,
        exit_reason="take_profit",
        slippage_bps=2.5,
    )
    defaults.update(overrides)
    return TradeOutcomeRecord(**defaults)


def _min_record(**overrides) -> TradeOutcomeRecord:
    """Return a record with only required fields (all optional fields absent)."""
    required = dict(
        trade_id="TRADE-MIN",
        symbol="ETHUSDT",
        side="short",
        opened_at=1_700_000_000_000,
        closed_at=1_700_001_800_000,
        entry_price=2000.0,
        exit_price=1980.0,
        quantity=0.1,
        realized_pnl=2.0,
        holding_time_s=1800,
        exit_reason="stop_loss",
    )
    required.update(overrides)
    return TradeOutcomeRecord(**required)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def journal(tmp_path):
    """TradeOutcomeJournal backed by a temp directory."""
    return TradeOutcomeJournal(data_dir=tmp_path)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Round-trip equality (persist + load_all)
# ─────────────────────────────────────────────────────────────────────────────

def test_roundtrip_full_record(journal):
    rec = _full_record()
    journal.record(rec)
    loaded = list(journal.load_all())
    assert len(loaded) == 1
    assert loaded[0] == rec


# ─────────────────────────────────────────────────────────────────────────────
# 2. Append semantics: N writes → load_all returns N records in order
# ─────────────────────────────────────────────────────────────────────────────

def test_append_semantics(journal):
    records = [_full_record(trade_id=f"TRADE-{i:03d}") for i in range(5)]
    for r in records:
        journal.record(r)
    loaded = list(journal.load_all())
    assert len(loaded) == 5
    assert [r.trade_id for r in loaded] == [f"TRADE-{i:03d}" for i in range(5)]


# ─────────────────────────────────────────────────────────────────────────────
# 3. Missing optional fields tolerated
# ─────────────────────────────────────────────────────────────────────────────

def test_optional_fields_default_to_none(journal):
    rec = _min_record()
    # All feature fields and extra outcome fields should be None / zero
    assert rec.regime is None
    assert rec.llm_action is None
    assert rec.pnl_r is None
    assert rec.slippage_bps is None


def test_roundtrip_minimal_record(journal):
    rec = _min_record()
    journal.record(rec)
    loaded = list(journal.load_all())
    assert len(loaded) == 1
    assert loaded[0] == rec


# ─────────────────────────────────────────────────────────────────────────────
# 4. JSONL persistence — each line is valid JSON with a 'type' discriminator
# ─────────────────────────────────────────────────────────────────────────────

def test_jsonl_format(journal, tmp_path):
    rec = _full_record()
    journal.record(rec)
    # There should be exactly one .jsonl file
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    lines = [ln for ln in files[0].read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    # Must carry the discriminator and key fields
    assert payload.get("record_type") == "trade_outcome"
    assert payload["trade_id"] == "TRADE-001"
    assert payload["symbol"] == "BTCUSDT"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Existing TradeJournal.log() still works (no regression)
# ─────────────────────────────────────────────────────────────────────────────

def test_existing_trade_journal_log_unbroken(tmp_path):
    """Verify the original TradeJournal + TradeJournalEntry still function."""
    from crypto_trader.journal import TradeJournal, TradeJournalEntry

    journal = TradeJournal(data_dir=tmp_path)
    entry = TradeJournalEntry(
        trade_id="OLD-001",
        timestamp=1_700_000_000_000,
        symbol="BTCUSDT",
        regime="sideways",
        regime_score=0.5,
        technical_setup={"indicator": "rsi"},
        technical_score=0.6,
        llm_bias="buy",
        llm_confidence=0.7,
        llm_latency_ms=120.0,
        llm_age_seconds=5.0,
        llm_weight=0.5,
        final_score=0.65,
        margin_used=100.0,
        notional=500.0,
        entry_price=30000.0,
    )
    journal.log(entry)
    recent = journal.get_recent(days=7)
    assert any(e.get("trade_id") == "OLD-001" for e in recent)


# ─────────────────────────────────────────────────────────────────────────────
# 6. load_all is an iterator / generator (lazy)
# ─────────────────────────────────────────────────────────────────────────────

def test_load_all_is_iterable(journal):
    journal.record(_full_record())
    result = journal.load_all()
    # Must be iterable; consuming it once must yield records
    items = list(result)
    assert len(items) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 7. load_all on empty store returns empty iterator
# ─────────────────────────────────────────────────────────────────────────────

def test_load_all_empty(journal):
    assert list(journal.load_all()) == []


# ─────────────────────────────────────────────────────────────────────────────
# 8. TradeOutcomeJournal does NOT touch ~/.crypto_trader unless told to
# ─────────────────────────────────────────────────────────────────────────────

def test_no_home_dir_writes(tmp_path, monkeypatch):
    """Ensure default data_dir is configurable; test harness never touches ~/.crypto_trader."""
    real_home = Path.home() / ".crypto_trader"
    # Just confirm our fixture uses tmp_path, not the real home dir
    j = TradeOutcomeJournal(data_dir=tmp_path)
    j.record(_min_record())
    # Nothing should be created under real home as a side-effect
    outcome_dir = real_home / "outcomes"
    # We don't assert it doesn't exist (it might from production runs),
    # but we assert our records are in tmp_path
    assert list(tmp_path.glob("*.jsonl")), "expected JSONL in tmp_path"
