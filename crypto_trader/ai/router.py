"""crypto_trader.ai.router — Adaptive LLM routing and fallback logic."""

import logging
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
    ):
        self.cloud = cloud_provider
        self.local = local_provider
        self.cache = cache
        self.telemetry = telemetry
        self.validator = validator

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

        provider_used = "none"
        raw_output = None
        latency = 0.0

        # 2. Route selection:
        #    Cloud first for swing (deep reasoning), local for intraday (low latency)
        use_cloud = self.cloud.health() and state.mode == "swing"

        if use_cloud:
            logger.info(f"[Router] Routing {state.symbol} to CLOUD primary")
            provider_used = "cloud"
            start_time = self.telemetry.start_timer()
            raw_output = self.cloud.chat(system_prompt, user_prompt, timeout_s)
            latency = self.telemetry.stop_timer(start_time)

            if not raw_output:
                logger.warning(f"[Router] CLOUD failed. Falling back to LOCAL for {state.symbol}")
                provider_used = "local_fallback"
                start_time = self.telemetry.start_timer()
                raw_output = self.local.chat(system_prompt, user_prompt, timeout_s)
                latency = self.telemetry.stop_timer(start_time)
        else:
            logger.info(f"[Router] Routing {state.symbol} to LOCAL primary")
            provider_used = "local"
            start_time = self.telemetry.start_timer()
            raw_output = self.local.chat(system_prompt, user_prompt, timeout_s)
            latency = self.telemetry.stop_timer(start_time)

        # 3. Handle total failure
        if not raw_output:
            logger.error(f"[Router] All providers failed for {state.symbol}. Outputting NO_TRADE.")
            decision = DecisionValidator.fallback_no_trade()
            self.telemetry.record_failure(state.symbol, provider_used, "all_providers_failed", latency)
            return decision

        # 4. Validate output
        decision, err_msg = self.validator.validate(raw_output, state)
        if err_msg:
            logger.warning(f"[Router] Validation failed: {err_msg}. Outputting fallback NO_TRADE.")
            decision = DecisionValidator.fallback_no_trade()
            self.telemetry.record_failure(state.symbol, provider_used, f"validation_error: {err_msg}", latency)
            return decision

        # 5. Populate Cache & telemetry logs
        self.cache.set(state, system_prompt, user_prompt, decision)
        self.telemetry.record_success(state.symbol, provider_used, decision, latency)
        return decision


def build_router(
    cloud_host: str = "https://api.ollama.com",
    cloud_model: str = "qwen3.5:cloud",
    cloud_api_key: str = "",
    local_host: str = "http://localhost:11434",
    local_model: str = "qwen3.5:4b",
    intraday_cache_ttl: int = 45,
    swing_cache_ttl: int = 300,
) -> LLMRouter:
    """Factory: build a fully wired LLMRouter with default settings."""
    from .providers.ollama_cloud import OllamaCloudProvider
    from .providers.ollama_local import OllamaLocalProvider
    from .cache import LLMCache
    from .telemetry import LLMTelemetry
    from .validators.decision_schema import DecisionValidator

    return LLMRouter(
        cloud_provider=OllamaCloudProvider(host=cloud_host, model=cloud_model, api_key=cloud_api_key),
        local_provider=OllamaLocalProvider(host=local_host, model=local_model),
        cache=LLMCache(intraday_ttl_s=intraday_cache_ttl, swing_ttl_s=swing_cache_ttl),
        telemetry=LLMTelemetry(),
        validator=DecisionValidator(),
    )
