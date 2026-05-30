"""
Phase 2 — config hot-reload.

Covers ConfigStore poll-on-change + classification, the apply_overrides hooks on
RiskManager / AdaptiveThresholdManager / playbooks, and the engine's
_apply_runtime_overrides routing (SAFE now, FLAT_ONLY deferred until flat,
RESTART ignored), exercised against a lightweight shim.
"""
import json
import time
from types import SimpleNamespace

import pytest

from crypto_trader.config_store import ConfigStore, SAFE_KEYS, FLAT_ONLY_KEYS, RESTART_KEYS
from crypto_trader.risk import RiskManager, AdaptiveThresholdManager
from crypto_trader.strategies.supertrend2 import PlaybookSupertrend2
from crypto_trader.strategies.mean_reversion import PlaybookMeanReversion
from crypto_trader.engine_ws import WebSocketTradingEngine


def _write(path, data):
    path.write_text(json.dumps(data))
    # ensure a distinct mtime from any prior write
    import os
    t = time.time() + 1
    os.utime(path, (t, t))


# ── ConfigStore ──────────────────────────────────────────────────────────────

def test_poll_returns_none_when_no_file(tmp_path):
    cs = ConfigStore(path=tmp_path / "missing.json")
    assert cs.poll() is None


def test_poll_returns_dict_then_none_until_change(tmp_path):
    p = tmp_path / "ov.json"
    _write(p, {"final_score_threshold": 0.9})
    cs = ConfigStore(path=p)
    first = cs.poll()
    assert first == {"final_score_threshold": 0.9}
    assert cs.poll() is None  # no change → None
    _write(p, {"final_score_threshold": 0.8})
    assert cs.poll() == {"final_score_threshold": 0.8}


def test_poll_supports_overrides_wrapper(tmp_path):
    p = tmp_path / "ov.json"
    _write(p, {"overrides": {"mr_entry_band": 0.02}, "status": "approved"})
    cs = ConfigStore(path=p)
    assert cs.poll() == {"mr_entry_band": 0.02}


def test_poll_keeps_last_good_on_corrupt(tmp_path):
    p = tmp_path / "ov.json"
    _write(p, {"final_score_threshold": 0.9})
    cs = ConfigStore(path=p)
    assert cs.poll() == {"final_score_threshold": 0.9}
    p.write_text("{not valid")
    import os
    t = time.time() + 2
    os.utime(p, (t, t))
    assert cs.poll() is None          # corrupt → no apply
    assert cs.snapshot() == {"final_score_threshold": 0.9}  # last-good retained


def test_poll_emits_empty_dict_when_file_deleted(tmp_path):
    p = tmp_path / "ov.json"
    _write(p, {"final_score_threshold": 0.9})
    cs = ConfigStore(path=p)
    cs.poll()
    p.unlink()
    assert cs.poll() == {}   # revert signal, once
    assert cs.poll() is None


def test_classify():
    assert ConfigStore.classify("final_score_threshold") == "safe"
    assert ConfigStore.classify("st2_sl_pct") == "flat_only"
    assert ConfigStore.classify("strategy_mode") == "restart"
    assert ConfigStore.classify("nonsense") == "unknown"


# ── apply_overrides hooks ────────────────────────────────────────────────────

def test_risk_apply_overrides():
    r = RiskManager()
    r.apply_overrides({"max_daily_trades": 9, "cooldown_after_loss_minutes": 30,
                       "max_daily_drawdown_pct": 0.02})
    assert r.max_daily == 9
    assert r.cooldown_after_loss_seconds == 30 * 60
    assert r.max_daily_drawdown_pct == 0.02


def test_adaptive_apply_overrides():
    a = AdaptiveThresholdManager(base_threshold=0.7)
    a.apply_overrides({"final_score_threshold": 0.85, "adaptive_target_trades_per_day": 4})
    assert a.base_threshold == 0.85
    assert a.target_interval == pytest.approx(6.0)  # 24/4


def test_st2_apply_overrides():
    pb = PlaybookSupertrend2()
    pb.apply_overrides({"st2_factor": 4.5, "st2_sl_pct": 2.0, "mr_entry_band": 0.1})
    assert pb.factor == 4.5
    assert pb.sl_pct == 2.0
    # foreign-prefix key ignored
    assert not hasattr(pb, "entry_band") or pb.__dict__.get("entry_band") != 0.1


def test_mr_apply_overrides():
    pb = PlaybookMeanReversion()
    pb.apply_overrides({"mr_entry_band": 0.03, "mr_stop_loss_pct": 0.01})
    assert pb.entry_band == 0.03
    assert pb.stop_loss_pct == 0.01


# ── engine routing ───────────────────────────────────────────────────────────

def _engine_shim(tmp_path, has_position=False):
    pos = object() if has_position else None
    return SimpleNamespace(
        symbol="SOLUSDT",
        cfg=SimpleNamespace(final_score_threshold=0.7, st2_sl_pct=1.5),
        config_store=ConfigStore(path=tmp_path / "ov.json"),
        _pending_flat_overrides={},
        wallet=SimpleNamespace(get_open_position=lambda s: pos),
        adaptive_threshold=AdaptiveThresholdManager(),
        risk_manager=RiskManager(),
        playbook_st2=PlaybookSupertrend2(),
        playbook_mr=None,
    )


def test_engine_applies_safe_override(tmp_path):
    sh = _engine_shim(tmp_path, has_position=False)
    _write(tmp_path / "ov.json", {"final_score_threshold": 0.88, "st2_factor": 5.0})
    WebSocketTradingEngine._apply_runtime_overrides(sh)
    assert sh.adaptive_threshold.base_threshold == 0.88
    assert sh.playbook_st2.factor == 5.0
    assert sh.cfg.final_score_threshold == 0.88


def test_engine_defers_flat_only_when_in_position(tmp_path):
    sh = _engine_shim(tmp_path, has_position=True)
    _write(tmp_path / "ov.json", {"st2_sl_pct": 3.0})
    WebSocketTradingEngine._apply_runtime_overrides(sh)
    # not applied yet — still buffered
    assert sh.playbook_st2.sl_pct != 3.0
    assert "st2_sl_pct" in sh._pending_flat_overrides

    # next tick, no file change, now flat → drains the buffer
    sh.wallet.get_open_position = lambda s: None
    WebSocketTradingEngine._apply_runtime_overrides(sh)
    assert sh.playbook_st2.sl_pct == 3.0
    assert sh._pending_flat_overrides == {}


def test_engine_ignores_restart_keys(tmp_path):
    sh = _engine_shim(tmp_path, has_position=False)
    _write(tmp_path / "ov.json", {"strategy_mode": "mean_reversion", "st2_atr_period": 99})
    before = sh.playbook_st2.atr_period
    WebSocketTradingEngine._apply_runtime_overrides(sh)
    assert sh.playbook_st2.atr_period == before  # unchanged
