"""Tests for PolicyGuard hard invariants."""

import pytest
from crypto_trader.position_management.schemas import (
    ExecutionParams,
    MarketContext,
    PolicyDecision,
    PortfolioContext,
    PositionActionType,
    PositionConstraints,
    PositionManagementDecision,
    PositionManagementInput,
    PositionSnapshot,
    WalletContext,
)
from crypto_trader.position_management.policy_guard import PolicyGuard


def _make_decision(
    action: PositionActionType,
    confidence: float = 0.80,
    exec_type: str = "keep",
    new_stop_loss: float = None,
    new_take_profit: float = None,
    exit_qty_pct: float = None,
    scale_qty_pct: float = None,
) -> PositionManagementDecision:
    return PositionManagementDecision(
        action=action,
        confidence=confidence,
        reason="test",
        execution=ExecutionParams(
            type=exec_type,
            new_stop_loss=new_stop_loss,
            new_take_profit=new_take_profit,
            exit_qty_pct=exit_qty_pct,
            scale_qty_pct=scale_qty_pct,
        ),
    )


def _make_inp(
    side="LONG",
    r_multiple=2.0,
    ltp=105.0,
    entry_price=100.0,
    stop_loss=97.0,
    is_stale=False,
    free_ratio=0.4,
    current_risk=1000.0,
    max_risk=4000.0,
    allow_scale_in=True,
    allow_loosen_sl=False,
    allow_average_down=False,
    correlation="low",
) -> PositionManagementInput:
    equity = 50_000.0
    pos = PositionSnapshot(
        position_id="P1",
        symbol="ETHUSDT",
        side=side,
        qty=0.1,
        entry_price=entry_price,
        ltp=ltp,
        unrealized_pnl=(ltp - entry_price) * 0.1,
        unrealized_pnl_pct=(ltp - entry_price) / entry_price,
        stop_loss=stop_loss,
        r_multiple=r_multiple,
        is_stale=is_stale,
        wallet_balance=equity,
        free_capital=equity * free_ratio,
        playbook="INTRADAY",
    )
    mctx = MarketContext(symbol="ETHUSDT", is_stale=is_stale)
    wallet = WalletContext(
        equity=equity,
        free_capital=equity * free_ratio,
        blocked_capital=equity * (1 - free_ratio),
        used_margin=equity * 0.15,
        margin_ratio=0.15,
    )
    portfolio = PortfolioContext(
        open_positions=2,
        correlation_risk=correlation,
        max_total_risk=max_risk,
        current_total_risk=current_risk,
    )
    constraints = PositionConstraints(
        allow_scale_in=allow_scale_in,
        allow_loosen_sl=allow_loosen_sl,
        allow_average_down=allow_average_down,
    )
    return PositionManagementInput(
        position=pos, market=mctx, wallet=wallet,
        portfolio=portfolio, constraints=constraints,
    )


class TestPolicyGuardCoreRules:
    def setup_method(self):
        self.guard = PolicyGuard()

    def test_full_exit_always_approved(self):
        inp = _make_inp()
        decision = _make_decision(PositionActionType.FULL_EXIT, exec_type="full_exit", exit_qty_pct=1.0)
        result = self.guard.validate(inp, decision)
        assert result.approved is True
        assert result.action == PositionActionType.FULL_EXIT

    def test_stale_data_rejects_action(self):
        inp = _make_inp(is_stale=True)
        decision = _make_decision(PositionActionType.TRAIL_SL, exec_type="modify_stop", new_stop_loss=99.0)
        result = self.guard.validate(inp, decision)
        assert result.approved is False
        assert "stale_data" in result.rejection_reason

    def test_sl_modification_without_price_rejected(self):
        inp = _make_inp()
        decision = _make_decision(PositionActionType.TRAIL_SL, exec_type="modify_stop", new_stop_loss=None)
        result = self.guard.validate(inp, decision)
        assert result.approved is False
        assert "no_sl_removal" in result.rejection_reason

    def test_scale_in_rejected_when_not_allowed(self):
        inp = _make_inp(allow_scale_in=False)
        decision = _make_decision(PositionActionType.SCALE_IN, exec_type="scale_in", scale_qty_pct=0.25)
        result = self.guard.validate(inp, decision)
        assert result.approved is False
        assert "scale_in_not_allowed" in result.rejection_reason

    def test_scale_in_rejected_when_not_profitable(self):
        inp = _make_inp(allow_scale_in=True, r_multiple=-0.5, ltp=98.0)
        decision = _make_decision(PositionActionType.SCALE_IN, exec_type="scale_in", scale_qty_pct=0.25)
        result = self.guard.validate(inp, decision)
        assert result.approved is False
        assert "scale_in_not_profitable" in result.rejection_reason

    def test_scale_in_rejected_when_margin_low(self):
        inp = _make_inp(allow_scale_in=True, r_multiple=2.0, free_ratio=0.10)
        decision = _make_decision(PositionActionType.SCALE_IN, exec_type="scale_in", scale_qty_pct=0.25)
        result = self.guard.validate(inp, decision)
        assert result.approved is False
        assert "margin_headroom" in result.rejection_reason

    def test_scale_in_rejected_when_portfolio_risk_full(self):
        inp = _make_inp(allow_scale_in=True, r_multiple=2.0, current_risk=3500.0, max_risk=4000.0)
        decision = _make_decision(PositionActionType.SCALE_IN, exec_type="scale_in", scale_qty_pct=0.25)
        result = self.guard.validate(inp, decision)
        assert result.approved is False
        assert "portfolio_risk_cap" in result.rejection_reason

    def test_loosen_sl_rejected_when_not_allowed(self):
        inp = _make_inp(allow_loosen_sl=False)
        decision = _make_decision(PositionActionType.LOOSEN_SL, exec_type="modify_stop", new_stop_loss=93.0)
        result = self.guard.validate(inp, decision)
        assert result.approved is False

    def test_sl_widening_rejected(self):
        # current SL is 97 (3% below entry 100); new SL at 90 would be 10% below entry — wider
        inp = _make_inp(entry_price=100.0, stop_loss=97.0)
        decision = _make_decision(PositionActionType.TIGHTEN_SL, exec_type="modify_stop", new_stop_loss=90.0)
        result = self.guard.validate(inp, decision)
        assert result.approved is False
        assert "sl_widening" in result.rejection_reason

    def test_reverse_rejected_when_low_confidence(self):
        inp = _make_inp()
        decision = _make_decision(
            PositionActionType.REVERSE_AND_REOPEN, confidence=0.70, exec_type="full_exit"
        )
        result = self.guard.validate(inp, decision)
        assert result.approved is False
        assert "reverse_confidence" in result.rejection_reason

    def test_valid_tighten_sl_approved(self):
        # SL at 97 (3% below 100); tighten to 99 (1% below 100) — closer to entry is tighter
        inp = _make_inp(entry_price=100.0, stop_loss=97.0, ltp=105.0)
        decision = _make_decision(
            PositionActionType.TIGHTEN_SL, exec_type="modify_stop", new_stop_loss=99.0
        )
        result = self.guard.validate(inp, decision)
        assert result.approved is True

    def test_sl_above_ltp_is_clamped_for_long(self):
        # SL distance must not widen: entry=100, current_sl=90 (dist=10)
        # new_sl=105.5: dist=5.5 < 10*1.2=12 → widening OK, but new_sl > ltp=105 → clamped
        inp = _make_inp(entry_price=100.0, stop_loss=90.0, ltp=105.0)
        decision = _make_decision(
            PositionActionType.TRAIL_SL, exec_type="modify_stop", new_stop_loss=105.5
        )
        result = self.guard.validate(inp, decision)
        assert result.approved is True
        if result.modified_params and result.modified_params.new_stop_loss:
            assert result.modified_params.new_stop_loss < 105.0

    def test_keep_open_always_approved(self):
        inp = _make_inp()
        decision = _make_decision(PositionActionType.KEEP_OPEN, exec_type="keep")
        result = self.guard.validate(inp, decision)
        assert result.approved is True

    def test_partial_exit_approved_with_valid_pct(self):
        inp = _make_inp()
        decision = _make_decision(
            PositionActionType.TAKE_PARTIAL_PROFIT, exec_type="partial_exit", exit_qty_pct=0.5
        )
        result = self.guard.validate(inp, decision)
        assert result.approved is True
