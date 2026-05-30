"""
Regression: per-symbol reconcile must not flag a live position in OTHER symbols.

Bug: account_sync.snapshot() returns ALL venue positions; preflight loops each
watchlist symbol against a shared GLOBAL wallet. During (say) the BTCUSDT pass,
a live SOLUSDT venue position was flagged 'missing_position' and tripped a false
kill switch. Fix: _compare scopes both internal and venue dicts to the target
symbol.
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from crypto_trader.execution.reconciler import Reconciler
from crypto_trader.execution.account_sync import AccountSnapshot


def _reconciler(internal_open: dict, wallet=None, adopt_venue=False):
    """Build a Reconciler with _internal_positions stubbed to internal_open."""
    r = Reconciler.__new__(Reconciler)
    r.wallet = wallet if wallet is not None else SimpleNamespace(positions={})
    r.account_sync = SimpleNamespace(engine=SimpleNamespace())
    r.risk = None
    r.bus = None
    r.qty_tolerance = Decimal("0.05")
    r.strict_cancel = False
    r.adopt_venue = adopt_venue
    r._internal_positions = lambda: internal_open
    return r


def _snap(venue_positions: dict):
    return AccountSnapshot(ok=True, positions=venue_positions, open_orders=[], fills=[])


def test_other_symbol_live_position_not_flagged_on_btc_pass():
    """Venue holds SOL; reconciling BTCUSDT must produce NO mismatch."""
    r = _reconciler(internal_open={})  # internal flat
    snap = _snap({"SOLUSDT": {"symbol": "SOLUSDT", "quantity": 3.02, "side": "LONG"}})
    mismatches = r._compare(snap, symbol="BTCUSDT")
    assert mismatches == []


def test_target_symbol_missing_position_still_flagged():
    """If the TARGET symbol's position is live on venue but absent internally,
    it must still be flagged (the real safety case is preserved)."""
    r = _reconciler(internal_open={})
    snap = _snap({"SOLUSDT": {"symbol": "SOLUSDT", "quantity": 3.02, "side": "LONG"}})
    mismatches = r._compare(snap, symbol="SOLUSDT")
    kinds = {m.kind for m in mismatches}
    assert "missing_position" in kinds


def test_ghost_position_scoped_to_symbol():
    """Internal-open BTC with no venue position is a ghost only on the BTC pass."""
    r = _reconciler(internal_open={
        "BTCUSDT": {"quantity": Decimal("0.01"), "side": "long", "entry_price": 60000},
    })
    snap = _snap({})  # venue flat
    # SOL pass: BTC ghost must NOT surface
    assert r._compare(snap, symbol="SOLUSDT") == []
    # BTC pass: ghost surfaces
    kinds = {m.kind for m in r._compare(snap, symbol="BTCUSDT")}
    assert "ghost_position" in kinds


def test_none_symbol_compares_whole_account():
    """symbol=None keeps the original whole-account behavior."""
    r = _reconciler(internal_open={})
    snap = _snap({"SOLUSDT": {"symbol": "SOLUSDT", "quantity": 3.02, "side": "LONG"}})
    mismatches = r._compare(snap, symbol=None)
    assert any(m.kind == "missing_position" for m in mismatches)


# ── adoption (RECONCILE_ADOPT_VENUE) ─────────────────────────────────────────

def test_missing_position_halts_when_adopt_disabled():
    """Default: missing_position is unrepaired → would trip the kill switch."""
    r = _reconciler(internal_open={}, adopt_venue=False)
    snap = _snap({"SOLUSDT": {"symbol": "SOLUSDT", "quantity": 3.02,
                              "side": "LONG", "entry_price": 82.2}})
    out = r._compare(snap, symbol="SOLUSDT")
    mp = [m for m in out if m.kind == "missing_position"]
    assert mp and mp[0].repaired is False


def test_missing_position_adopted_when_flag_on():
    """adopt_venue=True: the venue position is adopted and the mismatch is
    marked repaired (no kill switch)."""
    adopted = {}

    def fake_adopt(symbol, side, quantity, entry_price, leverage=None):
        adopted.update(symbol=symbol, side=side, quantity=quantity,
                       entry_price=entry_price)
        return object()  # non-None = success

    wallet = SimpleNamespace(positions={}, adopt_venue_position=fake_adopt)
    r = _reconciler(internal_open={}, wallet=wallet, adopt_venue=True)
    snap = _snap({"SOLUSDT": {"symbol": "SOLUSDT", "quantity": 3.02,
                              "side": "LONG", "entry_price": 82.2}})
    out = r._compare(snap, symbol="SOLUSDT")
    mp = [m for m in out if m.kind == "missing_position"]
    assert mp and mp[0].repaired is True
    assert adopted["symbol"] == "SOLUSDT"
    assert adopted["quantity"] == 3.02
    assert adopted["entry_price"] == 82.2


def test_adoption_failure_stays_unrepaired():
    """If the wallet's adoption returns None, the mismatch stays unrepaired
    (still halts) — adoption must not silently swallow a real desync."""
    wallet = SimpleNamespace(positions={}, adopt_venue_position=lambda **k: None)
    r = _reconciler(internal_open={}, wallet=wallet, adopt_venue=True)
    snap = _snap({"SOLUSDT": {"symbol": "SOLUSDT", "quantity": 3.02,
                              "side": "LONG", "entry_price": 82.2}})
    out = r._compare(snap, symbol="SOLUSDT")
    mp = [m for m in out if m.kind == "missing_position"]
    assert mp and mp[0].repaired is False
