"""
Phase 4 — cost/latency-aware LLM routing (triage local → escalate cloud).

Verifies: local-first triage, escalation only on uncertain/swing actionable
calls, cloud override on escalation, cloud-as-fallback when local down, safe
NO_TRADE when all providers fail, and that an OPEN provider (unhealthy) is
skipped rather than failing the whole route.
"""
from types import SimpleNamespace

import pytest

from crypto_trader.ai.router import LLMRouter
from crypto_trader.ai.schemas import LLMDecision, MarketStatePayload


class _FakeProvider:
    """Stand-in for an Ollama provider: scriptable health + raw chat output."""
    def __init__(self, healthy=True, raw=None):
        self._healthy = healthy
        self._raw = raw
        self.calls = 0

    def health(self):
        return self._healthy

    def chat(self, system_prompt, user_prompt, timeout_s):
        self.calls += 1
        return self._raw


class _FakeCache:
    def get(self, *a):
        return None

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


class _FakeValidator:
    """Passes raw dicts straight through as LLMDecision (raw is the decision)."""
    def validate(self, raw, state):
        if isinstance(raw, LLMDecision):
            return raw, None
        return None, "not a decision"

    @staticmethod
    def fallback_no_trade():
        return LLMDecision(action="NO_TRADE", confidence=0.0,
                           reason_codes=["GATED_BY_SYSTEM"])


def _decision(action="LONG", conf=0.65):
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


def _router(local, cloud, **kw):
    return LLMRouter(
        cloud_provider=cloud, local_provider=local,
        cache=_FakeCache(), telemetry=_FakeTelemetry(), validator=_FakeValidator(),
        **kw,
    )


def _state(mode="intraday"):
    return MarketStatePayload(
        symbol="SOLUSDT",
        timeframe="15m",
        mode=mode,
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


def test_local_triage_no_escalation_when_confident():
    """High-confidence local decision: cloud is NOT called (cost saving)."""
    local = _FakeProvider(raw=_decision("LONG", conf=0.92))
    cloud = _FakeProvider(raw=_decision("SHORT", conf=0.9))
    r = _router(local, cloud)
    out = r.route(_state(), "sys", "usr")
    assert out.action == "LONG"
    assert local.calls == 1
    assert cloud.calls == 0


def test_escalates_on_uncertain_confidence():
    """Local actionable but uncertain (in band) → escalate; cloud overrides."""
    local = _FakeProvider(raw=_decision("LONG", conf=0.65))
    cloud = _FakeProvider(raw=_decision("SHORT", conf=0.85))
    r = _router(local, cloud)
    out = r.route(_state(), "sys", "usr")
    assert local.calls == 1
    assert cloud.calls == 1
    assert out.action == "SHORT"  # cloud conviction overrides


def test_no_escalation_for_no_trade():
    """Local NO_TRADE never escalates (nothing at stake)."""
    local = _FakeProvider(raw=_decision("NO_TRADE", conf=0.6))
    cloud = _FakeProvider(raw=_decision("LONG", conf=0.9))
    r = _router(local, cloud)
    out = r.route(_state(), "sys", "usr")
    assert out.action == "NO_TRADE"
    assert cloud.calls == 0


def test_swing_mode_escalates_even_when_confident():
    local = _FakeProvider(raw=_decision("LONG", conf=0.95))
    cloud = _FakeProvider(raw=_decision("LONG", conf=0.97))
    r = _router(local, cloud)
    out = r.route(_state(mode="swing"), "sys", "usr")
    assert cloud.calls == 1


def test_escalation_disabled_keeps_local():
    local = _FakeProvider(raw=_decision("LONG", conf=0.65))
    cloud = _FakeProvider(raw=_decision("SHORT", conf=0.9))
    r = _router(local, cloud, enable_escalation=False)
    out = r.route(_state(), "sys", "usr")
    assert cloud.calls == 0
    assert out.action == "LONG"


def test_cloud_failure_on_escalation_keeps_local():
    """Escalation attempt fails → keep the local decision, don't NO_TRADE."""
    local = _FakeProvider(raw=_decision("LONG", conf=0.65))
    cloud = _FakeProvider(healthy=True, raw=None)  # cloud returns nothing
    r = _router(local, cloud)
    out = r.route(_state(), "sys", "usr")
    assert out.action == "LONG"


def test_local_down_routes_to_cloud_primary():
    local = _FakeProvider(healthy=False)
    cloud = _FakeProvider(raw=_decision("SHORT", conf=0.7))
    r = _router(local, cloud)
    out = r.route(_state(), "sys", "usr")
    assert local.calls == 0       # unhealthy local is skipped, not called
    assert cloud.calls == 1
    assert out.action == "SHORT"


def test_both_down_returns_safe_no_trade():
    local = _FakeProvider(healthy=False)
    cloud = _FakeProvider(healthy=False)
    r = _router(local, cloud)
    out = r.route(_state(), "sys", "usr")
    assert out.action == "NO_TRADE"
    assert "GATED_BY_SYSTEM" in out.reason_codes


def test_local_returns_garbage_falls_back_to_cloud():
    """Local responds but validation rejects → cloud picks up as fallback."""
    local = _FakeProvider(raw="garbage-not-a-decision")
    cloud = _FakeProvider(raw=_decision("LONG", conf=0.7))
    r = _router(local, cloud)
    out = r.route(_state(), "sys", "usr")
    assert out.action == "LONG"
    assert cloud.calls == 1
