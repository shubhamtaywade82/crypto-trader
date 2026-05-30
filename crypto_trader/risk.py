"""
crypto_trader.risk — Risk Manager & Circuit Breakers
======================================================
Daily trade limits, consecutive loss tracking, and LLM failure circuit breaker.

The ``can_trade()`` gate is implemented with the Specification pattern so that
each rule is a named, independently-testable class.  Adding a new rule means:
  1. Write a new ``_*Spec`` class.
  2. Append ``& _NewSpec()`` to the ``_gate`` composition in ``can_trade()``.
No other method needs to change.
"""

import json
import os
import time
import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone, date
from pathlib import Path
from typing import List, Tuple, Optional
from decimal import Decimal

from .patterns.specification import Specification

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


from crypto_trader.margin_engine import MarginEngine, LeverageEngine

# ─────────────────────────────────────────────────────────────────────────────
# Specification objects for can_trade()
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _RiskCtx:
    """Snapshot of all state needed by the risk-gate specifications."""
    manager: "RiskManager"
    current_balance: Optional[float]
    initial_daily_balance: Optional[float]
    total_notional: float = 0.0


class _KillSwitchSpec(Specification["_RiskCtx"]):
    def is_satisfied_by(self, ctx: "_RiskCtx") -> Tuple[bool, str]:
        m = ctx.manager
        if m.kill_switch:
            return False, f"Kill switch active: {m.kill_switch_reason or 'unknown'}"
        return True, ""


class _DailyLimitSpec(Specification["_RiskCtx"]):
    def is_satisfied_by(self, ctx: "_RiskCtx") -> Tuple[bool, str]:
        m = ctx.manager
        if m._has_reached_daily_limit():
            return False, f"Daily trade limit reached ({m.max_daily})"
        return True, ""


class _VelocitySpec(Specification["_RiskCtx"]):
    def is_satisfied_by(self, ctx: "_RiskCtx") -> Tuple[bool, str]:
        m = ctx.manager
        if m._has_reached_velocity_limit():
            count = len(m._recent_open_times)
            return False, (
                f"Velocity limit reached "
                f"({count}/{m.max_orders_per_minute} orders/min)"
            )
        return True, ""


class _DailyDrawdownSpec(Specification["_RiskCtx"]):
    def is_satisfied_by(self, ctx: "_RiskCtx") -> Tuple[bool, str]:
        m = ctx.manager
        if m._is_daily_drawdown_limit_hit(ctx.initial_daily_balance):
            return False, (
                f"Daily drawdown limit hit: {m.daily_pnl:.2f} "
                f"({m.max_daily_drawdown_pct * 100:.1f}%)"
            )
        return True, ""


class _ConsecutiveLossSpec(Specification["_RiskCtx"]):
    def is_satisfied_by(self, ctx: "_RiskCtx") -> Tuple[bool, str]:
        m = ctx.manager
        if m._is_consecutive_loss_halt_active():
            return False, (
                f"Consecutive loss halt ({m.max_consecutive}) "
                f"— manual reset required"
            )
        return True, ""


class _CooldownSpec(Specification["_RiskCtx"]):
    def is_satisfied_by(self, ctx: "_RiskCtx") -> Tuple[bool, str]:
        m = ctx.manager
        in_cooldown, reason = m._check_cooldown_status()
        if in_cooldown:
            return False, reason
        return True, ""


class _MaxDrawdownSpec(Specification["_RiskCtx"]):
    def is_satisfied_by(self, ctx: "_RiskCtx") -> Tuple[bool, str]:
        m = ctx.manager
        if m._is_max_drawdown_reached(ctx.current_balance):
            cur = m._to_float(ctx.current_balance)
            peak = float(m.peak_balance)
            dd = (peak - cur) / peak
            return False, (
                f"Max drawdown reached "
                f"({dd:.1%} >= {m.max_drawdown_pct:.1%})"
            )
        return True, ""


class _PortfolioLeverageSpec(Specification["_RiskCtx"]):
    def is_satisfied_by(self, ctx: "_RiskCtx") -> Tuple[bool, str]:
        m = ctx.manager
        if ctx.current_balance and float(ctx.current_balance) > 0:
            eff_lev = m.leverage_engine.calculate_effective_leverage(
                ctx.total_notional, float(ctx.current_balance)
            )
            if float(eff_lev) > m.max_portfolio_leverage:
                return False, (
                    f"Portfolio leverage too high: "
                    f"{float(eff_lev):.2f}x > {m.max_portfolio_leverage:.2f}x"
                )
        return True, ""


# Composed gate — evaluated left-to-right; first failure short-circuits.
_TRADE_GATE = (
    _KillSwitchSpec()
    & _DailyLimitSpec()
    & _VelocitySpec()
    & _DailyDrawdownSpec()
    & _ConsecutiveLossSpec()
    & _CooldownSpec()
    & _MaxDrawdownSpec()
    & _PortfolioLeverageSpec()
)


class RiskManager:
    """Enforces daily trade limits, portfolio exposure, and consecutive loss stops."""

    _CORRELATED_GROUPS = [
        ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        ["DOGEUSDT", "SHIBUSDT", "PEPEUSDT"],
    ]

    def __init__(
        self,
        max_daily_trades: int = 2,
        max_consecutive_losses: int = 3,
        max_drawdown_pct: float = 0.20,
        max_daily_drawdown_pct: float = 0.03,
        cooldown_after_loss_minutes: int = 1440,
        max_correlated_positions: int = 2,
        max_orders_per_minute: int = 6,
        max_margin_ratio: float = 0.80,
        max_portfolio_notional_leverage: float = 3.0,  # Max total exposure / equity
    ):
        self.max_daily = max_daily_trades
        self.max_consecutive = max_consecutive_losses
        self.max_drawdown_pct = max_drawdown_pct
        self.max_daily_drawdown_pct = max_daily_drawdown_pct
        self.cooldown_after_loss_seconds = cooldown_after_loss_minutes * 60
        self.max_correlated_positions = max_correlated_positions
        self.max_margin_ratio = max_margin_ratio
        self.max_portfolio_leverage = max_portfolio_notional_leverage
        
        # G2: per-minute order velocity circuit breaker (runaway-loop guard).
        self.max_orders_per_minute = max_orders_per_minute
        self._recent_open_times: list = []
        
        self.daily_count = 0
        self.daily_pnl = 0.0
        self.last_trade_date: Optional[date] = None
        self.consecutive_losses = 0
        self.last_loss_time: Optional[float] = None
        self.peak_balance: Optional[float] = None
        
        # Hard kill switch
        self.kill_switch: bool = False
        self.kill_switch_reason: Optional[str] = None
        
        # Internalized Engines
        self.margin_engine = MarginEngine(max_margin_utilization=0.40) # Default util
        self.leverage_engine = LeverageEngine()
        
        self.state_file = DATA_DIR / "risk_state.json"
        self._load_state()

    def _today(self) -> date:
        return datetime.now(timezone.utc).date()

    def trigger_kill_switch(self, reason: str):
        """Hard-halt trading until explicitly cleared (G5/G9 safety)."""
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
        try:
            pnl = float(self.daily_pnl)
            initial = float(initial_daily_balance)
            return pnl <= -initial * self.max_daily_drawdown_pct
        except (TypeError, ValueError):
            return False

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
        if current_balance is None or not self.peak_balance or float(self.peak_balance) <= 0:
            return False
        try:
            cur = float(current_balance)
            peak = float(self.peak_balance)
            dd = (peak - cur) / peak
            return dd >= self.max_drawdown_pct
        except (TypeError, ValueError):
            return False

    def can_trade(
        self,
        current_balance: Optional[float] = None,
        initial_daily_balance: Optional[float] = None,
        total_notional: float = 0.0,
    ) -> Tuple[bool, str]:
        """
        Run all risk-gate specifications in order.

        Each check is a :class:`Specification` subclass defined at module level.
        To add a new rule, write a spec class and append it to ``_TRADE_GATE``.
        """
        self._reset_daily_stats_if_new_day()
        ctx = _RiskCtx(
            manager=self,
            current_balance=current_balance,
            initial_daily_balance=initial_daily_balance,
            total_notional=total_notional,
        )
        ok, reason = _TRADE_GATE.is_satisfied_by(ctx)
        return ok, reason or "OK"

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

    def record_close(self, net_pnl: float, current_balance: Optional[float] = None):
        """Record trade outcome. net_pnl is the FULL trade PnL (including partials).

        When ``current_balance`` (post-close wallet balance) is supplied, also
        enforce the daily-loss limit as a hard HALT. The pre-trade gate
        (:class:`_DailyDrawdownSpec`) only blocks the *next* entry; a fast
        intraday bleed across already-open positions could blow past the limit
        before the next entry attempt. Tripping the kill switch here closes that
        gap.
        """
        # Roll the daily counters over BEFORE accumulating, mirroring record_open
        # and can_trade. Without this, a close after UTC midnight (or the first
        # close of a new day before any record_open fired) would add onto
        # yesterday's daily_pnl — corrupting both the daily-loss tracking and the
        # `initial_daily = current_balance - daily_pnl` HALT reconstruction below.
        # Uses the same UTC _today() basis as the pre-trade gate.
        self._reset_daily_stats_if_new_day()
        self.daily_pnl += float(net_pnl)
        if net_pnl > 0:
            self.consecutive_losses = 0
            logger.info(f"[RISK] Win recorded. Consecutive losses reset to 0. Daily PnL: {self.daily_pnl:.2f}")
        else:
            self.consecutive_losses += 1
            self.last_loss_time = time.time()
            logger.warning(f"[RISK] Loss recorded. Consecutive losses: {self.consecutive_losses}/{self.max_consecutive}. Daily PnL: {self.daily_pnl:.2f}")
        self._save_state()

        # Daily-loss HALT (post-close). Reconstruct the day's opening balance the
        # same way the live engine does: initial = current - daily_pnl
        # (daily_pnl already includes this close). See engine_ws initial_daily_balance.
        if current_balance is not None:
            try:
                initial_daily = float(current_balance) - float(self.daily_pnl)
                if self._is_daily_drawdown_limit_hit(initial_daily):
                    self.trigger_kill_switch(
                        f"Daily loss limit hit: {self.daily_pnl:.2f} "
                        f"({self.max_daily_drawdown_pct * 100:.1f}% of {initial_daily:.2f})"
                    )
            except (TypeError, ValueError):
                pass

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

    def apply_overrides(self, ov: dict) -> None:
        """Live-update risk caps from a hot-reload overrides dict. Idempotent —
        safe to call repeatedly (shared across engines). Only the whitelisted
        caps below are touched; kill switch and counters are never altered."""
        if "max_daily_trades" in ov:
            self.max_daily = int(ov["max_daily_trades"])
        if "max_consecutive_losses" in ov:
            self.max_consecutive = int(ov["max_consecutive_losses"])
        if "max_orders_per_minute" in ov:
            self.max_orders_per_minute = int(ov["max_orders_per_minute"])
        if "max_margin_ratio" in ov:
            self.max_margin_ratio = float(ov["max_margin_ratio"])
        if "max_drawdown_pct" in ov:
            self.max_drawdown_pct = float(ov["max_drawdown_pct"])
        if "max_daily_drawdown_pct" in ov:
            self.max_daily_drawdown_pct = float(ov["max_daily_drawdown_pct"])
        if "cooldown_after_loss_minutes" in ov:
            self.cooldown_after_loss_seconds = int(ov["cooldown_after_loss_minutes"]) * 60

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

    def apply_overrides(self, ov: dict) -> None:
        """Live-update adaptive-threshold params from a hot-reload overrides dict."""
        if "final_score_threshold" in ov:
            self.base_threshold = float(ov["final_score_threshold"])
        if "adaptive_min_threshold" in ov:
            self.min_threshold = float(ov["adaptive_min_threshold"])
        if "adaptive_decay_per_hour" in ov:
            self.decay_rate = float(ov["adaptive_decay_per_hour"])
        if "adaptive_target_trades_per_day" in ov:
            tpd = float(ov["adaptive_target_trades_per_day"])
            self.target_interval = 24.0 / tpd if tpd > 0 else 24.0

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
    """
    Auto-disable LLM after repeated failures.

    Thin wrapper around :class:`~crypto_trader.patterns.CircuitBreaker` that
    preserves the original ``record_success`` / ``record_failure`` / ``can_use``
    API so existing callers don't need to change.
    """

    def __init__(self, max_failures: int = 5, cooldown_minutes: int = 30):
        from .patterns.circuit_breaker import CircuitBreaker as _CB
        self._cb = _CB(
            name="llm",
            failure_threshold=max_failures,
            recovery_timeout=cooldown_minutes * 60,
        )
        # Expose raw counters for compatibility with any code reading them directly.
        self.max_failures = max_failures
        self.cooldown = cooldown_minutes * 60

    @property
    def failure_count(self) -> int:
        return self._cb.failure_count

    @property
    def disabled(self) -> bool:
        return self._cb.is_open

    @disabled.setter
    def disabled(self, value: bool) -> None:
        if value:
            self._cb.trip()
        else:
            self._cb.reset()

    @property
    def last_failure_time(self) -> Optional[float]:
        return self._cb._last_failure_time

    def record_success(self) -> None:
        self._cb._on_success()

    def record_failure(self) -> bool:
        """Record a failure. Returns True if the LLM is now disabled."""
        self._cb._on_failure()
        is_open = self._cb.is_open
        if is_open:
            logger.critical(
                "[LLM CIRCUIT BREAKER] LLM disabled after %d failure(s)",
                self._cb.failure_count,
            )
        return is_open

    def can_use(self) -> bool:
        from .patterns.circuit_breaker import CircuitState
        state = self._cb.state  # triggers HALF_OPEN check automatically
        return state != CircuitState.OPEN
