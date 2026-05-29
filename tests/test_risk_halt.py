"""
Phase 0 — Daily-loss HALT.

The pre-trade gate (_DailyDrawdownSpec) only blocks the *next* entry. A fast
intraday bleed across already-open positions can blow past the daily-loss limit
before another entry is attempted. record_close(net_pnl, current_balance=...)
must trip the hard kill switch the moment realized daily loss breaches
max_daily_drawdown_pct of the day's opening balance.
"""
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from crypto_trader.risk import RiskManager


def _clean_risk(**kw) -> RiskManager:
    """RiskManager with an isolated temp state file (never touches the dev's
    real ~/.crypto_trader/risk_state.json). Mirrors test_risk_config_driven."""
    isolated_dir = Path(tempfile.mkdtemp(prefix="risk_halt_test_"))
    with patch("crypto_trader.risk.DATA_DIR", isolated_dir):
        r = RiskManager(**kw)
    r.last_trade_date = datetime.now(timezone.utc).date()
    return r


class TestDailyLossHalt:
    def test_kill_switch_trips_at_exactly_3pct(self):
        r = _clean_risk(max_daily_drawdown_pct=0.03, max_daily_trades=100,
                        max_consecutive_losses=999)
        initial = 10_000.0
        # One close realizing exactly -3%; post-close balance = initial + pnl.
        loss = -initial * 0.03
        r.record_close(loss, current_balance=initial + loss)
        assert r.kill_switch is True
        assert "daily loss" in (r.kill_switch_reason or "").lower()

    def test_kill_switch_trips_when_exceeded(self):
        r = _clean_risk(max_daily_drawdown_pct=0.03, max_daily_trades=100,
                        max_consecutive_losses=999)
        initial = 10_000.0
        loss = -initial * 0.05
        r.record_close(loss, current_balance=initial + loss)
        assert r.kill_switch is True

    def test_no_halt_below_limit(self):
        r = _clean_risk(max_daily_drawdown_pct=0.03, max_daily_trades=100,
                        max_consecutive_losses=999)
        initial = 10_000.0
        loss = -initial * 0.029  # just under
        r.record_close(loss, current_balance=initial + loss)
        assert r.kill_switch is False

    def test_halt_accumulates_across_multiple_closes(self):
        """Each close is small; the SUM crossing 3% must halt."""
        r = _clean_risk(max_daily_drawdown_pct=0.03, max_daily_trades=100,
                        max_consecutive_losses=999)
        initial = 10_000.0
        bal = initial
        for _ in range(3):  # 3 × -1.1% = -3.3% cumulative
            loss = -initial * 0.011
            bal += loss
            r.record_close(loss, current_balance=bal)
        assert r.kill_switch is True

    def test_no_halt_when_balance_not_supplied(self):
        """Backward-compat: omitting current_balance keeps the old behavior
        (gate-only, no post-close halt)."""
        r = _clean_risk(max_daily_drawdown_pct=0.03, max_daily_trades=100,
                        max_consecutive_losses=999)
        r.record_close(-10_000.0 * 0.05)  # huge loss, no balance arg
        assert r.kill_switch is False

    def test_wins_do_not_halt(self):
        r = _clean_risk(max_daily_drawdown_pct=0.03, max_daily_trades=100)
        r.record_close(+500.0, current_balance=10_500.0)
        assert r.kill_switch is False

    def test_halted_manager_blocks_can_trade(self):
        r = _clean_risk(max_daily_drawdown_pct=0.03, max_daily_trades=100,
                        max_consecutive_losses=999)
        initial = 10_000.0
        loss = -initial * 0.04
        r.record_close(loss, current_balance=initial + loss)
        ok, reason = r.can_trade(current_balance=initial + loss,
                                 initial_daily_balance=initial)
        assert not ok
        assert "kill switch" in reason.lower()
