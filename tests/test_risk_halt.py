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
from types import SimpleNamespace
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

    def test_record_close_rolls_over_at_day_boundary(self):
        """FIX C1 — a close crossing UTC midnight must NOT accumulate onto the
        prior day's daily_pnl. Day D logs a loss; on day D+1 the FIRST close
        (before any record_open) must reset daily_pnl to ONLY day D+1's loss,
        and the daily-loss HALT must evaluate against D+1's opening balance."""
        r = _clean_risk(max_daily_drawdown_pct=0.03, max_daily_trades=100,
                        max_consecutive_losses=999)
        initial = 10_000.0

        # Day D: realize a -2.9% loss (under the 3% limit on its own).
        day_d_loss = -initial * 0.029
        r.record_close(day_d_loss, current_balance=initial + day_d_loss)
        assert r.kill_switch is False
        assert r.daily_pnl == pytest.approx(day_d_loss)

        # Advance to day D+1: stamp last_trade_date as yesterday so the rollover
        # inside record_close fires (matches a real UTC-midnight crossing).
        from datetime import timedelta
        r.last_trade_date = r._today() - timedelta(days=1)

        # Day D+1: a fresh -2.9% loss. If record_close failed to roll over, the
        # accumulated daily_pnl would be ~-5.8% (>3%) and wrongly trip the HALT.
        day_d1_loss = -initial * 0.029
        r.record_close(day_d1_loss, current_balance=initial + day_d1_loss)

        # daily_pnl reflects ONLY day D+1's loss, not the sum across the boundary.
        assert r.daily_pnl == pytest.approx(day_d1_loss)
        # And -2.9% on D+1's opening balance is under 3% -> no false-positive HALT.
        assert r.kill_switch is False
        assert r.last_trade_date == r._today()

    def test_record_close_halts_against_new_day_balance(self):
        """FIX C1 — after the day-boundary rollover, a -4% loss on day D+1 must
        still correctly trip the HALT against D+1's reconstructed opening balance
        (proving the reset doesn't suppress legitimate same-day halts)."""
        r = _clean_risk(max_daily_drawdown_pct=0.03, max_daily_trades=100,
                        max_consecutive_losses=999)
        initial = 10_000.0
        # Day D: small loss.
        r.record_close(-initial * 0.01, current_balance=initial * 0.99)
        assert r.kill_switch is False

        # Advance to day D+1.
        from datetime import timedelta
        r.last_trade_date = r._today() - timedelta(days=1)

        # Day D+1: -4% breaches the 3% limit on the new day's opening balance.
        loss = -initial * 0.04
        r.record_close(loss, current_balance=initial + loss)
        assert r.daily_pnl == pytest.approx(loss)
        assert r.kill_switch is True

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


class TestLiveClosePathWiring:
    """FIX C — prove the LIVE engine close handler passes the post-close balance
    so the daily-loss HALT actually fires (it was dead before wiring)."""

    def _shim(self, risk, balance):
        return SimpleNamespace(
            symbol="SOLUSDT",
            risk_manager=risk,
            wallet=SimpleNamespace(wallet_balance=balance),
            _persist_trade_outcome=lambda event: None,
        )

    def test_live_close_handler_trips_halt_at_breach(self):
        from crypto_trader.engine_ws import WebSocketTradingEngine
        r = _clean_risk(max_daily_drawdown_pct=0.03, max_daily_trades=100,
                        max_consecutive_losses=999)
        initial = 10_000.0
        loss = -initial * 0.04  # -4% breaches the 3% limit
        event = SimpleNamespace(symbol="SOLUSDT", realized_pnl=loss)
        shim = self._shim(r, balance=initial + loss)
        WebSocketTradingEngine._on_trade_closed(shim, event)
        assert r.kill_switch is True
        assert "daily loss" in (r.kill_switch_reason or "").lower()

    def test_live_close_handler_no_halt_below_breach(self):
        from crypto_trader.engine_ws import WebSocketTradingEngine
        r = _clean_risk(max_daily_drawdown_pct=0.03, max_daily_trades=100,
                        max_consecutive_losses=999)
        initial = 10_000.0
        loss = -initial * 0.02  # -2%, under the limit
        event = SimpleNamespace(symbol="SOLUSDT", realized_pnl=loss)
        shim = self._shim(r, balance=initial + loss)
        WebSocketTradingEngine._on_trade_closed(shim, event)
        assert r.kill_switch is False
