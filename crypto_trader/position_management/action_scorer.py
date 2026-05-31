"""
crypto_trader.position_management.action_scorer
================================================
Layer 2 (deterministic): Computes a position-health score (0–100) and maps
it to candidate actions.  This runs BEFORE the AI assessor so that:

  1. The AI receives the score + top factors as additional context.
  2. The policy guard can use score-band overrides when confidence is low.

Score bands:
  80–100 → KEEP_OPEN / TRAIL_SL / TIGHTEN_SL
  60–79  → HOLD_AND_MONITOR / TIGHTEN_SL / MOVE_SL_TO_BREAKEVEN
  40–59  → TAKE_PARTIAL_PROFIT / SCALE_OUT / TIGHTEN_SL
  20–39  → FULL_EXIT / TAKE_PARTIAL_PROFIT
   0–19  → FULL_EXIT (forced — risk protection first)
"""

from __future__ import annotations

from typing import List, Tuple

from .schemas import PositionActionType, PositionManagementInput

# ── Factor weights (sum-of-parts, not normalised — raw score then clamped to 0–100) ──

_POSITIVE: dict[str, int] = {
    "trend_aligned":        20,
    "structure_intact":     15,
    "profit_gt_1r":         15,
    "momentum_strong":      10,
    "liquidity_favorable":   8,
    "volatility_supports":   7,
    "no_event_risk":         7,
    "low_spread":            5,
    "capital_efficient":     5,
    "healthy_age":           8,
}

_NEGATIVE: dict[str, int] = {
    "structure_broken":    -30,
    "momentum_fading":     -15,
    "near_exhaustion":     -10,
    "high_correlation":     -8,
    "low_free_capital":     -7,
    "event_risk_near":     -10,
    "spread_widening":      -5,
    "position_stale":      -8,
    "sl_at_risk":          -15,
    "margin_headroom_low":  -7,
}

# Starting baseline — neutral position gets 50
_BASELINE = 50


def score_position(inp: PositionManagementInput) -> Tuple[float, List[str]]:
    """
    Returns (score: float 0–100, active_factors: List[str]).
    active_factors lists every factor that contributed ≠ 0.
    """
    pos = inp.position
    market = inp.market
    wallet = inp.wallet
    portfolio = inp.portfolio

    delta = 0
    active: List[str] = []

    def add(name: str, value: int):
        nonlocal delta
        delta += value
        active.append(f"{name}:{'+' if value >= 0 else ''}{value}")

    # ── Positive factors ───────────────────────────────────────────────────

    # trend_aligned: position direction matches market trend
    if (pos.side == "LONG" and pos.trend_direction == "up") or \
       (pos.side == "SHORT" and pos.trend_direction == "down"):
        add("trend_aligned", _POSITIVE["trend_aligned"])

    # structure_intact: no BOS against position
    structure_up = pos.market_structure in ("BOS_UP", "CHOCH_UP")
    structure_down = pos.market_structure in ("BOS_DOWN", "CHOCH_DOWN")
    broken_against = (pos.side == "LONG" and structure_down) or \
                     (pos.side == "SHORT" and structure_up)
    if not broken_against and pos.market_structure not in ("UNKNOWN", "RANGE"):
        add("structure_intact", _POSITIVE["structure_intact"])

    # profit_gt_1r: position is at least 1R in profit
    if pos.r_multiple >= 1.0:
        add("profit_gt_1r", _POSITIVE["profit_gt_1r"])

    # momentum_strong
    if pos.momentum_state in ("STRONG_UP", "STRONG_DOWN"):
        if (pos.side == "LONG" and pos.momentum_state == "STRONG_UP") or \
           (pos.side == "SHORT" and pos.momentum_state == "STRONG_DOWN"):
            add("momentum_strong", _POSITIVE["momentum_strong"])

    # liquidity_favorable
    if pos.liquidity_state == "strong":
        add("liquidity_favorable", _POSITIVE["liquidity_favorable"])

    # volatility_supports: expanding vol in a trend is good for runners
    if pos.volatility_state == "expanding" and pos.trend_direction in ("up", "down"):
        add("volatility_supports", _POSITIVE["volatility_supports"])

    # no_event_risk
    if pos.event_risk == "none":
        add("no_event_risk", _POSITIVE["no_event_risk"])

    # low_spread
    if pos.spread_pct < 0.001:
        add("low_spread", _POSITIVE["low_spread"])

    # capital_efficient: free capital > 30% of equity
    if wallet.equity > 0 and wallet.free_capital / wallet.equity >= 0.30:
        add("capital_efficient", _POSITIVE["capital_efficient"])

    # healthy_age: intraday: not too old (< 12h), swing: < 48h
    if pos.playbook == "SWING":
        if pos.age_hours < 48:
            add("healthy_age", _POSITIVE["healthy_age"])
    else:
        if pos.age_hours < 12:
            add("healthy_age", _POSITIVE["healthy_age"])

    # ── Negative factors ──────────────────────────────────────────────────

    # structure_broken: BOS against position side
    if broken_against:
        add("structure_broken", _NEGATIVE["structure_broken"])

    # momentum_fading: momentum opposing the position
    momentum_against = (pos.side == "LONG" and pos.momentum_state in ("WEAK_DOWN", "STRONG_DOWN")) or \
                       (pos.side == "SHORT" and pos.momentum_state in ("WEAK_UP", "STRONG_UP"))
    if momentum_against:
        add("momentum_fading", _NEGATIVE["momentum_fading"])

    # near_exhaustion: at resistance (long) or support (short)
    if (pos.side == "LONG" and pos.near_resistance) or \
       (pos.side == "SHORT" and pos.near_support):
        add("near_exhaustion", _NEGATIVE["near_exhaustion"])

    # high_correlation
    if portfolio.correlation_risk == "high":
        add("high_correlation", _NEGATIVE["high_correlation"])
    elif portfolio.correlation_risk == "moderate":
        delta += _NEGATIVE["high_correlation"] // 2
        active.append(f"moderate_correlation:{_NEGATIVE['high_correlation'] // 2}")

    # low_free_capital: < 10% free
    if wallet.equity > 0 and wallet.free_capital / wallet.equity < 0.10:
        add("low_free_capital", _NEGATIVE["low_free_capital"])

    # event_risk_near
    if pos.event_risk in ("low", "high"):
        add("event_risk_near", _NEGATIVE["event_risk_near"])

    # spread_widening
    if pos.spread_pct > 0.003:
        add("spread_widening", _NEGATIVE["spread_widening"])

    # position_stale: no progress in profit for intraday
    if pos.playbook != "SWING" and pos.age_hours > 16:
        add("position_stale", _NEGATIVE["position_stale"])
    elif pos.playbook == "SWING" and pos.age_hours > 72:
        add("position_stale", _NEGATIVE["position_stale"])

    # sl_at_risk: LTP is within 0.3R of stop
    sl_distance = abs(pos.ltp - pos.stop_loss)
    initial_risk = abs(pos.entry_price - pos.stop_loss) if pos.stop_loss else 1.0
    if initial_risk > 0 and sl_distance / initial_risk < 0.3:
        add("sl_at_risk", _NEGATIVE["sl_at_risk"])

    # margin_headroom_low: margin ratio > 75%
    if wallet.margin_ratio > 0.75:
        add("margin_headroom_low", _NEGATIVE["margin_headroom_low"])

    # data_stale — always flag
    if pos.is_stale:
        add("data_stale", -20)

    score = max(0.0, min(100.0, _BASELINE + delta))
    return score, active


def score_to_candidate_actions(score: float) -> List[PositionActionType]:
    """Map a score to ordered candidate actions (best first)."""
    if score >= 80:
        return [
            PositionActionType.KEEP_OPEN,
            PositionActionType.TRAIL_SL,
            PositionActionType.TIGHTEN_SL,
        ]
    if score >= 60:
        return [
            PositionActionType.HOLD_AND_MONITOR,
            PositionActionType.TIGHTEN_SL,
            PositionActionType.MOVE_SL_TO_BREAKEVEN,
        ]
    if score >= 40:
        return [
            PositionActionType.TAKE_PARTIAL_PROFIT,
            PositionActionType.SCALE_OUT,
            PositionActionType.TIGHTEN_SL,
        ]
    if score >= 20:
        return [
            PositionActionType.FULL_EXIT,
            PositionActionType.TAKE_PARTIAL_PROFIT,
        ]
    return [PositionActionType.FULL_EXIT]


def compute_trail_stop(
    inp: PositionManagementInput,
    trail_atr_multiplier: float = 1.5,
) -> float:
    """Compute a new trailing stop price based on ATR and current position."""
    pos = inp.position
    atr = pos.atr_pct * pos.ltp if pos.atr_pct else 0.0
    trail_distance = atr * trail_atr_multiplier

    if pos.side == "LONG":
        new_sl = pos.ltp - trail_distance
        # Never move SL downward (only trail up)
        return max(new_sl, pos.stop_loss)
    else:
        new_sl = pos.ltp + trail_distance
        # Never move SL upward for shorts (only trail down)
        return min(new_sl, pos.stop_loss)


def compute_breakeven_stop(inp: PositionManagementInput) -> float:
    """Return entry price ± buffer as breakeven stop."""
    pos = inp.position
    atr = pos.atr_pct * pos.ltp if pos.atr_pct else pos.entry_price * 0.001
    buffer = min(atr * 0.1, pos.entry_price * 0.001)  # tiny buffer
    if pos.side == "LONG":
        return pos.entry_price + buffer
    return pos.entry_price - buffer
