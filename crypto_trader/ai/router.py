"""crypto_trader.ai.router — Adaptive LLM routing and fallback logic."""

import logging
import os
import threading
from typing import Optional

from crypto_trader.ai.schemas import MarketStatePayload, LLMDecision
from crypto_trader.ai.providers.ollama_local import OllamaLocalProvider
from crypto_trader.ai.providers.ollama_cloud import OllamaCloudProvider
from crypto_trader.ai.cache import LLMCache
from crypto_trader.ai.telemetry import LLMTelemetry
from crypto_trader.ai.validators.decision_schema import DecisionValidator

logger = logging.getLogger("crypto_trader.ai.router")


class LLMRouter:
    def __init__(
        self,
        cloud_provider: OllamaCloudProvider,
        local_provider: OllamaLocalProvider,
        cache: LLMCache,
        telemetry: LLMTelemetry,
        validator: DecisionValidator,
        enable_escalation: bool = True,
        escalate_conf_low: float = 0.55,
        escalate_conf_high: float = 0.80,
        escalate_on_swing: bool = True,
        shadow_evaluator=None,
    ):
        self.cloud = cloud_provider
        self.local = local_provider
        self.cache = cache
        self.telemetry = telemetry
        self.validator = validator
        # Cost/latency-aware routing: triage on cheap local first, escalate to
        # cloud only for high-stakes/uncertain calls.
        self.enable_escalation = enable_escalation
        self.escalate_conf_low = escalate_conf_low
        self.escalate_conf_high = escalate_conf_high
        self.escalate_on_swing = escalate_on_swing
        # Optional shadow evaluation (candidate-model trial). The shadow call
        # NEVER influences the returned decision — it runs on a daemon thread,
        # logs agreement metrics, and all its failures are swallowed.
        self.shadow = shadow_evaluator
        self._shadow_threads: list = []

    def _should_escalate(self, decision: LLMDecision, state: MarketStatePayload) -> bool:
        """Escalate a local triage decision to cloud conviction only when it's
        actionable AND either (a) swing mode (deep reasoning warranted) or
        (b) confidence sits in the uncertain band. Confident or NO_TRADE calls
        stay local — that's where the cost/latency savings come from."""
        if not self.enable_escalation:
            return False
        if decision.action == "NO_TRADE":
            return False
        if self.escalate_on_swing and getattr(state, "mode", "intraday") == "swing":
            return True
        return self.escalate_conf_low <= decision.confidence <= self.escalate_conf_high

    def _attempt(
        self, provider, provider_name: str, state: MarketStatePayload,
        system_prompt: str, user_prompt: str, timeout_s: int,
    ):
        """Call one provider and validate. Returns (decision|None, latency_ms).
        A None decision means call failed or validation rejected the output."""
        start_time = self.telemetry.start_timer()
        raw_output = provider.chat(system_prompt, user_prompt, timeout_s)
        latency = self.telemetry.stop_timer(start_time)
        if not raw_output:
            return None, latency
        decision, err_msg = self.validator.validate(raw_output, state)
        if err_msg:
            logger.warning("[Router] %s validation failed: %s", provider_name, err_msg)
            self.telemetry.record_failure(
                state.symbol, provider_name, f"validation_error: {err_msg}", latency
            )
            return None, latency
        return decision, latency

    def route(
        self,
        state: MarketStatePayload,
        system_prompt: str,
        user_prompt: str,
        timeout_s: int = 10,
    ) -> LLMDecision:
        # 1. Cache hit check
        cached_result = self.cache.get(state, system_prompt, user_prompt)
        if cached_result:
            self.telemetry.record_cache_hit(state.symbol)
            return cached_result

        local_ok = self.local.health()
        cloud_ok = self.cloud.health()
        decision = None
        provider_used = "none"
        latency = 0.0

        # 2. Triage on local first (cheap, low latency).
        if local_ok:
            logger.info("[Router] Triage %s on LOCAL", state.symbol)
            decision, latency = self._attempt(
                self.local, "local", state, system_prompt, user_prompt, timeout_s
            )
            provider_used = "local"

        # 3. Escalate to cloud for conviction on high-stakes/uncertain calls.
        if decision is not None and cloud_ok and self._should_escalate(decision, state):
            logger.info("[Router] Escalating %s to CLOUD for conviction "
                        "(action=%s conf=%.2f)", state.symbol, decision.action, decision.confidence)
            cloud_decision, cloud_latency = self._attempt(
                self.cloud, "cloud_escalated", state, system_prompt, user_prompt, timeout_s
            )
            if cloud_decision is not None:
                decision, latency, provider_used = cloud_decision, cloud_latency, "cloud_escalated"
            # cloud failure → keep the local decision (no veto on escalation miss)

        # 4. Local unavailable/failed → cloud as primary fallback.
        if decision is None and cloud_ok:
            logger.info("[Router] LOCAL unavailable for %s, routing to CLOUD primary", state.symbol)
            decision, latency = self._attempt(
                self.cloud, "cloud", state, system_prompt, user_prompt, timeout_s
            )
            provider_used = "cloud"

        # 5. Total failure → safe NO_TRADE (does not veto technical signals downstream).
        if decision is None:
            logger.error("[Router] All providers failed for %s. Outputting NO_TRADE.", state.symbol)
            self.telemetry.record_failure(state.symbol, provider_used, "all_providers_failed", latency)
            return DecisionValidator.fallback_no_trade()

        # 6. Populate cache & telemetry.
        self.cache.set(state, system_prompt, user_prompt, decision)
        self.telemetry.record_success(state.symbol, provider_used, decision, latency)

        # 7. Fire-and-forget shadow evaluation (candidate model trial). Never
        # blocks the trading path and never mutates the returned decision.
        if self.shadow is not None:
            self._launch_shadow(state, system_prompt, user_prompt, timeout_s, decision)

        return decision

    def _launch_shadow(self, state, system_prompt, user_prompt, timeout_s,
                       primary: LLMDecision) -> None:
        """Run the shadow evaluator on a daemon thread; exceptions are swallowed
        and counted. Thread refs are kept (bounded) only so tests can join."""
        def _run():
            try:
                self.shadow.evaluate(state, system_prompt, user_prompt,
                                     timeout_s, primary)
            except Exception as e:  # shadow must never break trading
                logger.debug("[Router] shadow evaluation error: %s", e)

        t = threading.Thread(target=_run, daemon=True,
                             name=f"shadow-{state.symbol}")
        t.start()
        self._shadow_threads.append(t)
        # Bound memory: keep only the last 32 thread refs.
        del self._shadow_threads[:-32]


def build_router(
    cloud_host: Optional[str] = None,
    cloud_model: Optional[str] = None,
    cloud_api_key: Optional[str] = None,
    local_host: Optional[str] = None,
    local_model: Optional[str] = None,
    local_num_predict: Optional[int] = None,
    intraday_cache_ttl: int = 45,
    swing_cache_ttl: int = 300,
) -> LLMRouter:
    """Factory: build a fully wired LLMRouter with per-role model selection.

    Each LLM role has its own model, resolved from env when the argument is
    omitted (explicit args always win). With every variable unset, behaviour is
    identical to the historical defaults:

      - Triage role (local):      OLLAMA_MODEL            (default qwen3.5:4b)
      - Conviction role (cloud):  CLOUD_OLLAMA_MODEL      (default gpt-oss:20b)
      - Shadow role (candidate):  SHADOW_OLLAMA_MODEL     (unset → disabled)

    Hosts/keys: LOCAL_OLLAMA_HOSTS, CLOUD_OLLAMA_HOST, CLOUD_OLLAMA_API_KEY and
    optional SHADOW_OLLAMA_HOSTS (defaults to the local host list). Shadow hosts
    must serve the shadow tag — a candidate model that exists only on one box
    should get its own SHADOW_OLLAMA_HOSTS.

    Both local (comma-separated host lists) and cloud (comma-separated keys)
    auto-rotate across their configured resources with per-provider circuit
    breakers. Routing triages on local and escalates to cloud only for
    uncertain/high-stakes calls — tunable via env."""
    from .providers.ollama_cloud import OllamaCloudProvider
    from .providers.ollama_local import OllamaLocalProvider
    from .cache import LLMCache
    from .telemetry import LLMTelemetry
    from .validators.decision_schema import DecisionValidator
    from .schemas import LLMDecision
    from .shadow import ShadowEvaluator

    cloud_host = cloud_host or os.getenv("CLOUD_OLLAMA_HOST", "https://ollama.com")
    cloud_model = cloud_model or os.getenv("CLOUD_OLLAMA_MODEL", "gpt-oss:20b")
    cloud_api_key = cloud_api_key if cloud_api_key is not None else os.getenv("CLOUD_OLLAMA_API_KEY", "")
    local_host = local_host or "http://localhost:11434"
    local_model = local_model or os.getenv("OLLAMA_MODEL", "qwen3.5:4b")
    if local_num_predict is None:
        try:
            local_num_predict = int(os.getenv("OLLAMA_NUM_PREDICT", "1536"))
        except (TypeError, ValueError):
            local_num_predict = 1536

    # Grammar-constrain local output to the exact decision shape (Ollama supports
    # a JSON schema in `format`). Cloud uses OpenAI-compat response_format instead
    # — Ollama Cloud does not support schema-based structured outputs.
    decision_schema = LLMDecision.model_json_schema()

    # Allow a comma-separated host list for local failover (mirrors cloud keys).
    local_hosts = os.getenv("LOCAL_OLLAMA_HOSTS", local_host)

    def _f(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, default))
        except (TypeError, ValueError):
            return default

    # Shadow role: opt-in candidate-model trial. Unset variable → disabled and
    # the router behaves exactly as before (zero extra inference, zero cost).
    shadow_evaluator = None
    shadow_model = os.getenv("SHADOW_OLLAMA_MODEL", "").strip()
    if shadow_model:
        shadow_hosts = os.getenv("SHADOW_OLLAMA_HOSTS", "").strip() or local_hosts
        shadow_evaluator = ShadowEvaluator(
            provider=OllamaLocalProvider(
                host=shadow_hosts, model=shadow_model,
                num_predict=local_num_predict, json_schema=decision_schema,
            ),
            validator=DecisionValidator(),
            model=shadow_model,
        )
        logger.info("[Router] Shadow evaluation enabled (model=%s)", shadow_model)

    return LLMRouter(
        cloud_provider=OllamaCloudProvider(host=cloud_host, model=cloud_model, api_key=cloud_api_key),
        local_provider=OllamaLocalProvider(
            host=local_hosts, model=local_model,
            num_predict=local_num_predict, json_schema=decision_schema,
        ),
        cache=LLMCache(intraday_ttl_s=intraday_cache_ttl, swing_ttl_s=swing_cache_ttl),
        telemetry=LLMTelemetry(),
        validator=DecisionValidator(),
        enable_escalation=os.getenv("LLM_ENABLE_ESCALATION", "true").lower() == "true",
        escalate_conf_low=_f("LLM_ESCALATE_CONF_LOW", 0.55),
        escalate_conf_high=_f("LLM_ESCALATE_CONF_HIGH", 0.80),
        escalate_on_swing=os.getenv("LLM_ESCALATE_ON_SWING", "true").lower() == "true",
        shadow_evaluator=shadow_evaluator,
    )
