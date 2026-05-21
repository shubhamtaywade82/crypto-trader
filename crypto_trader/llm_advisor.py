"""
crypto_trader.llm_advisor — Ollama LLM Integration
====================================================
Advisory-only LLM layer with:
    • Weighted confidence fusion (not binary veto)
    • Stale advice rejection
    • Latency budgeting
    • Circuit breaker
    • Disk cache with TTL

The LLM never places orders. It modifies technical confidence scores.
"""

import os
import json
import time
import logging
import hashlib
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from enum import Enum

import requests
import pandas as pd

from crypto_trader.risk import LLMCircuitBreaker

logger = logging.getLogger("crypto_trader.llm")

DATA_DIR = Path.home() / ".crypto_trader"
DATA_DIR.mkdir(exist_ok=True)

# ── Configuration ──
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:4b")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "320"))
OLLAMA_MAX_TOKENS = int(os.getenv("OLLAMA_MAX_TOKENS", "512"))
CACHE_TTL = int(os.getenv("LLM_CACHE_TTL", "1800"))
MAX_LLM_AGE_SECONDS = int(os.getenv("LLM_MAX_AGE", "20"))
LLM_MAX_LATENCY_MS = int(os.getenv("LLM_MAX_LATENCY", "3000"))
LLM_WEIGHT = float(os.getenv("LLM_WEIGHT", "0.20"))
FINAL_SCORE_THRESHOLD = float(os.getenv("FINAL_SCORE_THRESHOLD", "0.75"))


class LLMBias(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    UNCLEAR = "unclear"


class LLMRiskLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTREME = "extreme"


@dataclass
class LLMAdvice:
    timestamp: int
    symbol: str
    bias: LLMBias
    confidence: float          # 0.0 – 1.0
    sentiment_score: float     # -1.0 to +1.0
    risk_level: LLMRiskLevel
    key_factors: List[str]
    recommended_bias: str      # "long_only", "short_only", "any", "none"
    technical_alignment: float # -1.0 to +1.0
    veto: bool = False
    veto_reason: Optional[str] = None
    latency_ms: Optional[float] = None
    raw_response: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "bias": self.bias.value,
            "confidence": round(self.confidence, 3),
            "sentiment_score": round(self.sentiment_score, 3),
            "risk_level": self.risk_level.value,
            "key_factors": self.key_factors,
            "recommended_bias": self.recommended_bias,
            "technical_alignment": round(self.technical_alignment, 3),
            "veto": self.veto,
            "veto_reason": self.veto_reason,
            "latency_ms": self.latency_ms,
        }


SYSTEM_PROMPT = """You are a quantitative crypto futures risk advisor.
You analyze market data and return ONLY a JSON object. No markdown, no explanation.

You are NOT a trader. You do NOT predict direction. You assess:
1. Uncertainty — is the setup clear or ambiguous?
2. Risk — are there traps, fakeouts, or liquidation clusters?
3. Context — funding, OI, and sentiment alignment.

Rules:
- Be conservative. "neutral" is better than a forced wrong call.
- Flag HIGH risk when: declining volume on breakout, RSI divergence, extreme funding.
- Veto only when: extreme contradictory conditions (e.g., bullish setup but funding +0.1%, OI collapsing).

Output JSON:
{
  "bias": "bullish" | "bearish" | "neutral" | "unclear",
  "confidence": 0.0 to 1.0,
  "sentiment_score": -1.0 to 1.0,
  "risk_level": "low" | "medium" | "high" | "extreme",
  "key_factors": ["factor 1", "factor 2"],
  "recommended_bias": "long_only" | "short_only" | "any" | "none",
  "technical_alignment": -1.0 to 1.0,
  "veto_reason": null | "specific reason"
}"""


def build_prompt(
    symbol: str,
    df_1h: pd.DataFrame,
    df_4h: pd.DataFrame,
    regime: str,
    regime_score: float,
    mark_price: float,
    funding_rate: float,
    oi_delta: float,
    taker_ratio: float,
    open_positions: List[dict],
) -> str:
    """Build rich, structured prompt with derivatives context."""

    recent_1h = df_1h.tail(6)
    h1_lines = []
    for _, row in recent_1h.iterrows():
        d = "▲" if row["close"] > row["open"] else "▼"
        h1_lines.append(
            f"{d} O:{row['open']:.2f} H:{row['high']:.2f} L:{row['low']:.2f} C:{row['close']:.2f} V:{row['quote_volume']:.0f}"
        )

    recent_4h = df_4h.tail(4)
    h4_lines = []
    for _, row in recent_4h.iterrows():
        d = "▲" if row["close"] > row["open"] else "▼"
        h4_lines.append(
            f"{d} O:{row['open']:.2f} H:{row['high']:.2f} L:{row['low']:.2f} C:{row['close']:.2f} V:{row['quote_volume']:.0f}"
        )

    change_24h = (mark_price - df_1h["close"].iloc[-24]) / df_1h["close"].iloc[-24] * 100 if len(df_1h) >= 24 else 0

    pos_ctx = "No open positions."
    if open_positions:
        p = open_positions[0]
        pos_ctx = f"Open {p['side']} from {p['entry_price']:.2f}, U-PnL: {p['unrealized_pnl']:.2f}"

    h1_block = "\n".join(h1_lines)
    h4_block = "\n".join(h4_lines)

    return f"""Analyze {symbol} and return ONLY JSON.

Price: {mark_price:.2f} | 24h: {change_24h:+.2f}% | Regime: {regime} (score={regime_score:.2f})
Funding: {funding_rate:+.4f}% | OI Delta 24h: {oi_delta:+.1f}% | Taker Buy Ratio: {taker_ratio:.2f}

Last 6 x 1H:
{h1_block}

Last 4 x 4H:
{h4_block}

Positions: {pos_ctx}

Return ONLY JSON."""



class LLMCache:
    """File-based cache with TTL and cleanup."""

    def __init__(self, ttl: int = CACHE_TTL):
        self.ttl = ttl
        self.dir = DATA_DIR / "llm_cache"
        self.dir.mkdir(exist_ok=True)
        self._cleanup()

    def _key(self, symbol: str, prompt: str) -> str:
        return hashlib.sha256(f"{symbol}:{prompt}".encode()).hexdigest()[:16]

    def _cleanup(self):
        """Delete cache files older than 7 days."""
        cutoff = time.time() - (7 * 24 * 3600)
        for f in self.dir.glob("*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink()

    def get(self, symbol: str, prompt: str) -> Optional[LLMAdvice]:
        key = self._key(symbol, prompt)
        path = self.dir / f"{symbol}_{key}.json"
        if not path.exists():
            return None
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if time.time() - data.get("cached_at", 0) > self.ttl:
                return None
            return LLMAdvice(
                timestamp=data["timestamp"],
                symbol=data["symbol"],
                bias=LLMBias(data["bias"]),
                confidence=data["confidence"],
                sentiment_score=data["sentiment_score"],
                risk_level=LLMRiskLevel(data["risk_level"]),
                key_factors=data["key_factors"],
                recommended_bias=data["recommended_bias"],
                technical_alignment=data["technical_alignment"],
                veto=data["veto"],
                veto_reason=data.get("veto_reason"),
                latency_ms=data.get("latency_ms"),
                raw_response=data.get("raw_response", "")[:500],
            )
        except Exception:
            return None

    def set(self, symbol: str, prompt: str, advice: LLMAdvice):
        key = self._key(symbol, prompt)
        path = self.dir / f"{symbol}_{key}.json"
        data = advice.to_dict()
        data["cached_at"] = int(time.time())
        data["raw_response"] = advice.raw_response[:500]
        with open(path, "w") as f:
            json.dump(data, f, indent=2)


class OllamaClient:
    def __init__(self, host: str = OLLAMA_HOST, model: str = OLLAMA_MODEL,
                 timeout: int = OLLAMA_TIMEOUT, max_tokens: int = OLLAMA_MAX_TOKENS):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.session = requests.Session()

    def is_available(self) -> bool:
        try:
            return self.session.get(f"{self.host}/api/tags", timeout=5).status_code == 200
        except Exception:
            return False

    def generate(self, prompt: str, temperature: float = 0.3) -> Optional[str]:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": SYSTEM_PROMPT,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": self.max_tokens},
        }
        try:
            resp = self.session.post(f"{self.host}/api/generate", json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json().get("response", "").strip()
        except requests.Timeout:
            logger.warning("Ollama request timed out")
            return None
        except Exception as e:
            logger.warning(f"Ollama request failed: {e}")
            return None


class LLMResponseParser:
    @staticmethod
    def parse(raw: str, symbol: str) -> Optional[LLMAdvice]:
        if not raw:
            return None
        text = raw
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from LLM: {raw[:200]}")
            return None

        bias_str = data.get("bias", "unclear").lower()
        bias = LLMBias(bias_str) if bias_str in [e.value for e in LLMBias] else LLMBias.UNCLEAR
        conf = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
        score = max(-1.0, min(1.0, float(data.get("sentiment_score", 0.0))))
        risk_str = data.get("risk_level", "medium").lower()
        risk = LLMRiskLevel(risk_str) if risk_str in [e.value for e in LLMRiskLevel] else LLMRiskLevel.MEDIUM
        factors = data.get("key_factors", [])
        if not isinstance(factors, list):
            factors = [str(factors)]
        rec = data.get("recommended_bias", "any")
        if rec not in ("long_only", "short_only", "any", "none"):
            rec = "any"
        align = max(-1.0, min(1.0, float(data.get("technical_alignment", 0.0))))
        veto_reason = data.get("veto_reason")
        veto = veto_reason is not None and str(veto_reason).strip() != ""

        return LLMAdvice(
            timestamp=int(time.time()),
            symbol=symbol,
            bias=bias,
            confidence=conf,
            sentiment_score=score,
            risk_level=risk,
            key_factors=factors[:5],
            recommended_bias=rec,
            technical_alignment=align,
            veto=veto,
            veto_reason=veto_reason,
            raw_response=raw,
        )


class OllamaAdvisor:
    """High-level advisor with async support, cache, and circuit breaker."""

    def __init__(
        self,
        host: str = OLLAMA_HOST,
        model: str = OLLAMA_MODEL,
        timeout: int = OLLAMA_TIMEOUT,
        cache_ttl: int = CACHE_TTL,
        max_failures: int = 5,
    ):
        self.client = OllamaClient(host=host, model=model, timeout=timeout)
        self.parser = LLMResponseParser()
        self.cache = LLMCache(ttl=cache_ttl)
        self.circuit_breaker = LLMCircuitBreaker(max_failures=max_failures)
        self._last_advice: Optional[LLMAdvice] = None
        self._lock = threading.Lock()

    def is_ready(self) -> bool:
        return self.client.is_available() and self.circuit_breaker.can_use()

    def get_advice(
        self,
        symbol: str,
        df_1h: pd.DataFrame,
        df_4h: pd.DataFrame,
        regime: str,
        regime_score: float,
        mark_price: float,
        funding_rate: float,
        oi_delta: float,
        taker_ratio: float,
        open_positions: List[dict],
        force_refresh: bool = False,
    ) -> Optional[LLMAdvice]:
        if not self.circuit_breaker.can_use():
            logger.warning("[LLM] Circuit breaker active. LLM disabled.")
            return None

        prompt = build_prompt(symbol, df_1h, df_4h, regime, regime_score, mark_price,
                              funding_rate, oi_delta, taker_ratio, open_positions)

        if not force_refresh:
            cached = self.cache.get(symbol, prompt)
            if cached:
                with self._lock:
                    self._last_advice = cached
                return cached

        start = time.perf_counter()
        raw = self.client.generate(prompt)
        latency_ms = (time.perf_counter() - start) * 1000

        if raw is None:
            self.circuit_breaker.record_failure()
            with self._lock:
                return self._last_advice

        advice = self.parser.parse(raw, symbol)
        if advice is None:
            self.circuit_breaker.record_failure()
            with self._lock:
                return self._last_advice

        advice.latency_ms = latency_ms
        self.circuit_breaker.record_success()
        self.cache.set(symbol, prompt, advice)

        with self._lock:
            self._last_advice = advice

        logger.info(
            f"[LLM] {symbol} | {advice.bias.value} | conf={advice.confidence:.2f} | "
            f"score={advice.sentiment_score:+.2f} | risk={advice.risk_level.value} | "
            f"lat={latency_ms:.0f}ms | veto={advice.veto}"
        )
        return advice

    def get_advice_async(self, **kwargs) -> threading.Thread:
        def _worker():
            self.get_advice(**kwargs)
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return t

    def get_last_advice(self) -> Optional[LLMAdvice]:
        with self._lock:
            return self._last_advice

    def compute_final_score(
        self,
        technical_score: float,
        regime: str,
        regime_score: float,
        advice: Optional[LLMAdvice],
    ) -> Tuple[float, float, str]:
        """
        Weighted fusion: technical * (1-w) + LLM * w.
        Returns (final_score, llm_weight, explanation).
        """
        # Regime-based LLM gating
        if regime in ("TRENDING_UP", "TRENDING_DOWN") and regime_score >= 0.85:
            llm_weight = 0.0
            explanation = f"Strong trend (score={regime_score:.2f}), LLM bypassed"
        elif advice is None:
            llm_weight = 0.0
            explanation = "No LLM advice available"
        elif advice.latency_ms and advice.latency_ms > LLM_MAX_LATENCY_MS:
            llm_weight = 0.0
            explanation = f"LLM too slow ({advice.latency_ms:.0f}ms), ignored"
        elif time.time() - advice.timestamp > MAX_LLM_AGE_SECONDS:
            llm_weight = 0.0
            explanation = f"LLM advice stale ({time.time() - advice.timestamp:.0f}s), ignored"
        else:
            llm_weight = LLM_WEIGHT
            # Adjust weight by confidence
            llm_weight *= advice.confidence
            explanation = f"LLM active (weight={llm_weight:.2f}, conf={advice.confidence:.2f})"

        tech_component = technical_score * (1 - llm_weight)
        llm_component = (advice.confidence if advice else 0.0) * llm_weight
        # For LLM component, use confidence as a positive modifier only
        # If LLM disagrees strongly, it reduces the score
        if advice and advice.sentiment_score < -0.5:
            llm_component *= 0.5  # Halve the contribution when bearish

        final_score = tech_component + llm_component
        return round(final_score, 3), llm_weight, explanation
