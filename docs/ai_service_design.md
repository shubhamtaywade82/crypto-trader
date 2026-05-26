# AI Advisory Subsystem — Python Service Design

This document specifies the concrete Python service design for the decoupled AI Advisory subsystem. The system implements a two-provider adaptive router (Cloud primary, Local fallback) with strict output contract schema validation, caching, and telemetry.

---

## 📂 Subsystem Directory Layout

```text
crypto_trader/
  ai/
    __init__.py
    schemas.py             # Pydantic state & decision schemas
    state_builder.py       # Summarizes DataFrame & indicator state
    router.py              # Adaptive LLM routing & fallback logic
    cache.py               # sha256 prompt-keyed disk/memory cache
    telemetry.py           # Structured interaction logging & latency tracking
    prompts/
      system.txt           # Master system prompt
      intraday.txt         # Intraday specific trade instruction directives
      swing.txt            # Swing specific trade instruction directives
      review.txt           # Post-trade review instructions
    providers/
      __init__.py
      base.py              # Normalized LLMProvider interface
      ollama_local.py      # Adapter for local qwen3.5:4b chat endpoint
      ollama_cloud.py      # Adapter for cloud qwen3.5:cloud OpenAI-compatible chat endpoint
    validators/
      __init__.py
      decision_schema.py   # Output validator & post-LLM rule checks
```

---

## 📄 1. Schemas (`crypto_trader/ai/schemas.py`)

Using Pydantic models compatible with **Python 3.8**:

```python
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

# ──────── Market State Input Contract ────────

class MarketStatePayload(BaseModel):
    symbol: str = Field(..., description="Trading pair (e.g. SOLUSDT)")
    timeframe: str = Field(..., description="Interval (e.g. 15m)")
    mode: str = Field(..., description="Trading mode: 'intraday' or 'swing'")
    price: float = Field(..., description="Current mark price")
    htf_trend: str = Field(..., description="High-timeframe trend (e.g. bullish, bearish, chop)")
    market_structure: str = Field(..., description="SMC Structure (e.g. BOS_UP, CHOCH_UP, RANGE)")
    volatility_regime: str = Field(..., description="Regime classification (e.g. expanding, chop, compressed)")
    funding_rate: float = Field(..., description="Current funding rate percentage")
    open_interest_change: float = Field(..., description="24h open interest change percentage")
    volume_anomaly: bool = Field(..., description="True if volume > 2x standard deviation")
    liquidity_sweep: bool = Field(..., description="True if recent swing high/low swept")
    risk_budget_pct: float = Field(..., description="Available risk allocation slice (0.0 to 1.0)")


# ──────── LLM Decision Output Contract ────────

class EntryZone(BaseModel):
    low: float = Field(..., description="Lower bound of entry region")
    high: float = Field(..., description="Upper bound of entry region")

class LLMDecision(BaseModel):
    action: str = Field(..., description="Trade action: LONG, SHORT, or NO_TRADE")
    confidence: float = Field(..., description="Score from 0.0 (no confidence) to 1.0 (absolute)")
    setup_type: str = Field(..., description="Classification (e.g. Sweep Reversal, BOS Continuation)")
    entry_zone: EntryZone = Field(..., description="Calculated range of entry target prices")
    stop_loss: float = Field(..., description="Strict stop loss invalidation price")
    targets: List[float] = Field(..., description="Target profit scale-out prices")
    risk_reward: float = Field(..., description="Risk-reward ratio (e.g. 2.5)")
    invalidation: str = Field(..., description="Qualitative reasoning for SL placement")
    warnings: List[str] = Field(default_factory=list, description="Advisory warnings or traps detected")
    reason_codes: List[str] = Field(default_factory=list, description="Diagnostic tags (e.g. OB_RESISTANCE, HTF_CHOP)")
```

---

## 📄 2. State Builder (`crypto_trader/ai/state_builder.py`)

Converts indicators and DataFrames into a normalized single snapshot payload:

```python
import pandas as pd
from typing import Optional
from crypto_trader.ai.schemas import MarketStatePayload
from crypto_trader.structure import MarketStructureAnalyzer

class StateBuilder:
    @staticmethod
    def build(
        symbol: str,
        timeframe: str,
        mode: str,
        df_ltf: pd.DataFrame,
        df_htf: pd.DataFrame,
        mark_price: float,
        funding_rate: float,
        oi_delta: float,
        risk_budget: float
    ) -> MarketStatePayload:
        # 1. Analyze market structure using local indicator rule layers
        analyzer = MarketStructureAnalyzer(df_ltf, swing_window=3)
        struct = analyzer.analyze()

        # 2. HTF Trend identification
        ema200 = df_htf["close"].ewm(span=200, adjust=False).mean().iloc[-1]
        htf_trend = "bullish" if mark_price > ema200 else "bearish"

        # 3. Structure string
        recent_bos = struct.get("recent_bos")
        market_structure = "RANGE"
        if recent_bos:
            market_structure = f"{recent_bos['type']}_BOS"

        # 4. Volume anomaly
        vol_std = df_ltf["volume"].rolling(20).std().iloc[-1]
        vol_mean = df_ltf["volume"].rolling(20).mean().iloc[-1]
        volume_anomaly = bool(df_ltf["volume"].iloc[-1] > (vol_mean + 2 * vol_std))

        # 5. Liquidity sweep
        liquidity_sweep = struct.get("recent_sweep") is not None

        return MarketStatePayload(
            symbol=symbol,
            timeframe=timeframe,
            mode=mode,
            price=mark_price,
            htf_trend=htf_trend,
            market_structure=market_structure,
            volatility_regime=struct.get("regime", "chop"),
            funding_rate=funding_rate,
            open_interest_change=oi_delta,
            volume_anomaly=volume_anomaly,
            liquidity_sweep=liquidity_sweep,
            risk_budget_pct=risk_budget
        )
```

---

## 📄 3. Provider Interfaces (`crypto_trader/ai/providers/`)

### Base Provider Protocol (`crypto_trader/ai/providers/base.py`)

```python
from typing import Protocol, Dict
from crypto_trader.ai.schemas import LLMDecision

class LLMProvider(Protocol):
    def health(self) -> bool:
        """Verify the service endpoint is reachable and healthy."""
        ...

    def chat(self, system_prompt: str, user_prompt: str, timeout_s: int) -> Optional[str]:
        """Send message exchange to the provider, returning raw text or JSON."""
        ...
```

### Local Provider Adapter (`crypto_trader/ai/providers/ollama_local.py`)

Connects directly to the local Ollama API:

```python
import requests
from typing import Optional
from crypto_trader.ai.providers.base import LLMProvider

class OllamaLocalProvider(LLMProvider):
    def __init__(self, host: str = "http://localhost:11434", model: str = "qwen3.5:4b"):
        self.host = host.rstrip("/")
        self.model = model
        self.session = requests.Session()

    def health(self) -> bool:
        try:
            return self.session.get(f"{self.host}/api/tags", timeout=3).status_code == 200
        except Exception:
            return False

    def chat(self, system_prompt: str, user_prompt: str, timeout_s: int) -> Optional[str]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2}
        }
        try:
            resp = self.session.post(f"{self.host}/api/chat", json=payload, timeout=timeout_s)
            resp.raise_for_status()
            return resp.json()["message"]["content"]
        except Exception:
            return None
```

### Cloud Provider Adapter (`crypto_trader/ai/providers/ollama_cloud.py`)

Connects to the OpenAI-compatible API gateway for the cloud `qwen3.5` family:

```python
import requests
from typing import Optional
from crypto_trader.ai.providers.base import LLMProvider

class OllamaCloudProvider(LLMProvider):
    def __init__(self, host: str = "https://api.ollama.com", model: str = "qwen3.5:cloud", api_key: str = ""):
        self.host = host.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.session = requests.Session()
        if api_key:
            self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    def health(self) -> bool:
        # Requires API key existence
        return bool(self.api_key)

    def chat(self, system_prompt: str, user_prompt: str, timeout_s: int) -> Optional[str]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "stream": False
        }
        try:
            # OpenAI compatible completions path
            url = f"{self.host}/v1/chat/completions"
            resp = self.session.post(url, json=payload, timeout=timeout_s)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception:
            return None
```

---

## 📄 4. Adaptive Router (`crypto_trader/ai/router.py`)

Implements the single-retry fallback logic, health gating, and latency awareness:

```python
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
        validator: DecisionValidator
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
        timeout_s: int = 10
    ) -> LLMDecision:
        # 1. Cache hit check
        cached_result = self.cache.get(state, system_prompt, user_prompt)
        if cached_result:
            self.telemetry.record_cache_hit(state.symbol)
            return cached_result

        provider_used = "none"
        raw_output = None
        latency = 0.0

        # 2. Route selection
        # Cloud first if healthy & task calls for deep reasoning, fallback to local
        if self.cloud.health() and state.mode == "swing":
            logger.info(f"[Router] Routing {state.symbol} to CLOUD primary")
            provider_used = "cloud"
            start_time = telemetry.start_timer()
            raw_output = self.cloud.chat(system_prompt, user_prompt, timeout_s)
            latency = telemetry.stop_timer(start_time)

            if not raw_output:
                logger.warning(f"[Router] CLOUD failed. Falling back to LOCAL for {state.symbol}")
                provider_used = "local_fallback"
                start_time = telemetry.start_timer()
                raw_output = self.local.chat(system_prompt, user_prompt, timeout_s)
                latency = telemetry.stop_timer(start_time)
        else:
            logger.info(f"[Router] Routing {state.symbol} to LOCAL primary")
            provider_used = "local"
            start_time = telemetry.start_timer()
            raw_output = self.local.chat(system_prompt, user_prompt, timeout_s)
            latency = telemetry.stop_timer(start_time)

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
```

---

## 📄 5. Cache & Deduplication (`crypto_trader/ai/cache.py`)

Prompt and market-state hashed caching layer with TTL:

```python
import json
import hashlib
import time
from typing import Optional
from pathlib import Path
from crypto_trader.ai.schemas import MarketStatePayload, LLMDecision

DATA_DIR = Path.home() / ".crypto_trader"
DATA_DIR.mkdir(exist_ok=True)

class LLMCache:
    def __init__(self, intraday_ttl_s: int = 45, swing_ttl_s: int = 300):
        self.intraday_ttl = intraday_ttl_s
        self.swing_ttl = swing_ttl_s
        self.dir = DATA_DIR / "llm_cache"
        self.dir.mkdir(exist_ok=True)

    def _hash_key(self, state: MarketStatePayload, sys_prompt: str, user_prompt: str) -> str:
        # Standardize state properties for deterministic hashing
        state_serialized = state.json()
        payload = f"{state_serialized}:{sys_prompt}:{user_prompt}"
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, state: MarketStatePayload, sys_prompt: str, user_prompt: str) -> Optional[LLMDecision]:
        key = self._hash_key(state, sys_prompt, user_prompt)
        path = self.dir / f"cache_{key}.json"
        if not path.exists():
            return None

        try:
            with open(path, "r") as f:
                data = json.load(f)

            # Check TTL based on trading mode
            ttl = self.intraday_ttl if state.mode == "intraday" else self.swing_ttl
            if time.time() - data.get("cached_at", 0) > ttl:
                path.unlink() # Delete stale file
                return None

            return LLMDecision(**data["decision"])
        except Exception:
            return None

    def set(self, state: MarketStatePayload, sys_prompt: str, user_prompt: str, decision: LLMDecision) -> None:
        key = self._hash_key(state, sys_prompt, user_prompt)
        path = self.dir / f"cache_{key}.json"

        data = {
            "cached_at": time.time(),
            "decision": decision.dict()
        }
        try:
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception:
            pass
```

---

## 📄 6. Output Schema Validation & Invariant Gates (`crypto_trader/ai/validators/decision_schema.py`)

Enforces boundaries and checks the LLM against deterministic bounds:

```python
import json
from typing import Tuple, Optional
from crypto_trader.ai.schemas import MarketStatePayload, LLMDecision, EntryZone

class DecisionValidator:
    @staticmethod
    def validate(raw_json: str, state: MarketStatePayload) -> Tuple[LLMDecision, Optional[str]]:
        # 1. Parse JSON
        try:
            clean_text = raw_json.strip()
            if "```json" in clean_text:
                clean_text = clean_text.split("```json")[1].split("```")[0].strip()
            parsed = json.loads(clean_text)
        except Exception as e:
            return DecisionValidator.fallback_no_trade(), f"JSONDecodeError: {e}"

        # 2. Pydantic schema validation
        try:
            decision = LLMDecision(**parsed)
        except Exception as e:
            return DecisionValidator.fallback_no_trade(), f"ValidationError: {e}"

        # 3. Hard Safety Gate Boundaries
        if decision.action != "NO_TRADE":
            # Rule A: Invalidation Stop Loss price direction sanity
            if decision.action == "LONG" and decision.stop_loss >= state.price:
                return DecisionValidator.fallback_no_trade(), "RiskGate: LONG stop_loss must be below current price"
            if decision.action == "SHORT" and decision.stop_loss <= state.price:
                return DecisionValidator.fallback_no_trade(), "RiskGate: SHORT stop_loss must be above current price"

            # Rule B: Risk-reward safety cutoff
            if decision.risk_reward < 1.5:
                return DecisionValidator.fallback_no_trade(), f"RiskGate: Risk Reward ratio {decision.risk_reward} below minimum 1.5"

            # Rule C: Conflicting structures
            if decision.action == "LONG" and state.htf_trend == "bearish" and state.mode == "swing":
                return DecisionValidator.fallback_no_trade(), "RiskGate: LONG swing entry blocked on bearish HTF trend"

            # Rule D: Stale target structures
            if len(decision.targets) == 0:
                return DecisionValidator.fallback_no_trade(), "RiskGate: Missing profit targets"

        return decision, None

    @staticmethod
    def fallback_no_trade() -> LLMDecision:
        return LLMDecision(
            action="NO_TRADE",
            confidence=0.0,
            setup_type="FALLBACK_SAFETY",
            entry_zone=EntryZone(low=0.0, high=0.0),
            stop_loss=0.0,
            targets=[],
            risk_reward=0.0,
            invalidation="Failed validation gate checks",
            warnings=["Risk gate fallback activated"],
            reason_codes=["GATED_BY_SYSTEM"]
        )
```

---

## 📄 7. Telemetry & Analytics (`crypto_trader/ai/telemetry.py`)

A structured metrics logging layer to audit latency and routing metrics:

```python
import json
import time
import logging
from pathlib import Path
from datetime import datetime
from crypto_trader.ai.schemas import LLMDecision

logger = logging.getLogger("crypto_trader.ai.telemetry")
DATA_DIR = Path.home() / ".crypto_trader"

class LLMTelemetry:
    def __init__(self):
        self.log_dir = DATA_DIR / "llm_telemetry"
        self.log_dir.mkdir(exist_ok=True)

    def start_timer(self) -> float:
        return time.perf_counter()

    def stop_timer(self, start_time: float) -> float:
        return (time.perf_counter() - start_time) * 1000.0

    def record_cache_hit(self, symbol: str) -> None:
        logger.info(f"[Telemetry] Cache hit recorded for {symbol}")

    def record_success(self, symbol: str, provider: str, decision: LLMDecision, latency_ms: float) -> None:
        self._write_log(symbol, {
            "status": "success",
            "provider": provider,
            "latency_ms": round(latency_ms, 2),
            "action": decision.action,
            "confidence": decision.confidence,
            "setup_type": decision.setup_type,
            "reason_codes": decision.reason_codes
        })

    def record_failure(self, symbol: str, provider: str, reason: str, latency_ms: float) -> None:
        self._write_log(symbol, {
            "status": "failure",
            "provider": provider,
            "latency_ms": round(latency_ms, 2),
            "reason": reason
        })

    def _write_log(self, symbol: str, payload: dict) -> None:
        date_str = datetime.now().strftime("%Y-%m-%d")
        path = self.log_dir / f"metrics_{date_str}.jsonl"

        entry = {
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            **payload
        }
        try:
            with open(path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.warning(f"[Telemetry] Logging failed: {e}")
```
