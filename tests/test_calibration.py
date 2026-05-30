"""Phase 3 — offline human-gated calibration (proposer, walkforward, cli)."""
import json

import pytest

from crypto_trader.calibration import proposer, walkforward
from crypto_trader.calibration import cli
from crypto_trader.calibration.proposer import _clamp_step, DEFAULT_BOUNDS
from crypto_trader.journal import TradeOutcomeRecord, TradeOutcomeJournal


def _rec(pnl, t=0, regime="trend", pnl_r=None, reason="supertrend2", mae=None):
    return TradeOutcomeRecord(
        trade_id=f"t{t}", symbol="SOLUSDT", side="long",
        opened_at=t, closed_at=t, entry_price=100.0, exit_price=100.0 + pnl,
        quantity=1.0, realized_pnl=pnl, holding_time_s=60.0, exit_reason="x",
        regime=regime, pnl_r=pnl_r, entry_reason=reason, mae=mae,
    )


# ── bounds ───────────────────────────────────────────────────────────────────

def test_clamp_step_respects_max_step():
    # current 0.005, target 0.02, step 0.0025 → clamp to 0.0075
    assert _clamp_step(0.005, 0.02, (0.0025, 0.02, 0.0025)) == 0.0075


def test_clamp_step_respects_floor():
    assert _clamp_step(0.005, 0.0, (0.0025, 0.02, 0.0025)) == 0.0025


# ── proposer ─────────────────────────────────────────────────────────────────

def test_no_proposals_on_small_sample():
    recs = [_rec(10, t=i, pnl_r=1.0) for i in range(5)]
    assert proposer.propose(recs, {"risk_per_trade_pct": 0.005}) == []


def test_regime_multiplier_proposed_for_strong_regime():
    # 12 strongly-positive 'trend' trades → push high multiplier up
    recs = [_rec(10, t=i, regime="trend", pnl_r=2.0) for i in range(12)]
    out = proposer.propose(recs, {
        "regime_size_multiplier_high": 1.25, "regime_size_multiplier_low": 0.5,
    })
    names = {p.param for p in out}
    assert "regime_size_multiplier_high" in names
    hi = next(p for p in out if p.param == "regime_size_multiplier_high")
    assert hi.proposed > 1.25
    assert hi.proposed <= 1.5  # bounded


def test_worst_regime_lowers_low_multiplier():
    recs = ([_rec(10, t=i, regime="trend", pnl_r=2.0) for i in range(12)]
            + [_rec(-10, t=100 + i, regime="chop", pnl_r=-1.5) for i in range(12)])
    out = proposer.propose(recs, {
        "regime_size_multiplier_high": 1.25, "regime_size_multiplier_low": 0.5,
    })
    lo = next((p for p in out if p.param == "regime_size_multiplier_low"), None)
    assert lo is not None and lo.proposed < 0.5


def test_proposal_is_bounded_and_carries_rationale():
    recs = [_rec(10, t=i, regime="trend", pnl_r=2.0) for i in range(15)]
    out = proposer.propose(recs, {"regime_size_multiplier_high": 1.45})
    hi = next(p for p in out if p.param == "regime_size_multiplier_high")
    assert hi.proposed <= 1.5            # hard max
    assert hi.rationale and hi.n_trades >= 10


# ── walkforward ──────────────────────────────────────────────────────────────

def test_walkforward_improve_when_edge_persists():
    recs = [_rec(10, t=i, pnl_r=1.5) for i in range(40)]  # consistent winners
    v = walkforward.validate(recs, min_test_trades=5)
    assert v["verdict"] == "improve"


def test_walkforward_reject_when_edge_collapses():
    recs = ([_rec(10, t=i, pnl_r=2.0) for i in range(28)]            # good train
            + [_rec(-10, t=100 + i, pnl_r=-2.0) for i in range(12)])  # bad test
    v = walkforward.validate(recs, min_test_trades=5)
    assert v["verdict"] == "reject"


def test_walkforward_insufficient_data():
    recs = [_rec(10, t=i, pnl_r=1.0) for i in range(4)]
    v = walkforward.validate(recs, min_test_trades=5)
    assert v["verdict"] == "insufficient_data"


# ── cli ──────────────────────────────────────────────────────────────────────

def test_cli_propose_writes_artifact(tmp_path, monkeypatch):
    # seed an outcome journal the cli will read
    oj = TradeOutcomeJournal(data_dir=tmp_path / "outcomes")
    for i in range(15):
        oj.record(_rec(10, t=int(__import__("time").time() * 1000), regime="trend", pnl_r=2.0))
    monkeypatch.setattr(cli, "TradeOutcomeJournal", lambda: oj)

    rc = cli.main(["propose", "--days", "365", "--out", str(tmp_path / "cal")])
    assert rc == 0
    files = list((tmp_path / "cal").glob("proposal-*.json"))
    assert len(files) == 1
    art = json.loads(files[0].read_text())
    assert art["status"] == "pending_approval"
    assert "validation" in art


def test_cli_approve_writes_whitelisted_overrides(tmp_path, monkeypatch):
    proposal = {
        "proposals": [
            {"param": "regime_size_multiplier_high", "current": 1.25, "proposed": 1.35},
            {"param": "strategy_mode", "current": "x", "proposed": "y"},  # not whitelisted
        ],
        "validation": {"verdict": "improve"},
    }
    pfile = tmp_path / "proposal-x.json"
    pfile.write_text(json.dumps(proposal))
    ov_path = tmp_path / "runtime_overrides.json"
    monkeypatch.setattr(cli, "_OVERRIDES_PATH", ov_path)

    rc = cli.main(["approve", str(pfile), "--yes"])
    assert rc == 0
    written = json.loads(ov_path.read_text())
    assert written["overrides"] == {"regime_size_multiplier_high": 1.35}
    assert "strategy_mode" not in written["overrides"]  # restart key never approved
    assert written["status"] == "approved"


def test_cli_approve_requires_yes(tmp_path, monkeypatch):
    proposal = {"proposals": [{"param": "mr_entry_band", "current": 0.015, "proposed": 0.0175}],
                "validation": {"verdict": "improve"}}
    pfile = tmp_path / "p.json"
    pfile.write_text(json.dumps(proposal))
    ov_path = tmp_path / "runtime_overrides.json"
    monkeypatch.setattr(cli, "_OVERRIDES_PATH", ov_path)

    rc = cli.main(["approve", str(pfile)])  # no --yes
    assert rc == 1
    assert not ov_path.exists()


def test_cli_revert_removes_overrides(tmp_path, monkeypatch):
    ov_path = tmp_path / "runtime_overrides.json"
    ov_path.write_text("{}")
    monkeypatch.setattr(cli, "_OVERRIDES_PATH", ov_path)
    rc = cli.main(["revert"])
    assert rc == 0
    assert not ov_path.exists()
