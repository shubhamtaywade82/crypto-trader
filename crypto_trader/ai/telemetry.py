"""crypto_trader.ai.telemetry — Structured interaction logging and latency tracking."""

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
            "reason_codes": decision.reason_codes,
        })

    def record_failure(self, symbol: str, provider: str, reason: str, latency_ms: float) -> None:
        self._write_log(symbol, {
            "status": "failure",
            "provider": provider,
            "latency_ms": round(latency_ms, 2),
            "reason": reason,
        })

    def _write_log(self, symbol: str, payload: dict) -> None:
        date_str = datetime.now().strftime("%Y-%m-%d")
        path = self.log_dir / f"metrics_{date_str}.jsonl"

        entry = {
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            **payload,
        }
        try:
            with open(path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.warning(f"[Telemetry] Logging failed: {e}")
