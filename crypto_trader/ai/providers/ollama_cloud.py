"""crypto_trader.ai.providers.ollama_cloud — Adapter for cloud qwen3.5 OpenAI-compatible chat endpoint."""

import requests
import logging
from typing import Optional, List, Union

from crypto_trader.ai.providers.base import LLMProvider
from crypto_trader.ai.providers.rotation import ResourceRotator
from crypto_trader.patterns.circuit_breaker import CircuitBreaker

logger = logging.getLogger("crypto_trader.ai.providers.ollama_cloud")


class OllamaCloudProvider:
    def __init__(
        self,
        host: str = "https://api.ollama.com",
        model: str = "qwen3.5:cloud",
        api_key: Union[str, List[str]] = "",
        cooldown_s: int = 60,
        cb_threshold: int = 3,
        cb_recovery_s: int = 60,
    ):
        self.host = host.rstrip("/")
        self.model = model
        
        # Parse multiple keys if provided
        if isinstance(api_key, str):
            api_keys = [k.strip() for k in api_key.split(",") if k.strip()]
        else:
            api_keys = list(api_key) if api_key else []
            
        self.rotator = ResourceRotator(api_keys, cooldown_s=cooldown_s)
        self.circuit_breaker = CircuitBreaker(
            name="ollama_cloud",
            failure_threshold=cb_threshold,
            recovery_timeout=cb_recovery_s,
        )
        self.session = requests.Session()

    def health(self) -> bool:
        # Requires at least one available API key and circuit breaker not open
        return self.rotator.has_available() and not self.circuit_breaker.is_open

    def chat(self, system_prompt: str, user_prompt: str, timeout_s: int) -> Optional[str]:
        if self.circuit_breaker.is_open:
            logger.warning("[OllamaCloud] Circuit breaker is OPEN. Call rejected.")
            return None

        if self.rotator.total_configured() == 0:
            logger.error("[OllamaCloud] No API keys configured")
            return None

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "stream": False,
        }

        # Try rotating keys up to total_configured times
        for attempt in range(self.rotator.total_configured()):
            key, idx = self.rotator.get_next_available()
            if key is None:
                logger.warning("[OllamaCloud] All API keys are on cooldown")
                self.circuit_breaker._on_failure()
                return None

            # Masked key logging
            masked_key = f"{key[:4]}****{key[-4:]}" if len(key) > 8 else "****"
            logger.info(f"[OllamaCloud] Attempting request using key index {idx} ({masked_key})")

            # Update session authorization headers
            self.session.headers.update({"Authorization": f"Bearer {key}"})

            try:
                url = f"{self.host}/v1/chat/completions"
                resp = self.session.post(url, json=payload, timeout=timeout_s)
                
                # If rate limited (429) or unauthorized (401/403), rotate/cooldown and retry
                if resp.status_code in (401, 403, 429):
                    logger.warning(
                        "[OllamaCloud] Key %d failed with status %d. Putting on cooldown...",
                        idx, resp.status_code
                    )
                    self.rotator.record_failure(idx)
                    continue
                        
                resp.raise_for_status()
                # If success, clear key cooldown and circuit breaker failure count
                self.rotator.record_success(idx)
                self.circuit_breaker._on_success()
                return resp.json()["choices"][0]["message"]["content"]
            except Exception as e:
                logger.warning("[OllamaCloud] Request failed with key %d: %s. Putting on cooldown...", idx, e)
                self.rotator.record_failure(idx)
                continue

        # If all keys failed, register a failure on the circuit breaker
        self.circuit_breaker._on_failure()
        return None
