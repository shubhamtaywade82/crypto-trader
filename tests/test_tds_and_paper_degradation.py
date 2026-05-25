"""F2 (TDS accounting) + F4 (paper execution-degradation) tests."""
from decimal import Decimal
import uuid

from crypto_trader.wallet import DATA_DIR, EnhancedFuturesWallet, Playbook, PositionSide


def _setup(side=PositionSide.LONG, entry="100"):
    return {
        "entry_price": entry,
        "side": side,
        "playbook": Playbook.INTRADAY,
        "sl_price": "90" if side == PositionSide.LONG else "110",
        "tp_price": "110" if side == PositionSide.LONG else "90",
    }


def _fresh_wallet(symbol: str, **kwargs):
    ns = f"test_{uuid.uuid4().hex}"
    for pat in (f"wallet_{ns}_{symbol}*", f"wallet_events_{ns}_{symbol}*"):
        for p in DATA_DIR.glob(pat):
            p.unlink(missing_ok=True)
    return EnhancedFuturesWallet(symbol=symbol, state_namespace=ns, **kwargs)


def _events(w):
    import sqlite3
    with sqlite3.connect(w.db_file) as conn:
        return [r[0] for r in conn.execute("SELECT event_type FROM events").fetchall()]


# ── F2: TDS ──────────────────────────────────────────────────────────────────
def test_long_roundtrip_tds_reduces_pnl(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    # TDS off (baseline) vs on; same trade.
    base = _fresh_wallet("BTCUSDT", tds_enabled=False)
    base.open_position("BTCUSDT", _setup(), mark_price=100)
    base.close_position("BTCUSDT", mark_price=110, reason="T")
    pnl_no_tds = base.realized_pnl_total

    w = _fresh_wallet("BTCUSDT", tds_enabled=True, tds_rate=0.01)
    pos = w.open_position("BTCUSDT", _setup(), mark_price=100)
    qty = pos.remaining_quantity
    closed = w.close_position("BTCUSDT", mark_price=110, reason="T")
    # LONG sells at close: TDS = 1% * fill_price * qty, booked on the position.
    expected_tds = closed.close_price * qty * Decimal("0.01")
    assert abs(closed.tds_paid - expected_tds) < Decimal("1e-9")
    assert "TDS_CHARGED" in _events(w)
    # Net realized PnL is exactly TDS lower than the no-TDS run.
    assert abs((pnl_no_tds - w.realized_pnl_total) - expected_tds) < Decimal("1e-6")


def test_short_charges_tds_at_open_not_close(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = _fresh_wallet("ETHUSDT", tds_enabled=True, tds_rate=0.01)
    pos = w.open_position("ETHUSDT", _setup(side=PositionSide.SHORT), mark_price=100)
    qty = pos.remaining_quantity
    # SHORT sells at open: TDS already booked on open.
    expected_open_tds = pos.entry_price * qty * Decimal("0.01")
    assert abs(pos.tds_paid - expected_open_tds) < Decimal("1e-9")
    tds_before_close = pos.tds_paid
    closed = w.close_position("ETHUSDT", mark_price=95, reason="T")
    # No additional TDS at close for a SHORT (the close is a buy).
    assert closed.tds_paid == tds_before_close


def test_tds_disabled_is_regression_safe(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = _fresh_wallet("XRPUSDT", tds_enabled=False)
    pos = w.open_position("XRPUSDT", _setup(), mark_price=100)
    closed = w.close_position("XRPUSDT", mark_price=110, reason="T")
    assert pos.tds_paid == Decimal("0")
    assert closed.tds_paid == Decimal("0")
    assert "TDS_CHARGED" not in _events(w)


# ── F4: paper execution-degradation ──────────────────────────────────────────
def test_paper_spread_coeff_degrades_fill(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    coeff = 0.0015
    w = _fresh_wallet("SOLUSDT", paper_spread_coeff=coeff)
    pos = w.open_position("SOLUSDT", _setup(), mark_price=100)
    # LONG entry fills worse (higher) by exactly the spread coefficient.
    assert abs(pos.entry_price - Decimal("100") * (Decimal("1") + Decimal(str(coeff)))) < Decimal("1e-9")


def test_paper_collar_rejects_far_fill(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = _fresh_wallet("SOLUSDT", paper_collar_pct=0.10)
    # Intended entry 100 but mark is 120 (>10% away) -> REJECTED, no position.
    pos = w.open_position("SOLUSDT", _setup(entry="100"), mark_price=120)
    assert pos is None
    assert w.get_open_position("SOLUSDT") is None
    assert "ORDER_REJECTED" in _events(w)


def test_paper_collar_allows_near_fill(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = _fresh_wallet("SOLUSDT", paper_collar_pct=0.10)
    pos = w.open_position("SOLUSDT", _setup(entry="100"), mark_price=101)
    assert pos is not None
