"""crypto_trader.ai.shadow — Non-blocking shadow evaluation of a candidate model.

Runs a candidate model (e.g. a small 2B model) beside the live decision path.
The shadow call NEVER influences trading: it receives the exact same prompts as
the primary router call, its output is validated by the same DecisionValidator,
and only aggregated metrics are persisted. This is the measurement half of the
promotion gate — a candidate earns a role change only after beating the
incumbent on agreement rate, schema-failure rate, and latency over a
representative window.

Metrics are:
  - agreement_rate    : share of valid shadow decisions whose action equals the
                        primary action (direction-level agreement).
  - schema_fail_rate  : share of shadow calls producing unparseable/schema-
                        invalid output (small models truncate; this is the
                        hard-failure rate to watch).
  - latency p50 / p95 : must fit inside the data-freshness budget alongside
                        the primary call when promoted from shadow to triage.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from crypto_trader.ai.schemas import LLMDecision, MarketStatePayload

logger = logging.getLogger("crypto_trader.ai.shadow")

DATA_DIR = Path.home() / ".crypto_trader"


@dataclass
class ShadowResult:
    """Outcome of one shadow evaluation (purely observational)."""
    model: str
    action: Optional[str] = None          # None when call/schema failed
    confidence: Optional[float] = None
    latency_ms: float = 0.0
    schema_failed: bool = False           # responded but invalid/unparseable
    call_failed: bool = False             # provider returned nothing (transport)
    agreed_action: Optional[bool] = None  # None when no valid decision to compare
    error: Optional[str] = None


class ShadowMetrics:
    """Thread-safe accumulator for shadow evaluation outcomes.

    ``snapshot()`` returns plain-python aggregates (rates + latency percentiles)
    suitable for logging, the metrics API, or a Grafana JSON datasource.
    """

    def __init__(self, model: str, max_samples: int = 2048):
        self.model = model
        self._lock = threading.Lock()
        self._max_samples = max_samples
        self.total = 0
        self.schema_failures = 0
        self.call_failures = 0
        self.valid_decisions = 0
        self.action_agreements = 0
        self.dropped = 0                      # samples dropped: shadow concurrency saturated
        self._latencies: List[float] = []
        self._conf_deltas: List[float] = []

    def record(self, result: ShadowResult) -> None:
        with self._lock:
            self.total += 1
            if result.call_failed:
                self.call_failures += 1
                return
            if result.schema_failed:
                self.schema_failures += 1
                return
            self.valid_decisions += 1
            if result.agreed_action:
                self.action_agreements += 1
            if len(self._latencies) < self._max_samples:
                self._latencies.append(result.latency_ms)
            if result.confidence is not None:
                if len(self._conf_deltas) < self._max_samples:
                    self._conf_deltas.append(result.confidence)

    def record_dropped(self) -> None:
        """Count a sample dropped because the shadow worker capacity was full.

        Dropped samples keep the trading path unaffected (the whole point of the
        cap), but they must be visible: an agreement rate computed over the
        subset that made it through is otherwise silently biased toward periods
        when the candidate box was idle.
        """
        with self._lock:
            self.dropped += 1

    def snapshot(self) -> dict:
        with self._lock:
            valid = self.valid_decisions
            return {
                "model": self.model,
                "total": self.total,
                "call_failures": self.call_failures,
                "schema_failures": self.schema_failures,
                "schema_fail_rate": round(self.schema_failures / self.total, 4) if self.total else 0.0,
                "valid_decisions": valid,
                "action_agreements": self.action_agreements,
                "agreement_rate": round(self.action_agreements / valid, 4) if valid else 0.0,
                "dropped": self.dropped,
                "latency_p50_ms": _pct(self._latencies, 0.50),
                "latency_p95_ms": _pct(self._latencies, 0.95),
                "confidence_p50": _pct(self._conf_deltas, 0.50),
            }

    def to_jsonl(self, path: Path, result: ShadowResult, symbol: str,
                 primary_action: str, primary_confidence: float) -> None:
        """Append one raw evaluation record (audit trail beside the aggregates)."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "shadow_model": self.model,
            "primary_action": primary_action,
            "primary_confidence": primary_confidence,
            "shadow_action": result.action,
            "shadow_confidence": result.confidence,
            "latency_ms": round(result.latency_ms, 2),
            "schema_failed": result.schema_failed,
            "call_failed": result.call_failed,
            "agreed_action": result.agreed_action,
            "error": result.error,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:  # audit log is best-effort
            logger.warning("[Shadow] JSONL write failed: %s", e)


def _pct(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return round(s[idx], 2)


class ShadowEvaluator:
    """Evaluates a candidate model against the primary decision.

    ``provider`` is any object exposing the OllamaLocalProvider chat API
    (``chat(system_prompt, user_prompt, timeout_s) -> Optional[str]``) so tests
    can inject a fake. Validation reuses :class:`DecisionValidator` — the exact
    gate a promoted candidate would face — so schema-failure metrics reflect
    production behaviour, not a diluted harness.
    """

    def __init__(self, provider, validator, model: str,
                 metrics: Optional[ShadowMetrics] = None,
                 log_dir: Optional[Path] = None,
                 max_concurrent: int = 2):
        self.provider = provider
        self.validator = validator
        self.model = model
        self.metrics = metrics or ShadowMetrics(model)
        self.log_dir = Path(log_dir) if log_dir else DATA_DIR / "llm_telemetry"
        # ADR-001 isolation guarantee: shadow inference must never contend with
        # the primary model. A 2B candidate typically shares the GPU/CPU with the
        # triage model, so concurrent shadow inferences are capped — a submit
        # beyond capacity is DROPPED (counted in metrics.dropped), never queued,
        # never blocking the trading thread.
        self._sem = threading.Semaphore(max(1, int(max_concurrent)))
        self.max_concurrent = max(1, int(max_concurrent))

    def evaluate(
        self,
        state: MarketStatePayload,
        system_prompt: str,
        user_prompt: str,
        timeout_s: int,
        primary: LLMDecision,
    ) -> ShadowResult:
        """One synchronous shadow call. Designed to be invoked off the trading
        thread (the router runs it on a daemon thread); safe to call directly
        from tests or a batch backfill script."""
        start = time.perf_counter()
        result = ShadowResult(model=self.model)

        try:
            raw = self.provider.chat(system_prompt, user_prompt, timeout_s)
        except Exception as e:  # transport explodes → count as call failure
            result.call_failed = True
            result.error = f"{type(e).__name__}: {e}"
            result.latency_ms = (time.perf_counter() - start) * 1000
            self._finalize(result, state, primary)
            return result

        result.latency_ms = (time.perf_counter() - start) * 1000

        if not raw:
            result.call_failed = True
            result.error = "empty_response"
            self._finalize(result, state, primary)
            return result

        try:
            # strict_truncation=True: candidate metrics must be measured under
            # the exact promotion gate regardless of the operator's live
            # LLM_STRICT_TRUNCATION toggle, so agreement stays comparable over
            # time. Duck-typed validators (test fakes) predate the kwarg.
            try:
                decision, err = self.validator.validate(raw, state, strict_truncation=True)
            except TypeError:
                decision, err = self.validator.validate(raw, state)
        except Exception as e:  # validator itself must not crash the harness
            decision, err = None, f"validator_exception: {e}"
        if err or decision is None:
            result.schema_failed = True
            result.error = err or "validation_failed"
            self._finalize(result, state, primary)
            return result

        # Validated decision — compare direction with the primary.
        result.action = decision.action
        result.confidence = decision.confidence
        result.agreed_action = (decision.action == primary.action)
        self._finalize(result, state, primary)
        return result

    def try_submit(self, state: MarketStatePayload, system_prompt: str,
                   user_prompt: str, timeout_s: int, primary: LLMDecision,
                   registry: Optional[List] = None) -> bool:
        """Run one evaluation on a short-lived daemon thread, bounded by
        ``max_concurrent``. Non-blocking: returns False immediately (and counts
        a dropped sample) when the shadow is already at capacity, so the router
        never accumulates threads during a candidate-model stall.

        ``registry`` optionally receives the Thread handle so callers (the
        router) can join in tests.
        """
        if not self._sem.acquire(blocking=False):
            self.metrics.record_dropped()
            return False

        def _run():
            try:
                self.evaluate(state, system_prompt, user_prompt, timeout_s, primary)
            except Exception as e:  # shadow must never break trading
                logger.debug("[Shadow] submitted evaluation error: %s", e)
            finally:
                self._sem.release()

        try:
            t = threading.Thread(target=_run, daemon=True,
                                 name=f"shadow-{state.symbol}")
            t.start()
        except Exception:
            self._sem.release()
            self.metrics.record_dropped()
            return False
        if registry is not None:
            registry.append(t)
        return True

    def _finalize(self, result: ShadowResult, state: MarketStatePayload,
                  primary: LLMDecision) -> None:
        self.metrics.record(result)
        self.metrics.to_jsonl(
            self.log_dir / f"shadow_{datetime.now().strftime('%Y-%m-%d')}.jsonl",
            result,
            symbol=state.symbol,
            primary_action=primary.action,
            primary_confidence=primary.confidence,
        )
        if result.schema_failed or result.call_failed:
            logger.info(
                "[Shadow] %s | model=%s | fail schema=%s call=%s (%s) lat=%.0fms",
                state.symbol, self.model, result.schema_failed,
                result.call_failed, result.error, result.latency_ms,
            )
        else:
            logger.info(
                "[Shadow] %s | model=%s | primary=%s shadow=%s agree=%s "
                "conf=%.2f lat=%.0fms",
                state.symbol, self.model, primary.action, result.action,
                result.agreed_action, result.confidence or 0.0, result.latency_ms,
            )
