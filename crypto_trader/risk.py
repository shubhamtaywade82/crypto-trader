"""
crypto_trader.risk — Risk Manager & Circuit Breakers
======================================================
Daily trade limits, consecutive loss tracking, and LLM failure circuit breaker.
"""

import json
import os
import time
import logging
import tempfile
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Tuple, Optional
from decimal import Decimal

logger = logging.getLogger("crypto_trader.risk")

DATA_DIR = Path.home() / ".crypto_trader"
DATA_DIR.mkdir(exist_ok=True)


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically (temp file + fsync + rename) so a crash mid-write
    can't leave a truncated/corrupt state file — critical for the kill switch."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class RiskManager:
    """Enforces daily trade limits and consecutive loss stops."""

    _CORRELATED_GROUPS = [
        ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        ["DOGEUSDT", "SHIBUSDT", "PEPEUSDT"],
    ]

    def __init__(
        self,
        max_daily_trades: int = 2,
        max_consecutive_losses: int = 2,
        max_drawdown_pct: float = 0.20,
        max_daily_drawdown_pct: float = 0.05,
        cooldown_after_loss_minutes: int = 30,
        max_correlated_positions: int = 2,
        max_orders_per_minute: int = 6,
        max_margin_ratio: float = 0.80,
    ):
        self.max_daily = max_daily_trades
        self.max_consecutive = max_consecutive_losses
        self.max_drawdown_pct = max_drawdown_pct
        self.max_daily_drawdown_pct = max_daily_drawdown_pct
        self.cooldown_after_loss_seconds = cooldown_after_loss_minutes * 60
        self.max_correlated_positions = max_correlated_positions
        self.max_margin_ratio = max_margin_ratio
        # G2: per-minute order velocity circuit breaker (runaway-loop guard).
        # In-memory only; a restart resets the 60s window, which is safe.
        self.max_orders_per_minute = max_orders_per_minute
        self._recent_open_times: list = []
        self.daily_count = 0
        self.daily_pnl = 0.0
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

    def _to_float(self, val) -> float:
        return float(val)

    def _reset_daily_stats_if_new_day(self) -> None:
        today = self._today()
        if self.last_trade_date != today:
            self.daily_count = 0
            self.daily_pnl = 0.0
            self.last_trade_date = today

    def _has_reached_daily_limit(self) -> bool:
        return self.daily_count >= self.max_daily

    def _has_reached_velocity_limit(self) -> bool:
        if self.max_orders_per_minute <= 0:
            return False
        now = time.time()
        self._recent_open_times = [t for t in self._recent_open_times if now - t < 60.0]
        return len(self._recent_open_times) >= self.max_orders_per_minute

    def _is_daily_drawdown_limit_hit(self, initial_daily_balance: Optional[float]) -> bool:
        if not initial_daily_balance:
            return False
        return self.daily_pnl <= -initial_daily_balance * self.max_daily_drawdown_pct

    def _is_consecutive_loss_halt_active(self) -> bool:
        return self.consecutive_losses >= self.max_consecutive

    def _check_cooldown_status(self) -> Tuple[bool, str]:
        if not self.last_loss_time:
            return False, ""
        elapsed = time.time() - self.last_loss_time
        if elapsed < self.cooldown_after_loss_seconds:
            remaining = int((self.cooldown_after_loss_seconds - elapsed) // 60) + 1
            return True, f"Cooldown after loss active ({remaining}m remaining)"
        return False, ""

    def _is_max_drawdown_reached(self, current_balance: Optional[float]) -> bool:
        if current_balance is None or not self.peak_balance or self.peak_balance <= 0:
            return False
        cur = self._to_float(current_balance)
        peak = float(self.peak_balance)
        dd = (peak - cur) / peak
        return dd >= self.max_drawdown_pct

    def can_trade(self, current_balance: Optional[float] = None, initial_daily_balance: Optional[float] = None) -> Tuple[bool, str]:
        if self.kill_switch:
            return False, f"Kill switch active: {self.kill_switch_reason or 'unknown'}"

        self._reset_daily_stats_if_new_day()

        if self._has_reached_daily_limit():
            return False, f"Daily trade limit reached ({self.max_daily})"

        # G2: velocity circuit breaker — block runaway bursts within a 60s window.
        if self._has_reached_velocity_limit():
            return False, (f"Velocity limit reached "
                           f"({len(self._recent_open_times)}/{self.max_orders_per_minute} orders/min)")
        
        # Kill switch: 5% daily drawdown
        if self._is_daily_drawdown_limit_hit(initial_daily_balance):
            return False, f"Daily drawdown limit hit: {self.daily_pnl:.2f} ({self.max_daily_drawdown_pct*100:.1f}%)"

        if self._is_consecutive_loss_halt_active():
            return False, f"Consecutive loss halt ({self.max_consecutive}) — manual reset required"
        
        is_cooldown, cooldown_msg = self._check_cooldown_status()
        if is_cooldown:
            return False, cooldown_msg
        
        if self._is_max_drawdown_reached(current_balance):
            cur = self._to_float(current_balance)
            peak = float(self.peak_balance)
            dd = (peak - cur) / peak
            return False, f"Max drawdown reached ({dd:.1%} >= {self.max_drawdown_pct:.1%})"
        
        return True, "OK"

    def check_margin_ratio(self, ratio: float) -> Tuple[bool, str]:
        """Verify the authoritative exchange margin ratio is within safe bounds."""
        if ratio >= self.max_margin_ratio:
            return False, f"Exchange margin ratio too high: {ratio:.2%} >= {self.max_margin_ratio:.2%}"
        return True, "OK"

    def check_correlation(self, symbol: str, side: str, active_positions: dict) -> Tuple[bool, str]:
        """Prevent taking too many positions in the same direction within correlated groups."""
        correlated_groups = [
            ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
            ["DOGEUSDT", "SHIBUSDT", "PEPEUSDT"],
        ]
        
        for group in correlated_groups:
            if symbol in group:
                same_direction_count = 0
                for sym, pos in active_positions.items():
                    if sym in group and pos.get("side") == side:
                        same_direction_count += 1
                
                if same_direction_count >= self.max_correlated_positions:
                    return False, f"Correlation block: {same_direction_count} {side} positions already active in {group}"
        
        return True, "OK"

    def record_open(self):
        self._reset_daily_stats_if_new_day()
        self.daily_count += 1
        self._recent_open_times.append(time.time())  # G2 velocity tracking
        self._save_state()

    def record_close(self, net_pnl: float):
        """Record trade outcome. net_pnl is the FULL trade PnL (including partials)."""
        self.daily_pnl += float(net_pnl)
        if net_pnl > 0:
            self.consecutive_losses = 0
            logger.info(f"[RISK] Win recorded. Consecutive losses reset to 0. Daily PnL: {self.daily_pnl:.2f}")
        else:
            self.consecutive_losses += 1
            self.last_loss_time = time.time()
            logger.warning(f"[RISK] Loss recorded. Consecutive losses: {self.consecutive_losses}/{self.max_consecutive}. Daily PnL: {self.daily_pnl:.2f}")
        self._save_state()

    def update_peak_balance(self, current_balance: float):
        cur = self._to_float(current_balance)
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
            "daily_pnl": self.daily_pnl,
            "last_trade_date": self.last_trade_date.isoformat() if self.last_trade_date else None,
            "consecutive_losses": self.consecutive_losses,
            "last_loss_time": self.last_loss_time,
            "peak_balance": self.peak_balance,
            "kill_switch": self.kill_switch,
            "kill_switch_reason": self.kill_switch_reason,
        }
        _atomic_write_json(self.state_file, state)

    def _load_state(self):
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file, "r") as f:
                state = json.load(f)
            self.daily_count = state.get("daily_count", 0)
            self.daily_pnl = state.get("daily_pnl", 0.0)
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

    def _hours_since_last_trade(self) -> float:
        return (time.time() - self.last_trade_time) / 3600.0

    def _calculate_decayed_threshold(self, hours_since: float) -> float:
        excess_hours = hours_since - self.target_interval
        decay = excess_hours * self.decay_rate
        return max(self.min_threshold, self.base_threshold - decay)

    def _log_decay_if_active(self, current: float, hours_since: float) -> None:
        if current < self.base_threshold:
            logger.debug(f"[ADAPTIVE] Threshold decayed: {self.base_threshold:.2f} -> {current:.2f} ({hours_since:.1f}h since trade)")

    def get_threshold(self) -> float:
        """Calculate current threshold based on time since last trade."""
        hours_since = self._hours_since_last_trade()
        
        # Start decaying only after we've passed the target interval
        if hours_since <= self.target_interval:
            return self.base_threshold
        
        current = self._calculate_decayed_threshold(hours_since)
        self._log_decay_if_active(current, hours_since)
        
        return round(current, 3)


    def record_trade(self):
        """Reset the clock and threshold."""
        self.last_trade_time = time.time()
        self._save_state()
        logger.info(f"[ADAPTIVE] Trade recorded. Threshold reset to {self.base_threshold:.2f}")

    def _save_state(self):
        try:
            _atomic_write_json(self.state_file, {"last_trade_time": self.last_trade_time})
        except Exception as e:
            logger.warning("AdaptiveThresholdManager state save failed: %s", e)

    def _load_state(self):
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file, "r") as f:
                data = json.load(f)
            self.last_trade_time = data.get("last_trade_time", time.time())
        except Exception as e:
            logger.warning("AdaptiveThresholdManager state load failed: %s", e)


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

class MarginEngine:
    """
    Enforces futures margin safety rules.
    PRD §9: Margin utilization, liquidation distance.
    """
    def __init__(
        self,
        max_margin_utilization: float = 0.25,
        min_liquidation_distance_pct: float = 0.03,
        require_isolated_margin: bool = True
    ):
        self.max_margin_utilization = max_margin_utilization
        self.min_liquidation_distance_pct = min_liquidation_distance_pct
        self.require_isolated_margin = require_isolated_margin
        
    def validate_margin_mode(self, is_isolated: bool) -> Tuple[bool, str]:
        if self.require_isolated_margin and not is_isolated:
            return False, "Cross margin is prohibited. Isolated margin required."
        return True, "OK"
        
    def check_margin_utilization(self, total_maintenance_margin: float, wallet_balance: float) -> Tuple[bool, str]:
        if wallet_balance <= 0:
            return False, "Wallet balance is zero or negative."
        
        utilization = total_maintenance_margin / wallet_balance
        if utilization > self.max_margin_utilization:
            return False, f"Margin utilization too high: {utilization:.2%} > {self.max_margin_utilization:.2%}"
        return True, "OK"
        
    def check_liquidation_distance(self, entry_price: float, stop_loss_price: float, liquidation_price: float, side: str) -> Tuple[bool, str]:
        if side.lower() == "long":
            if stop_loss_price <= liquidation_price:
                return False, f"Stop loss {stop_loss_price} is at or below liquidation {liquidation_price}"
            dist = (entry_price - liquidation_price) / entry_price
        else:
            if stop_loss_price >= liquidation_price:
                return False, f"Stop loss {stop_loss_price} is at or above liquidation {liquidation_price}"
            dist = (liquidation_price - entry_price) / entry_price
            
        if dist < self.min_liquidation_distance_pct:
            return False, f"Liquidation distance too small: {dist:.2%} < {self.min_liquidation_distance_pct:.2%}"
        
        return True, "OK"

class LeverageEngine:
    """
    Enforces dynamic leverage rules.
    PRD §9: Leverage tracking, tier validation, ADL monitoring.
    """
    def __init__(self, default_leverage: int = 2, hard_max_leverage: int = 5):
        self.default_leverage = default_leverage
        self.hard_max_leverage = hard_max_leverage
        
    def validate_leverage_tier(self, requested_leverage: int, position_notional: float, max_notional_for_tier: float) -> Tuple[bool, str]:
        if requested_leverage > self.hard_max_leverage:
            return False, f"Requested leverage {requested_leverage}x exceeds hard max {self.hard_max_leverage}x"
            
        if position_notional > max_notional_for_tier:
            return False, f"Position notional {position_notional} exceeds tier max {max_notional_for_tier} for {requested_leverage}x"
            
        return True, "OK"
        
    def calculate_effective_leverage(self, total_position_notional: float, account_equity: float) -> float:
        if account_equity <= 0:
            return float('inf')
        return total_position_notional / account_equity
        
    def adjust_leverage_for_volatility(self, base_leverage: int, atr_pct: float, high_vol_threshold: float = 0.05, extreme_vol_threshold: float = 0.10) -> int:
        if atr_pct >= extreme_vol_threshold:
            return 0 # No trading
        elif atr_pct >= high_vol_threshold:
            return max(1, base_leverage // 2)
        return base_leverage
