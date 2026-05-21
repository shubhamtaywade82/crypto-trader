from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path

DATA_DIR = Path.home() / ".crypto_trader"
DATA_DIR.mkdir(parents=True, exist_ok=True)

@dataclass
class LLMCircuitBreaker:
    max_failures: int = 5
    failures: int = 0
    enabled: bool = True

    def record_success(self):
        self.failures = 0
    def record_failure(self):
        self.failures += 1
        if self.failures >= self.max_failures:
            self.enabled = False

class RiskManager:
    def __init__(self, max_daily: int = 2, max_consec_loss: int = 2):
        self.max_daily = max_daily
        self.max_consec_loss = max_consec_loss
        self.daily_count = 0
        self.consec_losses = 0
        self.last_day = None
        self.state_file = DATA_DIR / "risk_state.json"
        self._load()

    def _day(self):
        return datetime.now(timezone.utc).date().isoformat()

    def can_trade(self):
        day = self._day()
        if self.last_day != day:
            self.last_day, self.daily_count = day, 0
        return self.daily_count < self.max_daily and self.consec_losses < self.max_consec_loss

    def record_open(self):
        self.daily_count += 1
        self.save()

    def record_close(self, pnl: float):
        self.consec_losses = 0 if pnl > 0 else self.consec_losses + 1
        self.save()

    def save(self):
        self.state_file.write_text(json.dumps({
            "daily_count": self.daily_count,
            "consec_losses": self.consec_losses,
            "last_day": self.last_day,
        }))

    def _load(self):
        if not self.state_file.exists():
            return
        try:
            d = json.loads(self.state_file.read_text())
            self.daily_count = d.get("daily_count", 0)
            self.consec_losses = d.get("consec_losses", 0)
            self.last_day = d.get("last_day")
        except Exception:
            pass  # corrupt file — start fresh

