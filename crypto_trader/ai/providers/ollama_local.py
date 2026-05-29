"""crypto_trader.ai.providers.ollama_local — Adapter for local qwen3.5:4b chat endpoint."""

import requests
import logging
from typing import Optional, Union, Dict, Any, List

from crypto_trader.ai.providers.base import LLMProvider
from crypto_trader.ai.providers.rotation import ResourceRotator
from crypto_trader.patterns.circuit_breaker import CircuitBreaker

logger = logging.getLogger("crypto_trader.ai.providers.ollama_local")


class OllamaLocalProvider:
    def __init__(
        self,
        host: Union[str, List[str]] = "http://localhost:11434",
        model: str = "qwen3.5:4b",
        num_predict: int = 1536,
        json_schema: Optional[Dict[str, Any]] = None,
        cooldown_s: int = 60,
        cb_threshold: int = 3,
        cb_recovery_s: int = 60,
    ):
        self.model = model
        self.num_predict = num_predict
        self.response_format: Union[str, Dict[str, Any]] = json_schema or "json"
        
        # Parse multiple hosts if provided
        if isinstance(host, str):
            hosts = [h.strip().rstrip("/") for h in host.split(",") if h.strip()]
        else:
            hosts = [h.rstrip("/") for h in host] if host else ["http://localhost:11434"]
            
        self.rotator = ResourceRotator(hosts, cooldown_s=cooldown_s)
        self.circuit_breaker = CircuitBreaker(
            name="ollama_local",
            failure_threshold=cb_threshold,
            recovery_timeout=cb_recovery_s,
        )
        self.session = requests.Session()

    def health(self) -> bool:
        if self.circuit_breaker.is_open:
            return False

        if self.rotator.total_configured() == 0:
            return False

        # Try to ping the first available host that is not on cooldown
        now_available = []
        # We don't want to modify current_idx or trigger proactive rotation during health check,
        # so we inspect availability manually.
        for idx, host in enumerate(self.rotator.items):
            import time
            if time.time() >= self.rotator.cooldown_until[idx]:
                now_available.append((host, idx))

        if not now_available:
            return False

        # Ping the first available one to verify actual connectivity
        for host, idx in now_available[:2]:  # Limit to 2 pings to prevent blocking too long
            try:
                resp = self.session.get(f"{host}/api/tags", timeout=2)
                if resp.status_code == 200:
                    return True
                else:
                    self.rotator.record_failure(idx)
            except Exception:
                self.rotator.record_failure(idx)

        return False

    def chat(self, system_prompt: str, user_prompt: str, timeout_s: int) -> Optional[str]:
        if self.circuit_breaker.is_open:
            logger.warning("[OllamaLocal] Circuit breaker is OPEN. Call rejected.")
            return None

        if self.rotator.total_configured() == 0:
            logger.error("[OllamaLocal] No hosts configured")
            return None

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "format": self.response_format,
            "think": False,  # disable thinking tokens
            "options": {"temperature": 0.2, "num_predict": self.num_predict},
        }

        # Try rotating hosts up to total_configured times
        for attempt in range(self.rotator.total_configured()):
            host, idx = self.rotator.get_next_available()
            if host is None:
                logger.warning("[OllamaLocal] All hosts are on cooldown")
                self.circuit_breaker._on_failure()
                return None

            logger.info(f"[OllamaLocal] Attempting request using host index {idx} ({host})")

            try:
                resp = self.session.post(f"{host}/api/chat", json=payload, timeout=timeout_s)
                resp.raise_for_status()
                # On success, clear cooldown and circuit breaker failure count
                self.rotator.record_success(idx)
                self.circuit_breaker._on_success()
                return resp.json()["message"]["content"]
            except Exception as e:
                logger.warning("[OllamaLocal] Request failed on host %d (%s): %s. Putting on cooldown...", idx, host, e)
                self.rotator.record_failure(idx)
                continue

        # If all hosts failed, record a failure on the circuit breaker
        self.circuit_breaker._on_failure()
        return None
