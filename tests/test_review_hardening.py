"""
Post-implementation review hardening (ADR-001 PR review, round 2).

One test per failure mode found while tracing the production paths:

  F1  paper mock venue spec advertised 20x — paper mode could trade leverage
      live CoinDCX would clamp to 10, poisoning shadow-mode validation.
  F2  aggressive profile cap 20x + MAX_LEVERAGE env accepted unclamped —
      config/venue divergence live, real 20x positions in paper.
  F3  shadow inference spawned an unbounded thread per decision — a stalled
      candidate model piles up GPU work that contends with the PRIMARY model.
  F4  shadow metrics depended on the operator's LLM_STRICT_TRUNCATION toggle —
      agreement rate inflated when the live gate was off.
  F5  bare build_router() (multi_engine position-management) hardcoded
      localhost:11434 — non-default Ollama host/port silently degraded PM to
      the deterministic scorer.
  F6  malformed OLLAMA_NUM_PREDICT crashed the advisor's router build inside a
      bare int() — router silently replaced by the legacy client.
"""
import threading
from types import SimpleNamespace

import pytest

from crypto_trader.ai.router import build_router
from crypto_trader.ai.schemas import LLMDecision, MarketStatePayload
from crypto_trader.ai.shadow import ShadowEvaluator
from crypto_trader.ai.validators.decision_schema import DecisionValidator
from crypto_trader.config import TradingConfig
from crypto_trader.config._settings import _TRADING_PROFILES, SYSTEM_MAX_LEVERAGE
from crypto_trader.margin_engine import DynamicLeverageManager, LeverageEngine


# ── shared helpers (mirror test_ai_shadow.py conventions) ────────────────────

def _state():
    return MarketStatePayload(
        symbol="SOLUSDT", timeframe="15m", mode="intraday", price=100.0,
        htf_trend="bullish", market_structure="BOS_UP",
        volatility_regime="expanding", funding_rate=0.0001,
        open_interest_change=5.0, volume_anomaly=False, liquidity_sweep=False,
        risk_budget_pct=0.02,
    )


def _decision(action="LONG", conf=0.9):
    return LLMDecision(
        action=action, confidence=conf, setup_type="BOS Continuation",
        entry_zone={"low": 99.5, "high": 100.2}, stop_loss=99.0,
        targets=[105.0], risk_reward=2.5, invalidation="Structure break",
        warnings=[], reason_codes=[],
    )


def _valid_long_json(conf=0.8):
    return (
        '{"action":"LONG","confidence":' + str(conf) + ',"setup_type":"BOS Continuation",'
        '"entry_zone":{"low":99.5,"high":100.2},"stop_loss":99.0,'
        '"targets":[105.0],"risk_reward":2.5,"invalidation":"Structure break",'
        '"warnings":[],"reason_codes":[]}'
    )


class _FakeProvider:
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
    def validate(self, raw, state, strict_truncation=None):
        if isinstance(raw, LLMDecision):
            return raw, None
        return None, "not a decision"

    @staticmethod
    def fallback_no_trade():
        return LLMDecision(action="NO_TRADE", confidence=0.0,
                           reason_codes=["GATED_BY_SYSTEM"])


def _router(local, cloud, **kw):
    from crypto_trader.ai.router import LLMRouter
    return LLMRouter(
        cloud_provider=cloud, local_provider=local,
        cache=_FakeCache(), telemetry=_FakeTelemetry(),
        validator=_PassThroughValidator(), **kw,
    )


# ── F1: paper venue spec capped at the system cap ────────────────────────────

def test_f1_paper_mock_spec_capped_at_system_cap():
    from crypto_trader.execution.adapters.paper import _MockMapper
    spec = _MockMapper().get_spec("SOLUSDT")
    assert spec.max_leverage == SYSTEM_MAX_LEVERAGE == 10


def test_f1_paper_engine_cannot_operate_above_10x(monkeypatch):
    """The exact paper-mode hole: MAX_LEVERAGE=20 used to pass engine_live's
    `cfg.max_leverage <= spec.max_leverage` gate at 20x."""
    from crypto_trader.execution.adapters.paper import _MockMapper
    monkeypatch.setenv("MAX_LEVERAGE", "20")
    monkeypatch.delenv("TRADING_PROFILE", raising=False)
    cfg = TradingConfig.from_env()
    spec = _MockMapper().get_spec("SOLUSDT")
    assert cfg.max_leverage <= spec.max_leverage
    assert cfg.max_leverage == 10 and spec.max_leverage == 10


# ── F2: config layer clamped to the system cap ───────────────────────────────

def test_f2_from_env_clamps_max_leverage(monkeypatch):
    monkeypatch.setenv("MAX_LEVERAGE", "50")
    monkeypatch.delenv("LEVERAGE", raising=False)
    monkeypatch.delenv("TRADING_PROFILE", raising=False)
    cfg = TradingConfig.from_env()
    assert cfg.max_leverage == 10


def test_f2_all_profile_caps_within_system_cap():
    for name, profile in _TRADING_PROFILES.items():
        assert profile.max_leverage <= SYSTEM_MAX_LEVERAGE, (
            f"profile {name} permits {profile.max_leverage}x — above the "
            "ADR-001 system cap"
        )


def test_f2_dynamic_band_ceiling_clamped_in_manager():
    """Hand-edited config store / DYNAMIC_LEVERAGE_MAX env cannot push the
    manager's band above the system cap (engine-layer clamp, from_env passes
    the raw value through per test_dynamic_leverage's contract)."""
    cfg = SimpleNamespace(dynamic_leverage_min=5, dynamic_leverage_max=20)
    m = DynamicLeverageManager(cfg)
    assert m.max_leverage == 10
    lev = m.compute("S", base_leverage=20, venue_max_leverage=20,
                    atr_pct=0.0, drawdown=0.0, margin_ratio=0.0,
                    regime="TREND_EXPANSION")
    assert lev <= 10


def test_f2_system_cap_single_source():
    from crypto_trader.exchanges.instrument_mapper import _MAX_USABLE_LEVERAGE
    assert _MAX_USABLE_LEVERAGE == SYSTEM_MAX_LEVERAGE
    monkeypatch_free = LeverageEngine()
    assert monkeypatch_free.hard_max_leverage == SYSTEM_MAX_LEVERAGE


# ── F3: shadow concurrency bounded, saturation drops + counts ────────────────

def _blocking_provider(gate):
    class _Blocking:
        def chat(self, system_prompt, user_prompt, timeout_s):
            gate.wait(timeout=5)
            return _valid_long_json(0.7)
    return _Blocking()


def test_f3_try_submit_drops_when_saturated(tmp_path):
    gate = threading.Event()
    ev = ShadowEvaluator(_blocking_provider(gate), DecisionValidator(),
                         "mini:2b", log_dir=tmp_path, max_concurrent=1)
    registry = []
    ok1 = ev.try_submit(_state(), "s", "u", 5, _decision("LONG", 0.9),
                        registry=registry)
    ok2 = ev.try_submit(_state(), "s", "u", 5, _decision("LONG", 0.9),
                        registry=registry)
    assert ok1 is True and ok2 is False
    snap = ev.metrics.snapshot()
    assert snap["dropped"] == 1
    assert snap["total"] == 0          # dropped ≠ evaluated
    gate.set()
    for t in registry:
        t.join(timeout=5)


def test_f3_router_backpressure_no_thread_pileup(tmp_path):
    gate = threading.Event()
    local = _FakeProvider(raw=_decision("LONG", 0.92))
    cloud = _FakeProvider()
    shadow = ShadowEvaluator(_blocking_provider(gate), DecisionValidator(),
                             "mini:2b", log_dir=tmp_path, max_concurrent=1)
    r = _router(local, cloud, shadow_evaluator=shadow)
    out1 = r.route(_state(), "sys", "usr")
    out2 = r.route(_state(), "sys", "usr")
    assert out1.action == "LONG" and out2.action == "LONG"   # trading unaffected
    assert len(r._shadow_threads) == 1                        # ONE shadow thread
    assert shadow.metrics.snapshot()["dropped"] == 1
    gate.set()
    for t in list(r._shadow_threads):
        t.join(timeout=5)


def test_f3_capacity_recovered_after_release(tmp_path):
    gate = threading.Event()
    ev = ShadowEvaluator(_blocking_provider(gate), DecisionValidator(),
                         "mini:2b", log_dir=tmp_path, max_concurrent=1)
    registry = []
    assert ev.try_submit(_state(), "s", "u", 5, _decision("LONG", 0.9),
                         registry=registry) is True
    gate.set()
    for t in registry:
        t.join(timeout=5)
    # slot released → next submit succeeds again
    assert ev.try_submit(_state(), "s", "u", 5, _decision("LONG", 0.9),
                         registry=registry) is True
    for t in registry[1:]:
        t.join(timeout=5)


# ── F4: shadow measures under the promotion gate regardless of env ──────────

def test_f4_strict_kwarg_overrides_env(monkeypatch):
    monkeypatch.setenv("LLM_STRICT_TRUNCATION", "false")
    truncated = _valid_long_json(0.8)[:-30]
    # env off → salvaged actionable decision passes the live path
    d, err = DecisionValidator.validate(truncated, _state())
    assert err is None and d.action == "LONG"
    # forced strict → rejected fail-closed
    d2, err2 = DecisionValidator.validate(truncated, _state(), strict_truncation=True)
    assert err2 is not None and d2.action == "NO_TRADE"


def test_f4_strict_kwarg_env_true_kept(monkeypatch):
    monkeypatch.setenv("LLM_STRICT_TRUNCATION", "false")
    complete = _valid_long_json(0.8)
    d, err = DecisionValidator.validate(complete, _state(), strict_truncation=True)
    assert err is None and d.action == "LONG"   # complete JSON unaffected by gate


def test_f4_shadow_forced_strict_even_when_env_off(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_STRICT_TRUNCATION", "false")
    ev = ShadowEvaluator(_FakeProvider(raw=_valid_long_json(0.8)[:-30]),
                         DecisionValidator(), "mini:2b", log_dir=tmp_path)
    res = ev.evaluate(_state(), "sys", "usr", 5, _decision("LONG", 0.9))
    assert res.schema_failed is True            # counted as schema failure, not agreement


# ── F5: bare build_router() honours the deployment host env ─────────────────

def test_f5_build_router_honours_ollama_base_url(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://192.168.1.50:11434")
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.delenv("LOCAL_OLLAMA_HOST", raising=False)
    monkeypatch.delenv("LOCAL_OLLAMA_HOSTS", raising=False)
    r = build_router()
    assert r.local.rotator.items == ["http://192.168.1.50:11434"]


def test_f5_build_router_ollama_host_fallback(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("LOCAL_OLLAMA_HOST", raising=False)
    monkeypatch.delenv("LOCAL_OLLAMA_HOSTS", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "http://10.0.0.9:11434")
    r = build_router()
    assert r.local.rotator.items == ["http://10.0.0.9:11434"]


def test_f5_explicit_host_still_wins(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://from-env:11434")
    monkeypatch.delenv("LOCAL_OLLAMA_HOSTS", raising=False)
    r = build_router(local_host="http://explicit:11434")
    assert r.local.rotator.items == ["http://explicit:11434"]


def test_f5_shadow_hosts_follow_resolved_local_host(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://remote-box:11434")
    monkeypatch.delenv("SHADOW_OLLAMA_HOSTS", raising=False)
    monkeypatch.delenv("LOCAL_OLLAMA_HOSTS", raising=False)
    monkeypatch.delenv("LOCAL_OLLAMA_HOST", raising=False)
    monkeypatch.setenv("SHADOW_OLLAMA_MODEL", "mini:2b")
    r = build_router()
    assert r.shadow is not None
    assert r.shadow.provider.rotator.items == ["http://remote-box:11434"]


def test_f5_shadow_max_concurrent_env(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("LOCAL_OLLAMA_HOSTS", raising=False)
    monkeypatch.setenv("SHADOW_OLLAMA_MODEL", "mini:2b")
    monkeypatch.setenv("SHADOW_MAX_CONCURRENT", "4")
    r = build_router()
    assert r.shadow.max_concurrent == 4


# ── F6: malformed OLLAMA_NUM_PREDICT no longer kills the router ─────────────

def _local_advisor(monkeypatch, reachable=True):
    import crypto_trader.llm_advisor as la
    monkeypatch.setattr(la.OllamaClient, "is_available", lambda self: reachable)
    return la.OllamaAdvisor(host="http://localhost:11434", model="qwen3.5:4b")


def test_f6_advisor_router_survives_malformed_num_predict(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_PREDICT", "not-a-number")
    advisor = _local_advisor(monkeypatch)
    assert advisor._router is not None                       # router still wired
    assert advisor._router.local.num_predict == 1536         # safe default


def test_f6_advisor_router_valid_num_predict_flows(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_PREDICT", "999")
    advisor = _local_advisor(monkeypatch)
    assert advisor._router is not None
    assert advisor._router.local.num_predict == 999


# ── F7: advisor construction must not crash in LOCAL mode ────────────────────
#
# Regression for the review's worst find: OllamaAdvisor.__init__ called
# self.client.is_ready() — a method that never existed on OllamaClient. In the
# default paper/local config (USE_CLOUD_LLM=false) every construction raised
# AttributeError; engine.py / engine_ws.py call build_advisor() unprotected, so
# engine startup died and the LLM/shadow path never ran at all.

def test_f7_local_mode_advisor_constructs_without_crash(monkeypatch):
    import crypto_trader.llm_advisor as la
    monkeypatch.delenv("USE_CLOUD_LLM", raising=False)
    advisor = _local_advisor(monkeypatch, reachable=False)   # Ollama down
    assert advisor._router is not None                       # still fully wired


def test_f7_advisor_construction_never_probes_when_cloud(monkeypatch):
    import crypto_trader.llm_advisor as la
    monkeypatch.setenv("USE_CLOUD_LLM", "true")

    def _boom(self):
        raise AssertionError("cloud mode must not probe the local host")

    monkeypatch.setattr(la.OllamaClient, "is_available", _boom)
    advisor = la.OllamaAdvisor(host="http://localhost:11434", model="m")
    assert advisor._router is not None


def test_f7_build_advisor_local_mode_returns_advisor(monkeypatch):
    import crypto_trader.llm_advisor as la
    monkeypatch.delenv("USE_CLOUD_LLM", raising=False)
    monkeypatch.setenv("USE_LLM", "true")
    advisor = la.build_advisor(use_llm=True, llm_host="http://localhost:11434",
                               llm_model="qwen3.5:4b")
    assert isinstance(advisor, la.OllamaAdvisor)
    assert advisor._router is not None
