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
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from enum import Enum

import requests
import pandas as pd

from crypto_trader.risk import LLMCircuitBreaker
from crypto_trader.structure import MarketStructureAnalyzer

logger = logging.getLogger("crypto_trader.llm")

DATA_DIR = Path.home() / ".crypto_trader"
DATA_DIR.mkdir(exist_ok=True)

# ── Configuration ──
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:4b")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")

# ── Cloud vs Local Toggle ──
USE_CLOUD_LLM = os.getenv("USE_CLOUD_LLM", "false").lower() in ("true", "1", "yes")

# Separate Cloud Config to allow easy switching
CLOUD_OLLAMA_HOST = os.getenv("CLOUD_OLLAMA_HOST", "https://ollama.com")
CLOUD_OLLAMA_MODEL = os.getenv("CLOUD_OLLAMA_MODEL", "deepseek-v3:cloud")
CLOUD_OLLAMA_API_KEY = os.getenv("CLOUD_OLLAMA_API_KEY", "")

USE_OPENAI_FORMAT = os.getenv("USE_OPENAI_FORMAT", "false").lower() in ("true", "1", "yes")
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


SYSTEM_PROMPT = """You are a quantitative crypto futures risk advisor specializing in Smart Money Concepts (SMC) and Price Action (PA) analysis.
You analyze market structure data and return ONLY a JSON object. No markdown, no explanation.

You are NOT a trader. You do NOT predict direction. You assess:
1. Uncertainty — is the setup clear or ambiguous?
2. Risk — are there traps, fakeouts, or liquidation clusters?
3. Context — funding, OI, and market structure alignment.

Strategy Context (SOL 10x Snap Playbook):
You act as a strict risk filter for an automated trading engine executing 10x leverage trades. The bot handles entries/exits autonomously based on these parameters:
- Intraday Playbook: Targets +1.0% profit, strict -0.7% stop-loss.
- Swing Playbook: Targets +2.0% profit, strict -1.2% stop-loss.

YOUR MANDATE (VETO CONDITIONS):
1. 4H CHOP: If the 4H market structure is ranging, tangled, or has no clear Swing High/Low structure, you MUST veto (Golden Rule: "No trade if 4H is chop").
2. SMC TRAPS: If there is an active, unmitigated Order Block (OB) or Fair Value Gap (FVG) directly in the path of the trade within the 1.0% take-profit distance, you MUST veto. The trade will likely hit the resistance and reverse into the tight 0.7% stop-loss.
3. MOMENTUM CONTRADICTION: If the recent 15M/1H price action (e.g., bearish engulfing, sweeps) severely contradicts the 4H trend bias, flag risk_level as "high" or "extreme".

SMC & PA Guidelines:
- Swing Highs / Lows form key liquidity boundaries. Sweeps of these swings indicate high reversal probability (liquidity taken).
- Breaks of Structure (BOS) indicate trend continuation.
- Unmitigated Fair Value Gaps (FVG) and Order Blocks (OB) act as powerful magnet support/resistance zones.
- Rejection Pinbars and Engulfing patterns confirm active buy/sell side pressure.

Rules:
- Be conservative. "neutral" is better than a forced wrong call.
- Flag HIGH risk when: extreme opposite funding, sweeps against bias, or trading directly into active unmitigated opposite Order Blocks.
- Veto only when: extreme contradictory conditions (e.g., bullish setup but funding +0.1%, OI collapsing, or bearish market structure).

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


def _build_structure_block(tf_name: str, df: pd.DataFrame) -> str:
    if df is None or len(df) < 20:
        return f"[MARKET STRUCTURE - {tf_name}]\n- Insufficient data"

    analyzer = MarketStructureAnalyzer(df, swing_window=3)
    struct = analyzer.analyze()
    lines = []

    # EMAs Trend
    close = df["close"]
    ema9 = close.ewm(span=9, adjust=False).mean().iloc[-1]
    ema21 = close.ewm(span=21, adjust=False).mean().iloc[-1]
    ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1] if len(close) >= 200 else close.ewm(span=len(close), adjust=False).mean().iloc[-1]
    last_close = close.iloc[-1]

    if last_close > ema9 > ema21 > ema200:
        trend = "Strongly Bullish (EMA9 > EMA21 > EMA200)"
    elif last_close < ema9 < ema21 < ema200:
        trend = "Strongly Bearish (EMA9 < EMA21 < EMA200)"
    elif last_close > ema200:
        trend = "Bullish Core (Price above EMA200)"
    else:
        trend = "Bearish Core (Price below EMA200)"
    lines.append(f"- Trend Alignment: {trend}")

    # BOS
    recent_bos = struct.get("recent_bos")
    if recent_bos:
        bos_type = recent_bos["type"]
        candles_ago = recent_bos.get("candles_ago", 0)
        lines.append(f"- Swing Structure: {bos_type} Break of Structure (BOS) occurred {candles_ago} candles ago at {recent_bos['swing_price']:.2f} (Break price: {recent_bos['break_price']:.2f})")
    else:
        lines.append("- Swing Structure: Range Bound / Confined")

    # Sweeps
    recent_sweep = struct.get("recent_sweep")
    if recent_sweep:
        sweep_type = recent_sweep["type"]
        candles_ago = recent_sweep.get("candles_ago", 0)
        lines.append(f"- Liquidity Sweep: {sweep_type} sweep detected {candles_ago} candles ago at {recent_sweep['swing_price']:.2f} (Wick swept to: {recent_sweep['swept_price']:.2f})")

    # FVGs
    unmitigated_fvgs = struct.get("unmitigated_fvgs", [])
    if unmitigated_fvgs:
        fvg_strs = [f"{f['type']} FVG ({f['bottom']:.2f}-{f['top']:.2f})" for f in unmitigated_fvgs]
        lines.append(f"- Fair Value Gaps: Active {', '.join(fvg_strs)} (Unmitigated)")
    else:
        lines.append("- Fair Value Gaps: No active unmitigated FVGs")

    # OBs
    unmitigated_obs = struct.get("unmitigated_order_blocks", [])
    if unmitigated_obs:
        ob_strs = [f"{o['type']} OB ({o['low']:.2f}-{o['high']:.2f})" for o in unmitigated_obs]
        lines.append(f"- Order Blocks: Active {', '.join(ob_strs)} (Unmitigated)")
    else:
        lines.append("- Order Blocks: No unmitigated Order Blocks")

    # Price Action
    pa_signals = struct.get("price_action_signals", [])
    if pa_signals:
        pa_strs = [f"{sig['pattern']} ({sig['candle']} candle)" for sig in pa_signals]
        lines.append(f"- Price Action: Recent signals: {', '.join(pa_strs)}")

    block = f"[MARKET STRUCTURE - {tf_name}]\n" + "\n".join(lines)
    return block


def build_prompt(
    symbol: str,
    df_5m: pd.DataFrame,
    df_15m: pd.DataFrame,
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
    """Build rich, structured prompt with pre-calculated SMC & Price Action market structure context."""

    blocks = []
    if df_5m is not None:
        blocks.append(_build_structure_block("5M", df_5m))
    if df_15m is not None:
        blocks.append(_build_structure_block("15M", df_15m))
    if df_1h is not None:
        blocks.append(_build_structure_block("1H", df_1h))
    if df_4h is not None:
        blocks.append(_build_structure_block("4H", df_4h))

    change_24h = (mark_price - df_1h["close"].iloc[-24]) / df_1h["close"].iloc[-24] * 100 if df_1h is not None and len(df_1h) >= 24 else 0

    pos_ctx = "No open positions."
    if open_positions:
        p = open_positions[0]
        pos_ctx = f"Open {p['side']} from {p['entry_price']:.2f}, U-PnL: {p['unrealized_pnl']:.2f}"

    structure_text = "\n\n".join(blocks)

    return f"""Analyze {symbol} and return ONLY JSON.

    Price: {mark_price:.2f} | 24h: {change_24h:+.2f}% | Regime: {regime} (score={regime_score:.2f})
    Funding: {funding_rate:+.4f}% | OI Delta 24h: {oi_delta:+.1f}% | Taker Buy Ratio: {taker_ratio:.2f}

    {structure_text}

    Positions: {pos_ctx}

    Return ONLY JSON."""


class LLMJournal:
    """Appends all LLM interactions to a daily log file for analysis and optimization."""

    def __init__(self):
        self.dir = DATA_DIR / "llm_logs"
        self.dir.mkdir(exist_ok=True)
        self._lock = threading.Lock()

    def log(self, symbol: str, prompt: str, response: str, advice: Optional[LLMAdvice]):
        """Write interaction to daily .jsonl file."""
        with self._lock:
            try:
                date_str = datetime.now().strftime("%Y-%m-%d")
                path = self.dir / f"llm_{date_str}.jsonl"

                entry = {
                    "timestamp": datetime.now().isoformat(),
                    "symbol": symbol,
                    "prompt": prompt,
                    "raw_response": response,
                    "parsed_advice": advice.to_dict() if advice else None
                }

                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
            except Exception as e:
                logger.warning(f"[LLM Journal] Failed to log interaction: {e}")


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


_OLLAMA_GLOBAL_LOCK = threading.Lock()

class OllamaClient:
    def __init__(
        self,
        host: str = OLLAMA_HOST,
        model: str = OLLAMA_MODEL,
        api_key: str = OLLAMA_API_KEY,
        use_openai: bool = USE_OPENAI_FORMAT,
        timeout: int = OLLAMA_TIMEOUT,
        max_tokens: int = OLLAMA_MAX_TOKENS
    ):
        self.host = host.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.use_openai = use_openai
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.session = requests.Session()
        
        if self.api_key:
            self.session.headers.update({"Authorization": f"Bearer {self.api_key}"})

    def is_available(self) -> bool:
        # For cloud hosts (ollama.com or OpenAI-compatible), we prioritize the API key presence
        is_cloud = "ollama.com" in self.host or self.use_openai
        if is_cloud:
            return self.api_key != ""
            
        try:
            # Local Ollama check
            return self.session.get(f"{self.host}/api/tags", timeout=5).status_code == 200
        except Exception:
            return self.api_key != "" # Fallback for remote native Ollama with auth

    def generate(self, prompt: str, temperature: float = 0.3) -> Optional[str]:
        if self.use_openai:
            return self._generate_openai(prompt, temperature)
        return self._generate_ollama(prompt, temperature)

    def _generate_ollama(self, prompt: str, temperature: float = 0.3) -> Optional[str]:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": SYSTEM_PROMPT,
            "stream": False,
            "think": False,
            "options": {"temperature": temperature, "num_predict": self.max_tokens},
        }
        with _OLLAMA_GLOBAL_LOCK:
            try:
                resp = self.session.post(f"{self.host}/api/generate", json=payload, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                response_text = data.get("response", "").strip()
                if not response_text and "thinking" in data:
                    response_text = data["thinking"].strip()
                return response_text
            except requests.Timeout:
                logger.warning("Ollama request timed out")
                return None
            except Exception as e:
                logger.warning(f"Ollama request failed: {e}")
                return None

    def _generate_openai(self, prompt: str, temperature: float = 0.3) -> Optional[str]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            "temperature": temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        try:
            resp = self.session.post(f"{self.host}/v1/chat/completions", json=payload, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"Cloud LLM request failed: {e}")
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
        api_key: str = OLLAMA_API_KEY,
        use_openai: bool = USE_OPENAI_FORMAT,
        timeout: int = OLLAMA_TIMEOUT,
        cache_ttl: int = CACHE_TTL,
        max_failures: int = 5,
        log_llm: bool = False,
    ):
        self.client = OllamaClient(
            host=host,
            model=model,
            api_key=api_key,
            use_openai=use_openai,
            timeout=timeout
        )
        self.parser = LLMResponseParser()
        self.cache = LLMCache(ttl=cache_ttl)
        self.journal = LLMJournal()
        self.circuit_breaker = LLMCircuitBreaker(max_failures=max_failures)
        self.log_llm = log_llm or os.getenv("LOG_LLM", "false").lower() in ("true", "1", "yes")
        self._last_advice: Optional[LLMAdvice] = None
        self._lock = threading.Lock()

    def is_ready(self) -> bool:
        return self.client.is_available() and self.circuit_breaker.can_use()

    def get_advice(
        self,
        symbol: str,
        df_5m: pd.DataFrame,
        df_15m: pd.DataFrame,
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

        prompt = build_prompt(symbol, df_5m, df_15m, df_1h, df_4h, regime, regime_score, mark_price,
                              funding_rate, oi_delta, taker_ratio, open_positions)

        if not force_refresh:
            cached = self.cache.get(symbol, prompt)
            if cached:
                if self.log_llm:
                    logger.info(f"[LLM Cache Hit] Reused cached advice for {symbol}")
                with self._lock:
                    self._last_advice = cached
                return cached

        if self.log_llm:
            logger.info(f"[LLM Prompt] Sent to model '{self.client.model}':\n{prompt}\n" + "="*50)

        start = time.perf_counter()
        raw = self.client.generate(prompt)
        latency_ms = (time.perf_counter() - start) * 1000

        if raw is None:
            self.circuit_breaker.record_failure()
            with self._lock:
                return self._last_advice

        if self.log_llm:
            resp_str = str(raw)
            if resp_str and len(resp_str) > 1000:
                resp_str = resp_str[:1000] + "... (truncated)"
            logger.info(f"[LLM Response] Received raw output:\n{resp_str}\n" + "="*50)

        advice = self.parser.parse(raw, symbol)
        if advice is None:
            self.circuit_breaker.record_failure()
            with self._lock:
                return self._last_advice

        advice.latency_ms = latency_ms
        self.circuit_breaker.record_success()
        self.cache.set(symbol, prompt, advice)
        self.journal.log(symbol, prompt, raw, advice)

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
