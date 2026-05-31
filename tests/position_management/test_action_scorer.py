"""Tests for the deterministic ActionScorer."""

import pytest
from crypto_trader.position_management.schemas import (
    MarketContext,
    PortfolioContext,
    PositionActionType,
    PositionConstraints,
    PositionManagementInput,
    PositionSnapshot,
    WalletContext,
)
from crypto_trader.position_management.action_scorer import (
    compute_breakeven_stop,
    compute_trail_stop,
    score_position,
    score_to_candidate_actions,
)


def _make_inp(
    side="LONG",
    r_multiple=2.0,
    trend="up",
    structure="BOS_UP",
    momentum="STRONG_UP",
    volatility="expanding",
    liquidity="strong",
    event_risk="none",
    spread_pct=0.0005,
    is_stale=False,
    age_hours=4.0,
    entry_price=100.0,
    ltp=104.0,
    stop_loss=98.0,
    free_capital_ratio=0.5,
    correlation="low",
    current_risk=1000.0,
    max_risk=4000.0,
) -> PositionManagementInput:
    equity = 100_000.0
    free = equity * free_capital_ratio
    pos = PositionSnapshot(
        position_id="test_P1",
        symbol="BTCUSDT",
        side=side,
        qty=0.01,
        entry_price=entry_price,
        ltp=ltp,
        unrealized_pnl=(ltp - entry_price) * 0.01,
        unrealized_pnl_pct=(ltp - entry_price) / entry_price,
        stop_loss=stop_loss,
        r_multiple=r_multiple,
        market_regime="TREND_EXPANSION",
        volatility_state=volatility,
        liquidity_state=liquidity,
        trend_direction=trend,
        market_structure=structure,
        momentum_state=momentum,
        event_risk=event_risk,
        spread_pct=spread_pct,
        age_hours=age_hours,
        is_stale=is_stale,
        wallet_balance=equity,
        free_capital=free,
        playbook="INTRADAY",
    )
    mctx = MarketContext(
        symbol="BTCUSDT",
        trend=trend,
        regime="TREND_EXPANSION",
        volatility_state=volatility,
        structure=structure,
        momentum=momentum,
        liquidity=liquidity,
        event_risk=event_risk,
        spread_pct=spread_pct,
        is_stale=is_stale,
    )
    wallet = WalletContext(
        equity=equity,
        free_capital=free,
        blocked_capital=equity - free,
        used_margin=equity * 0.1,
        margin_ratio=0.1,
    )
    portfolio = PortfolioContext(
        open_positions=2,
        correlation_risk=correlation,
        max_total_risk=max_risk,
        current_total_risk=current_risk,
    )
    constraints = PositionConstraints()
    return PositionManagementInput(
        position=pos, market=mctx, wallet=wallet,
        portfolio=portfolio, constraints=constraints,
    )


class TestScorePosition:
    def test_ideal_long_scores_high(self):
        inp = _make_inp()
        score, factors = score_position(inp)
        assert score >= 80, f"Expected score >= 80 for ideal long, got {score}"
        assert any("trend_aligned" in f for f in factors)
        assert any("structure_intact" in f for f in factors)

    def test_stale_data_lowers_score(self):
        # Use a neutral position (no positive/negative extremes) so stale makes a measurable difference
        inp_normal = _make_inp(trend="neutral", structure="RANGE", momentum="NEUTRAL",
                               r_multiple=0.0, event_risk="none")
        inp_stale  = _make_inp(trend="neutral", structure="RANGE", momentum="NEUTRAL",
                               r_multiple=0.0, event_risk="none", is_stale=True)
        score_n, _ = score_position(inp_normal)
        score_s, factors_s = score_position(inp_stale)
        assert score_s < score_n
        assert any("data_stale" in f for f in factors_s)

    def test_broken_structure_penalised(self):
        base = dict(trend="neutral", momentum="NEUTRAL", r_multiple=0.5, event_risk="none")
        inp_good   = _make_inp(**base, structure="BOS_UP")
        inp_broken = _make_inp(**base, structure="BOS_DOWN")
        score_g, _ = score_position(inp_good)
        score_b, factors_b = score_position(inp_broken)
        assert score_b < score_g
        assert any("structure_broken" in f for f in factors_b)

    def test_event_risk_penalised(self):
        base = dict(trend="neutral", structure="RANGE", momentum="NEUTRAL", r_multiple=0.5)
        inp_clean = _make_inp(**base, event_risk="none")
        inp_risky = _make_inp(**base, event_risk="high")
        s_clean, _ = score_position(inp_clean)
        s_risky, _ = score_position(inp_risky)
        assert s_risky < s_clean

    def test_high_correlation_penalised(self):
        base = dict(trend="neutral", structure="RANGE", momentum="NEUTRAL", r_multiple=0.5)
        inp_low  = _make_inp(**base, correlation="low")
        inp_high = _make_inp(**base, correlation="high")
        s_low, _ = score_position(inp_low)
        s_high, _ = score_position(inp_high)
        assert s_high < s_low

    def test_score_clamped_to_0_100(self):
        inp = _make_inp(
            structure="BOS_DOWN",
            momentum="STRONG_DOWN",
            event_risk="high",
            correlation="high",
            spread_pct=0.01,
            is_stale=True,
            age_hours=20,
        )
        score, _ = score_position(inp)
        assert 0 <= score <= 100

    def test_profitable_position_rewarded(self):
        base = dict(trend="neutral", structure="RANGE", momentum="NEUTRAL", event_risk="none")
        inp_profit = _make_inp(**base, r_multiple=2.0)
        inp_flat   = _make_inp(**base, r_multiple=0.0)
        s_p, _ = score_position(inp_profit)
        s_f, _ = score_position(inp_flat)
        assert s_p > s_f


class TestScoreToCandidates:
    def test_high_score_keeps_open(self):
        actions = score_to_candidate_actions(85)
        assert PositionActionType.KEEP_OPEN in actions

    def test_low_score_forces_exit(self):
        actions = score_to_candidate_actions(15)
        assert PositionActionType.FULL_EXIT in actions
        assert len(actions) == 1

    def test_medium_score_partial_exit(self):
        actions = score_to_candidate_actions(45)
        assert PositionActionType.TAKE_PARTIAL_PROFIT in actions

    def test_poor_score_suggests_exit_or_trim(self):
        actions = score_to_candidate_actions(30)
        assert PositionActionType.FULL_EXIT in actions


class TestTrailStop:
    def test_trail_moves_sl_up_for_long(self):
        inp = _make_inp(entry_price=100.0, ltp=110.0, stop_loss=98.0)
        inp.position.atr_pct = 0.02  # 2% ATR
        new_sl = compute_trail_stop(inp, trail_atr_multiplier=1.5)
        assert new_sl > 98.0, "Trail stop should move up for profitable LONG"
        assert new_sl < 110.0, "Trail stop should be below LTP"

    def test_trail_never_moves_sl_down(self):
        inp = _make_inp(entry_price=100.0, ltp=101.0, stop_loss=98.0)
        inp.position.atr_pct = 0.05  # very wide ATR
        new_sl = compute_trail_stop(inp, trail_atr_multiplier=1.5)
        assert new_sl >= 98.0, "Trail should never move SL further from entry for LONG"


class TestBreakevenStop:
    def test_breakeven_above_entry_for_long(self):
        inp = _make_inp(entry_price=100.0, ltp=105.0, stop_loss=97.0)
        inp.position.atr_pct = 0.01
        be_sl = compute_breakeven_stop(inp)
        assert be_sl > 100.0, "Breakeven stop for LONG should be above entry"

    def test_breakeven_below_entry_for_short(self):
        inp = _make_inp(side="SHORT", entry_price=100.0, ltp=95.0, stop_loss=103.0, trend="down",
                        structure="BOS_DOWN", momentum="STRONG_DOWN")
        inp.position.atr_pct = 0.01
        be_sl = compute_breakeven_stop(inp)
        assert be_sl < 100.0, "Breakeven stop for SHORT should be below entry"
