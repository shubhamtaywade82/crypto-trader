from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import time

@dataclass
class LLMAdvice:
    confidence: float = 0.5
    veto_reason: Optional[str] = None
    timestamp: float = 0.0
    latency_ms: float = 0.0

class OllamaAdvisor:
    def __init__(self, timeout: int = 15):
        self.timeout = timeout

    def advise(self, prompt: str) -> LLMAdvice:
        t0 = time.time()
        return LLMAdvice(confidence=0.5, veto_reason=None, timestamp=time.time(), latency_ms=(time.time()-t0)*1000)
