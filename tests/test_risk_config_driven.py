"""
TDD: Task 1 — Make hard risk limits config-driven with spec defaults.

Tests cover:
  - daily_drawdown: 3% blocks at >= 3%, passes at < 3%
  - consecutive_loss: 3 consecutive losses triggers lockout; 2 does not
  - cooldown: active after loss for configured window; config default is 1440m
  - risk_per_trade: position sizing uses 0.5% of equity by default
  - env override: from_env picks up each of the four env vars
"""
import os
import tempfile
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from crypto_trader.risk import RiskManager
from crypto_trader.config import TradingConfig


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean_risk(**kw) -> RiskManager:
    """Instantiate a RiskManager with an isolated temp state file.

    Patches the module-level DATA_DIR path BEFORE construction so that
    _load_state() never reads the developer's real ~/.crypto_trader/risk_state.json.
    The returned manager has a clean zeroed state and last_trade_date set to
    today so _reset_daily_stats_if_new_day() never clobbers test-set values.
    """
    isolated_dir = Path(tempfile.mkdtemp(prefix="risk_test_"))
    # Patch DATA_DIR so that self.state_file = DATA_DIR / "risk_state.json"
    # inside __init__ points at an empty temp directory, not the real one.
    with patch("crypto_trader.risk.DATA_DIR", isolated_dir):
        r = RiskManager(**kw)
    # Ensure _reset_daily_stats_if_new_day() is a no-op during tests by
    # pinning the date to today (UTC, matching RiskManager._today()); otherwise
    # a None last_trade_date resets daily_pnl before the drawdown gate runs.
    r.last_trade_date = datetime.now(timezone.utc).date()
    return r


# ─────────────────────────────────────────────────────────────────────────────
# 1. Daily-drawdown gate (spec default 3%)
# ─────────────────────────────────────────────────────────────────────────────

class TestDailyDrawdownGate:
    def test_blocks_when_loss_equals_3pct(self):
        r = _clean_risk(max_daily_drawdown_pct=0.03, max_daily_trades=100)
        initial = 10_000.0
        # simulate exactly -3% realized loss
        r.daily_pnl = -initial * 0.03
        ok, reason = r.can_trade(initial_daily_balance=initial)
        assert not ok
        assert "drawdown" in reason.lower() or "Drawdown" in reason

    def test_blocks_when_loss_exceeds_3pct(self):
        r = _clean_risk(max_daily_drawdown_pct=0.03, max_daily_trades=100)
        initial = 10_000.0
        r.daily_pnl = -initial * 0.05  # -5%, well beyond limit
        ok, reason = r.can_trade(initial_daily_balance=initial)
        assert not ok

    def test_passes_when_loss_below_3pct(self):
        r = _clean_risk(max_daily_drawdown_pct=0.03, max_daily_trades=100)
        initial = 10_000.0
        r.daily_pnl = -initial * 0.029  # just under limit
        ok, _ = r.can_trade(initial_daily_balance=initial)
        assert ok

    def test_passes_with_no_loss(self):
        r = _clean_risk(max_daily_drawdown_pct=0.03, max_daily_trades=100)
        ok, _ = r.can_trade(initial_daily_balance=10_000.0)
        assert ok

    def test_spec_default_is_0_03(self):
        """RiskManager() constructed without arguments must default to 3%."""
        r = _clean_risk()
        assert r.max_daily_drawdown_pct == pytest.approx(0.03)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Consecutive-loss lockout (spec default 3)
# ─────────────────────────────────────────────────────────────────────────────

class TestConsecutiveLossLockout:
    def test_3_consecutive_losses_blocks_trading(self):
        r = _clean_risk(max_consecutive_losses=3, max_daily_trades=100)
        for _ in range(3):
            r.record_close(-10.0)
        ok, reason = r.can_trade()
        assert not ok
        assert "consecutive" in reason.lower() or "Consecutive" in reason

    def test_2_consecutive_losses_do_not_block(self):
        # Use cooldown_after_loss_minutes=0 to isolate the consecutive-loss gate
        r = _clean_risk(max_consecutive_losses=3, max_daily_trades=100,
                        cooldown_after_loss_minutes=0)
        for _ in range(2):
            r.record_close(-10.0)
        ok, reason = r.can_trade()
        assert ok, f"Should not block at 2/3 losses, got: {reason}"

    def test_win_resets_consecutive_counter(self):
        r = _clean_risk(max_consecutive_losses=3, max_daily_trades=100,
                        cooldown_after_loss_minutes=0)
        for _ in range(2):
            r.record_close(-10.0)
        r.record_close(+10.0)  # win resets counter
        r.record_close(-10.0)  # one loss again
        ok, reason = r.can_trade()
        assert ok, f"Should not block after reset + 1 loss, got: {reason}"

    def test_spec_default_max_consecutive_is_3(self):
        """RiskManager() constructed without arguments must default to 3."""
        r = _clean_risk()
        assert r.max_consecutive == 3


# ─────────────────────────────────────────────────────────────────────────────
# 3. Cooldown after a loss
# ─────────────────────────────────────────────────────────────────────────────

class TestCooldownAfterLoss:
    def test_cooldown_active_immediately_after_loss(self):
        cooldown_minutes = 5  # small value for test speed
        r = _clean_risk(cooldown_after_loss_minutes=cooldown_minutes, max_daily_trades=100)
        r.record_close(-1.0)
        in_cooldown, reason = r._check_cooldown_status()
        assert in_cooldown
        assert "Cooldown" in reason or "cooldown" in reason

    def test_cooldown_not_active_without_loss(self):
        r = _clean_risk(cooldown_after_loss_minutes=5, max_daily_trades=100)
        in_cooldown, _ = r._check_cooldown_status()
        assert not in_cooldown

    def test_cooldown_not_active_after_elapsed(self):
        r = _clean_risk(cooldown_after_loss_minutes=1, max_daily_trades=100)
        # Manually place last_loss_time far in the past (> cooldown window)
        r.last_loss_time = time.time() - 120  # 2 minutes ago, cooldown = 1 min
        in_cooldown, _ = r._check_cooldown_status()
        assert not in_cooldown

    def test_can_trade_blocked_during_cooldown(self):
        r = _clean_risk(cooldown_after_loss_minutes=60, max_daily_trades=100)
        r.record_close(-1.0)
        ok, reason = r.can_trade()
        assert not ok
        assert "cooldown" in reason.lower() or "Cooldown" in reason

    def test_config_default_cooldown_is_1440_minutes(self):
        """RiskManager() constructed without arguments must default to 1440m (24h)."""
        r = _clean_risk()
        assert r.cooldown_after_loss_seconds == 1440 * 60


# ─────────────────────────────────────────────────────────────────────────────
# 4. Risk-per-trade position sizing (spec default 0.5%)
# ─────────────────────────────────────────────────────────────────────────────

class TestRiskPerTradeDefault:
    def test_trading_config_default_risk_per_trade_is_0_005(self):
        """TradingConfig() dataclass default must be 0.005 (spec: 0.5%)."""
        cfg = TradingConfig()
        assert cfg.risk_per_trade_pct == pytest.approx(0.005)

    def test_from_env_default_risk_per_trade_is_0_005(self, monkeypatch):
        """from_env() with no TRADING_PROFILE or RISK_PER_TRADE_PCT must resolve to 0.005."""
        # Unset all relevant env vars so we get pure defaults
        for var in ("TRADING_PROFILE", "RISK_PER_TRADE_PCT"):
            monkeypatch.delenv(var, raising=False)
        cfg = TradingConfig.from_env()
        assert cfg.risk_per_trade_pct == pytest.approx(0.005)

    def test_moderate_profile_risk_per_trade_unchanged(self):
        """The 'moderate' named profile must keep its explicit 0.02 value."""
        from crypto_trader.config import _TRADING_PROFILES
        assert _TRADING_PROFILES["moderate"].risk_per_trade_pct == pytest.approx(0.02)

    def test_conservative_profile_risk_per_trade_unchanged(self):
        """The 'conservative' named profile must keep its explicit 0.01 value."""
        from crypto_trader.config import _TRADING_PROFILES
        assert _TRADING_PROFILES["conservative"].risk_per_trade_pct == pytest.approx(0.01)


# ─────────────────────────────────────────────────────────────────────────────
# 5. TradingConfig fields and env overrides
# ─────────────────────────────────────────────────────────────────────────────

class TestTradingConfigRiskFields:
    def test_config_has_max_daily_drawdown_pct(self):
        cfg = TradingConfig()
        assert hasattr(cfg, "max_daily_drawdown_pct")
        assert cfg.max_daily_drawdown_pct == pytest.approx(0.03)

    def test_config_has_cooldown_after_loss_minutes(self):
        cfg = TradingConfig()
        assert hasattr(cfg, "cooldown_after_loss_minutes")
        assert cfg.cooldown_after_loss_minutes == 60

    def test_config_has_max_consecutive_losses(self):
        cfg = TradingConfig()
        assert hasattr(cfg, "max_consecutive_losses")
        assert cfg.max_consecutive_losses == 3

    def test_config_has_risk_per_trade_pct(self):
        cfg = TradingConfig()
        assert hasattr(cfg, "risk_per_trade_pct")
        assert cfg.risk_per_trade_pct == pytest.approx(0.005)

    def test_env_override_daily_max_loss_pct(self, monkeypatch):
        monkeypatch.setenv("DAILY_MAX_LOSS_PCT", "0.07")
        monkeypatch.delenv("TRADING_PROFILE", raising=False)
        cfg = TradingConfig.from_env()
        assert cfg.max_daily_drawdown_pct == pytest.approx(0.07)

    def test_env_override_loss_cooldown_minutes(self, monkeypatch):
        monkeypatch.setenv("LOSS_COOLDOWN_MINUTES", "720")
        monkeypatch.delenv("TRADING_PROFILE", raising=False)
        cfg = TradingConfig.from_env()
        assert cfg.cooldown_after_loss_minutes == 720

    def test_env_override_max_consecutive_losses(self, monkeypatch):
        monkeypatch.setenv("MAX_CONSECUTIVE_LOSSES", "5")
        monkeypatch.delenv("TRADING_PROFILE", raising=False)
        cfg = TradingConfig.from_env()
        assert cfg.max_consecutive_losses == 5

    def test_env_override_risk_per_trade_pct(self, monkeypatch):
        monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.01")
        monkeypatch.delenv("TRADING_PROFILE", raising=False)
        cfg = TradingConfig.from_env()
        assert cfg.risk_per_trade_pct == pytest.approx(0.01)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Config values flow through to RiskManager in multi_engine
# ─────────────────────────────────────────────────────────────────────────────

class TestConfigFlowsToRiskManager:
    def test_risk_manager_receives_daily_drawdown_from_config(self):
        """TradingConfig values must be passable to RiskManager constructor."""
        cfg = TradingConfig(max_daily_drawdown_pct=0.03)
        r = _clean_risk(max_daily_drawdown_pct=cfg.max_daily_drawdown_pct)
        assert r.max_daily_drawdown_pct == pytest.approx(0.03)

    def test_risk_manager_receives_cooldown_from_config(self):
        cfg = TradingConfig(cooldown_after_loss_minutes=1440)
        r = _clean_risk(cooldown_after_loss_minutes=cfg.cooldown_after_loss_minutes)
        assert r.cooldown_after_loss_seconds == 1440 * 60

    def test_risk_manager_receives_max_consecutive_from_config(self):
        cfg = TradingConfig(max_consecutive_losses=3)
        r = _clean_risk(max_consecutive_losses=cfg.max_consecutive_losses)
        assert r.max_consecutive == 3


# ─────────────────────────────────────────────────────────────────────────────
# 7. Default leverage check
# ─────────────────────────────────────────────────────────────────────────────

class TestLeverageDefaults:
    def test_default_max_leverage_is_3_in_trading_config(self):
        """TradingConfig() dataclass default max_leverage must be 3."""
        cfg = TradingConfig()
        assert cfg.max_leverage == 3

    def test_hard_cap_leverage_is_20_in_margin_engine(self):
        """LeverageEngine hard cap raised to 20x (5x–20x range allowed)."""
        from crypto_trader.margin_engine import LeverageEngine
        le = LeverageEngine()
        assert le.hard_max_leverage == 20


# ─────────────────────────────────────────────────────────────────────────────
# 8. Profile precedence: named profile values must survive into from_env()
# ─────────────────────────────────────────────────────────────────────────────

class TestProfilePrecedence:
    """Req 5 — named profiles keep their explicit risk appetites.

    Precedence:
      1. Explicit env var  →  wins when set.
      2. Named TRADING_PROFILE active, no env override  →  profile value.
      3. No profile, no env override  →  spec defaults (0.005 / 3).
    """

    def test_conservative_profile_preserves_risk_per_trade(self, monkeypatch):
        """TRADING_PROFILE=conservative + no RISK_PER_TRADE_PCT → 0.01 (profile value)."""
        monkeypatch.setenv("TRADING_PROFILE", "conservative")
        monkeypatch.delenv("RISK_PER_TRADE_PCT", raising=False)
        cfg = TradingConfig.from_env()
        assert cfg.risk_per_trade_pct == pytest.approx(0.01)

    def test_conservative_profile_preserves_max_consecutive_losses(self, monkeypatch):
        """TRADING_PROFILE=conservative + no MAX_CONSECUTIVE_LOSSES → 1 (profile value)."""
        monkeypatch.setenv("TRADING_PROFILE", "conservative")
        monkeypatch.delenv("MAX_CONSECUTIVE_LOSSES", raising=False)
        cfg = TradingConfig.from_env()
        assert cfg.max_consecutive_losses == 1

    def test_aggressive_profile_preserves_risk_per_trade(self, monkeypatch):
        """TRADING_PROFILE=aggressive + no RISK_PER_TRADE_PCT → 0.04 (profile value)."""
        monkeypatch.setenv("TRADING_PROFILE", "aggressive")
        monkeypatch.delenv("RISK_PER_TRADE_PCT", raising=False)
        cfg = TradingConfig.from_env()
        assert cfg.risk_per_trade_pct == pytest.approx(0.04)

    def test_aggressive_profile_preserves_max_consecutive_losses(self, monkeypatch):
        """TRADING_PROFILE=aggressive + no MAX_CONSECUTIVE_LOSSES → 3 (profile value)."""
        monkeypatch.setenv("TRADING_PROFILE", "aggressive")
        monkeypatch.delenv("MAX_CONSECUTIVE_LOSSES", raising=False)
        cfg = TradingConfig.from_env()
        assert cfg.max_consecutive_losses == 3

    def test_no_profile_no_env_uses_spec_default_risk(self, monkeypatch):
        """No TRADING_PROFILE + no RISK_PER_TRADE_PCT → spec default 0.005."""
        monkeypatch.delenv("TRADING_PROFILE", raising=False)
        monkeypatch.delenv("RISK_PER_TRADE_PCT", raising=False)
        cfg = TradingConfig.from_env()
        assert cfg.risk_per_trade_pct == pytest.approx(0.005)

    def test_no_profile_no_env_uses_spec_default_consecutive(self, monkeypatch):
        """No TRADING_PROFILE + no MAX_CONSECUTIVE_LOSSES → spec default 3."""
        monkeypatch.delenv("TRADING_PROFILE", raising=False)
        monkeypatch.delenv("MAX_CONSECUTIVE_LOSSES", raising=False)
        cfg = TradingConfig.from_env()
        assert cfg.max_consecutive_losses == 3

    def test_env_var_wins_over_conservative_profile_risk(self, monkeypatch):
        """TRADING_PROFILE=conservative + RISK_PER_TRADE_PCT=0.005 → env wins (0.005)."""
        monkeypatch.setenv("TRADING_PROFILE", "conservative")
        monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.005")
        cfg = TradingConfig.from_env()
        assert cfg.risk_per_trade_pct == pytest.approx(0.005)

    def test_env_var_wins_over_conservative_profile_consecutive(self, monkeypatch):
        """TRADING_PROFILE=conservative + MAX_CONSECUTIVE_LOSSES=5 → env wins (5)."""
        monkeypatch.setenv("TRADING_PROFILE", "conservative")
        monkeypatch.setenv("MAX_CONSECUTIVE_LOSSES", "5")
        cfg = TradingConfig.from_env()
        assert cfg.max_consecutive_losses == 5
