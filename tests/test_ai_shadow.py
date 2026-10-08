"""
Shadow evaluation harness (candidate-model trial beside the live decision path)
and per-role model selection in build_router().

Verifies:
  - ShadowEvaluator metrics: agreement, schema failures, call failures, JSONL log
  - Router wiring: shadow fires off-thread, never mutates the primary decision,
    never breaks routing when it explodes, skipped on cache hits
  - build_router(): triage/conviction/shadow roles resolve from env, explicit
    args still win, unset SHADOW_OLLAMA_MODEL keeps the router shadow-free
"""
import json
import threading

import pytest

from crypto_trader.ai.router import LLMRouter, build_router
from crypto_trader.ai.schemas import LLMDecision, MarketStatePayload
from crypto_trader.ai.shadow import ShadowEvaluator, ShadowMetrics, ShadowResult
from crypto_trader.ai.validators.decision_schema import DecisionValidator


# ── fakes (mirror test_llm_routing.py conventions) ──────────────────────────

class _FakeProvider:
    def __init__(self, healthy=True, raw=None, exc=None):
        self._healthy = healthy
        self._raw = raw
        self._exc = exc
        self.calls = 0

    def health(self):
        return self._healthy

    def chat(self, system_prompt, user_prompt, timeout_s):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._raw


class _FakeCache:
    def __init__(self, hit=None):
        self._hit = hit

    def get(self, *a):
        return self._hit

    def set(self, *a):
        pass


class _FakeTelemetry:
    def start_timer(self):
        return 0.0

    def stop_timer(self, s):
        return 1.0

    def record_cache_hit(self, *a):
        pass

    def record_success(self, *a):
        pass

    def record_failure(self, *a):
        pass


class _PassThroughValidator:
    """Passes raw LLMDecision objects straight through."""
    def validate(self, raw, state):
        if isinstance(raw, LLMDecision):
            return raw, None
        return None, "not a decision"

    @staticmethod
    def fallback_no_trade():
        return LLMDecision(action="NO_TRADE", confidence=0.0,
                           reason_codes=["GATED_BY_SYSTEM"])


class _ExplodingEvaluator:
    def evaluate(self, *a, **kw):
        raise RuntimeError("shadow harness exploded")


def _decision(action="LONG", conf=0.9):
    return LLMDecision(
        action=action,
        confidence=conf,
        setup_type="Sweep Reversal",
        entry_zone={"low": 100.0, "high": 101.0},
        stop_loss=99.0,
        targets=[105.0],
        risk_reward=4.0,
        invalidation="Structure break",
        warnings=[],
        reason_codes=[],
    )


def _state():
    return MarketStatePayload(
        symbol="SOLUSDT",
        timeframe="15m",
        mode="intraday",
        price=100.0,
        htf_trend="bullish",
        market_structure="BOS_UP",
        volatility_regime="expanding",
        funding_rate=0.0001,
        open_interest_change=5.0,
        volume_anomaly=False,
        liquidity_sweep=False,
        risk_budget_pct=0.02,
    )


def _valid_long_json(conf=0.8):
    return (
        '{"action":"LONG","confidence":' + str(conf) + ',"setup_type":"BOS Continuation",'
        '"entry_zone":{"low":99.5,"high":100.2},"stop_loss":99.0,'
        '"targets":[105.0],"risk_reward":2.5,"invalidation":"Structure break",'
        '"warnings":[],"reason_codes":[]}'
    )


def _router(local, cloud, **kw):
    return LLMRouter(
        cloud_provider=cloud, local_provider=local,
        cache=_FakeCache(), telemetry=_FakeTelemetry(),
        validator=_PassThroughValidator(),
        **kw,
    )


# ── ShadowEvaluator ──────────────────────────────────────────────────────────

def test_shadow_agreement_recorded(tmp_path):
    shadow_provider = _FakeProvider(raw=_valid_long_json(0.75))
    ev = ShadowEvaluator(shadow_provider, DecisionValidator(), "mini:2b",
                         log_dir=tmp_path)
    result = ev.evaluate(_state(), "sys", "usr", 5, _decision("LONG", 0.9))
    assert result.agreed_action is True
    assert result.action == "LONG"
    assert not result.schema_failed and not result.call_failed
    snap = ev.metrics.snapshot()
    assert snap["total"] == 1 and snap["agreement_rate"] == 1.0
    assert snap["schema_fail_rate"] == 0.0


def test_shadow_disagreement_recorded(tmp_path):
    shadow_provider = _FakeProvider(raw=_valid_long_json(0.4))
    ev = ShadowEvaluator(shadow_provider, DecisionValidator(), "mini:2b",
                         log_dir=tmp_path)
    result = ev.evaluate(_state(), "sys", "usr", 5, _decision("SHORT", 0.9))
    assert result.agreed_action is False
    assert ev.metrics.snapshot()["agreement_rate"] == 0.0


def test_shadow_schema_failure_counted(tmp_path):
    shadow_provider = _FakeProvider(raw="garbage-not-json")
    ev = ShadowEvaluator(shadow_provider, DecisionValidator(), "mini:2b",
                         log_dir=tmp_path)
    result = ev.evaluate(_state(), "sys", "usr", 5, _decision("LONG", 0.9))
    assert result.schema_failed is True
    assert result.agreed_action is None
    snap = ev.metrics.snapshot()
    assert snap["schema_failures"] == 1
    assert snap["schema_fail_rate"] == 1.0
    assert snap["valid_decisions"] == 0


def test_shadow_call_failure_counted(tmp_path):
    shadow_provider = _FakeProvider(raw=None)  # transport-level empty response
    ev = ShadowEvaluator(shadow_provider, DecisionValidator(), "mini:2b",
                         log_dir=tmp_path)
    result = ev.evaluate(_state(), "sys", "usr", 5, _decision("LONG", 0.9))
    assert result.call_failed is True
    snap = ev.metrics.snapshot()
    assert snap["call_failures"] == 1
    assert snap["schema_failures"] == 0


def test_shadow_provider_exception_swallowed(tmp_path):
    shadow_provider = _FakeProvider(exc=ConnectionError("host down"))
    ev = ShadowEvaluator(shadow_provider, DecisionValidator(), "mini:2b",
                         log_dir=tmp_path)
    result = ev.evaluate(_state(), "sys", "usr", 5, _decision("LONG", 0.9))
    assert result.call_failed is True
    assert "ConnectionError" in result.error


def test_shadow_truncated_actionable_counts_as_schema_failure(tmp_path):
    """Strict truncation gate + shadow: repaired actionable output is a fail —
    exactly what a promoted candidate would face in production."""
    truncated = _valid_long_json(0.8)[:-30]  # cut mid-object
    shadow_provider = _FakeProvider(raw=truncated)
    ev = ShadowEvaluator(shadow_provider, DecisionValidator(), "mini:2b",
                         log_dir=tmp_path)
    result = ev.evaluate(_state(), "sys", "usr", 5, _decision("LONG", 0.9))
    assert result.schema_failed is True


def test_shadow_jsonl_audit_log(tmp_path):
    shadow_provider = _FakeProvider(raw=_valid_long_json(0.7))
    ev = ShadowEvaluator(shadow_provider, DecisionValidator(), "mini:2b",
                         log_dir=tmp_path)
    ev.evaluate(_state(), "sys", "usr", 5, _decision("LONG", 0.9))
    files = list(tmp_path.glob("shadow_*.jsonl"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text().strip())
    assert entry["shadow_model"] == "mini:2b"
    assert entry["primary_action"] == "LONG"
    assert entry["shadow_action"] == "LONG"
    assert entry["agreed_action"] is True


def test_shadow_metrics_latency_percentiles(tmp_path):
    ev = ShadowEvaluator(_FakeProvider(raw=_valid_long_json(0.7)),
                         DecisionValidator(), "mini:2b", log_dir=tmp_path)
    primary = _decision("LONG", 0.9)
    for _ in range(5):
        ev.evaluate(_state(), "sys", "usr", 5, primary)
    snap = ev.metrics.snapshot()
    assert snap["latency_p50_ms"] is not None
    assert snap["latency_p95_ms"] is not None
    assert snap["latency_p95_ms"] >= snap["latency_p50_ms"]


def test_shadow_metrics_thread_safety(tmp_path):
    m = ShadowMetrics("mini:2b")
    def _bump():
        for _ in range(50):
            m.record(ShadowResult(model="mini:2b", action="LONG",
                                  confidence=0.5, latency_ms=1.0,
                                  agreed_action=True))
    threads = [threading.Thread(target=_bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert m.snapshot()["total"] == 200
    assert m.snapshot()["agreement_rate"] == 1.0


# ── router wiring ────────────────────────────────────────────────────────────

def _join_shadow_threads(router):
    for t in list(router._shadow_threads):
        t.join(timeout=5)


def test_router_fires_shadow_without_mutating_decision():
    local = _FakeProvider(raw=_decision("LONG", 0.92))
    cloud = _FakeProvider(raw=_decision("SHORT", 0.9))
    calls = []
    shadow = ShadowEvaluator(_FakeProvider(raw=_valid_long_json(0.7)),
                             DecisionValidator(), "mini:2b",
                             log_dir="/nonexistent-shadow-log")
    # wrap evaluate to assert it saw the primary decision
    orig = shadow.evaluate
    def _spy(state, sys_p, usr_p, timeout, primary):
        calls.append(primary.action)
        return orig(state, sys_p, usr_p, timeout, primary)
    shadow.evaluate = _spy

    r = _router(local, cloud, shadow_evaluator=shadow)
    out = r.route(_state(), "sys", "usr")
    _join_shadow_threads(r)
    assert out.action == "LONG"            # primary decision untouched
    assert local.calls == 1 and cloud.calls == 0
    assert calls == ["LONG"]               # shadow saw the same primary


def test_router_shadow_explosion_does_not_break_route():
    local = _FakeProvider(raw=_decision("LONG", 0.92))
    cloud = _FakeProvider(raw=_decision("SHORT", 0.9))
    r = _router(local, cloud, shadow_evaluator=_ExplodingEvaluator())
    out = r.route(_state(), "sys", "usr")
    _join_shadow_threads(r)
    assert out.action == "LONG"


def test_router_cache_hit_skips_shadow():
    primary = _decision("LONG", 0.92)
    local = _FakeProvider(raw=primary)
    cloud = _FakeProvider()
    shadow = ShadowEvaluator(_FakeProvider(raw=_valid_long_json(0.7)),
                             DecisionValidator(), "mini:2b",
                             log_dir="/nonexistent-shadow-log")
    r = _router(local, cloud, shadow_evaluator=shadow)
    r_with_cache = LLMRouter(
        cloud_provider=cloud, local_provider=local,
        cache=_FakeCache(hit=primary), telemetry=_FakeTelemetry(),
        validator=_PassThroughValidator(), shadow_evaluator=shadow,
    )
    out = r_with_cache.route(_state(), "sys", "usr")
    _join_shadow_threads(r_with_cache)
    assert out.action == "LONG"
    assert local.calls == 0 and cloud.calls == 0   # cache served; no inference
    assert len(r_with_cache._shadow_threads) == 0  # shadow not fired on hits


# ── build_router per-role env selection ──────────────────────────────────────

def test_build_router_env_models(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "triage-model:2b")
    monkeypatch.setenv("CLOUD_OLLAMA_MODEL", "conviction-model:20b")
    monkeypatch.delenv("SHADOW_OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("LOCAL_OLLAMA_HOSTS", raising=False)
    r = build_router()
    assert r.local.model == "triage-model:2b"
    assert r.cloud.model == "conviction-model:20b"
    assert r.shadow is None                 # unset → disabled, zero extra cost


def test_build_router_shadow_role(monkeypatch):
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("CLOUD_OLLAMA_MODEL", raising=False)
    monkeypatch.setenv("SHADOW_OLLAMA_MODEL", "minicpm-candidate:2b")
    r = build_router()
    assert r.shadow is not None
    assert r.shadow.model == "minicpm-candidate:2b"
    assert r.local.model == "qwen3.5:4b"    # triage role untouched


def test_build_router_explicit_args_beat_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "env-model:2b")
    monkeypatch.setenv("CLOUD_OLLAMA_MODEL", "env-cloud:20b")
    r = build_router(local_model="explicit-model:7b", cloud_model="explicit-cloud:70b")
    assert r.local.model == "explicit-model:7b"
    assert r.cloud.model == "explicit-cloud:70b"


def test_build_router_shadow_hosts_default_to_local(monkeypatch):
    monkeypatch.setenv("SHADOW_OLLAMA_MODEL", "mini:2b")
    monkeypatch.setenv("LOCAL_OLLAMA_HOSTS", "http://h1:11434,http://h2:11434")
    r = build_router()
    assert r.shadow is not None
    assert r.shadow.provider.rotator.items == ["http://h1:11434", "http://h2:11434"]
