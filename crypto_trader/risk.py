"""
crypto_trader.risk — Risk Manager & Circuit Breakers
======================================================
Daily trade limits, consecutive loss tracking, and LLM failure circuit breaker.
"""

import json
import time
import logging
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Tuple, Optional
from decimal import Decimal

logger = logging.getLogger("crypto_trader.risk")

DATA_DIR = Path.home() / ".crypto_trader"
DATA_DIR.mkdir(exist_ok=True)


class RiskManager:
    """Enforces daily trade limits and consecutive loss stops."""

    def __init__(
        self,
        max_daily_trades: int = 2,
        max_consecutive_losses: int = 2,
        max_drawdown_pct: float = 0.20,
        cooldown_after_loss_minutes: int = 30,
    ):
        self.max_daily = max_daily_trades
        self.max_consecutive = max_consecutive_losses
        self.max_drawdown_pct = max_drawdown_pct
        self.cooldown_after_loss_seconds = cooldown_after_loss_minutes * 60
        self.daily_count = 0
        self.last_trade_date: Optional[date] = None
        self.consecutive_losses = 0
        self.last_loss_time: Optional[float] = None
        self.peak_balance: Optional[float] = None
        # Hard kill switch (e.g. reconciliation desync, stale feed). Requires
        # explicit clear — never auto-resets.
        self.kill_switch: bool = False
        self.kill_switch_reason: Optional[str] = None
        self.state_file = DATA_DIR / "risk_state.json"
        self._load_state()

    def _today(self) -> date:
        return datetime.now(timezone.utc).date()

    def trigger_kill_switch(self, reason: str):
        """Hard-halt trading until explicitly cleared (reconciliation desync, stale feed)."""
        if not self.kill_switch:
            logger.critical(f"[KILL SWITCH] Trading halted: {reason}")
        self.kill_switch = True
        self.kill_switch_reason = reason
        self._save_state()

    def clear_kill_switch(self):
        """Manual reset after the underlying condition is resolved."""
        self.kill_switch = False
        self.kill_switch_reason = None
        self._save_state()
        logger.info("[KILL SWITCH] Cleared")

    def can_trade(self, current_balance: Optional[float] = None) -> Tuple[bool, str]:
        if self.kill_switch:
            return False, f"Kill switch active: {self.kill_switch_reason or 'unknown'}"

        today = self._today()
        if self.last_trade_date != today:
            self.daily_count = 0
            self.last_trade_date = today

        if self.daily_count >= self.max_daily:
            return False, f"Daily trade limit reached ({self.max_daily})"
        if self.consecutive_losses >= self.max_consecutive:
            return False, f"Consecutive loss halt ({self.max_consecutive}) — manual reset required"
        if self.last_loss_time:
            elapsed = time.time() - self.last_loss_time
            if elapsed < self.cooldown_after_loss_seconds:
                remaining = int((self.cooldown_after_loss_seconds - elapsed) // 60) + 1
                return False, f"Cooldown after loss active ({remaining}m remaining)"
        if current_balance is not None and self.peak_balance and self.peak_balance > 0:
            cur = float(current_balance) if isinstance(current_balance, Decimal) else float(current_balance)
            peak = float(self.peak_balance) if isinstance(self.peak_balance, Decimal) else float(self.peak_balance)
            dd = (peak - cur) / peak
            if dd >= self.max_drawdown_pct:
                return False, f"Max drawdown reached ({dd:.1%} >= {self.max_drawdown_pct:.1%})"
        return True, "OK"

    def record_open(self):
        today = self._today()
        if self.last_trade_date != today:
            self.daily_count = 0
            self.last_trade_date = today
        self.daily_count += 1
        self._save_state()

    def record_close(self, net_pnl: float):
        """Record trade outcome. net_pnl is the FULL trade PnL (including partials)."""
        if net_pnl > 0:
            self.consecutive_losses = 0
            logger.info(f"[RISK] Win recorded. Consecutive losses reset to 0.")
        else:
            self.consecutive_losses += 1
            self.last_loss_time = time.time()
            logger.warning(f"[RISK] Loss recorded. Consecutive losses: {self.consecutive_losses}/{self.max_consecutive}")
        self._save_state()

    def update_peak_balance(self, current_balance: float):
        cur = float(current_balance) if isinstance(current_balance, Decimal) else float(current_balance)
        if cur <= 0:
            return
        peak = float(self.peak_balance) if self.peak_balance is not None else None
        if peak is None or cur > peak:
            self.peak_balance = cur
            self._save_state()

    def reset_consecutive_losses(self):
        """Manual reset after reviewing strategy."""
        self.consecutive_losses = 0
        self._save_state()
        logger.info("[RISK] Consecutive loss counter manually reset")

    def _save_state(self):
        state = {
            "daily_count": self.daily_count,
            "last_trade_date": self.last_trade_date.isoformat() if self.last_trade_date else None,
            "consecutive_losses": self.consecutive_losses,
            "last_loss_time": self.last_loss_time,
            "peak_balance": self.peak_balance,
            "kill_switch": self.kill_switch,
            "kill_switch_reason": self.kill_switch_reason,
        }
        with open(self.state_file, "w") as f:
            json.dump(state, f)

    def _load_state(self):
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file, "r") as f:
                state = json.load(f)
            self.daily_count = state.get("daily_count", 0)
            d = state.get("last_trade_date")
            self.last_trade_date = date.fromisoformat(d) if d else None
            self.consecutive_losses = state.get("consecutive_losses", 0)
            self.last_loss_time = state.get("last_loss_time")
            self.peak_balance = state.get("peak_balance")
            self.kill_switch = bool(state.get("kill_switch", False))
            self.kill_switch_reason = state.get("kill_switch_reason")
        except Exception as e:
            logger.warning(f"RiskManager load failed: {e}")


class AdaptiveThresholdManager:
    """
    Gradually lowers the entry threshold if no trades occur for a long time.
    Ensures the system doesn't stay 'silent' indefinitely (e.g., aiming for 1 trade/day).
    """

    def __init__(
        self,
        base_threshold: float = 0.75,
        min_threshold: float = 0.50,
        decay_per_hour: float = 0.01,
        target_trades_per_day: float = 1.0,
    ):
        self.base_threshold = base_threshold
        self.min_threshold = min_threshold
        self.decay_rate = decay_per_hour
        self.target_interval = 24.0 / target_trades_per_day if target_trades_per_day > 0 else 24.0
        
        self.last_trade_time = time.time()
        self.state_file = DATA_DIR / "adaptive_threshold.json"
        self._load_state()

    def get_threshold(self) -> float:
        """Calculate current threshold based on time since last trade."""
        hours_since = (time.time() - self.last_trade_time) / 3600.0
        
        # Start decaying only after we've passed the target interval
        if hours_since <= self.target_interval:
            return self.base_threshold
        
        excess_hours = hours_since - self.target_interval
        decay = excess_hours * self.decay_rate
        current = max(self.min_threshold, self.base_threshold - decay)
        
        if current < self.base_threshold:
            logger.debug(f"[ADAPTIVE] Threshold decayed: {self.base_threshold:.2f} -> {current:.2f} ({hours_since:.1f}h since trade)")
        
        return round(current, 3)

    def record_trade(self):
        """Reset the clock and threshold."""
        self.last_trade_time = time.time()
        self._save_state()
        logger.info(f"[ADAPTIVE] Trade recorded. Threshold reset to {self.base_threshold:.2f}")

    def _save_state(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump({"last_trade_time": self.last_trade_time}, f)
        except Exception:
            pass

    def _load_state(self):
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file, "r") as f:
                data = json.load(f)
            self.last_trade_time = data.get("last_trade_time", time.time())
        except Exception:
            pass


class LLMCircuitBreaker:
    """Auto-disable LLM after repeated failures."""

    def __init__(self, max_failures: int = 5, cooldown_minutes: int = 30):
        self.max_failures = max_failures
        self.cooldown = cooldown_minutes * 60
        self.failure_count = 0
        self.last_failure_time: Optional[float] = None
        self.disabled = False

    def record_success(self):
        self.failure_count = 0
        self.disabled = False

    def record_failure(self) -> bool:
        """Record failure. Returns True if LLM should be disabled."""
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.max_failures:
            self.disabled = True
            logger.critical(f"[LLM CIRCUIT BREAKER] LLM disabled after {self.failure_count} failures")
            return True
        return False

    def can_use(self) -> bool:
        if not self.disabled:
            return True
        # Auto-re-enable after cooldown
        if self.last_failure_time and (time.time() - self.last_failure_time) > self.cooldown:
            logger.info("[LLM CIRCUIT BREAKER] Cooldown expired. Re-enabling LLM.")
            self.disabled = False
            self.failure_count = 0
            return True
        return False
