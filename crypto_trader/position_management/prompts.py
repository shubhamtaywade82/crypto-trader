"""
crypto_trader.position_management.prompts
==========================================
Prompt templates for the AI position management assessor.

These are separate from the entry-signal prompts so the LLM is given
a focused, position-management framing rather than an entry-evaluation one.
"""

from __future__ import annotations

import json

from .schemas import PositionManagementInput


SYSTEM_PROMPT = """You are an expert futures position manager with deep knowledge of
risk management, market structure, and trading psychology.

Your role is to assess open positions and recommend ONE management action.
You must output ONLY valid JSON matching this exact schema:

{
  "action": "<one of: KEEP_OPEN | TIGHTEN_SL | LOOSEN_SL | MOVE_SL_TO_BREAKEVEN | TRAIL_SL | TAKE_PARTIAL_PROFIT | FULL_EXIT | SCALE_IN | SCALE_OUT | REVERSE_AND_REOPEN | HOLD_AND_MONITOR | PAUSE_MANAGEMENT>",
  "confidence": <float 0.0 to 1.0>,
  "reason": "<concise one-sentence rationale>",
  "risk_effect": {
    "delta_risk": <float, negative means risk reduction>,
    "capital_freed": <float>
  },
  "execution": {
    "type": "<keep | modify_stop | partial_exit | full_exit | scale_in>",
    "new_stop_loss": <float or null>,
    "new_take_profit": <float or null>,
    "exit_qty_pct": <float 0.0-1.0 or null>,
    "scale_qty_pct": <float or null>,
    "trail_distance_pct": <float or null>
  },
  "fallback": "<HOLD_AND_MONITOR or KEEP_OPEN>"
}

Critical constraints (non-negotiable):
1. NEVER recommend removing a stop loss. new_stop_loss must always be set if action modifies the stop.
2. NEVER recommend LOOSEN_SL unless constraints.allow_loosen_sl is true.
3. NEVER recommend SCALE_IN unless constraints.allow_scale_in is true and the position is already profitable.
4. NEVER recommend averaging down into a losing position unless constraints.allow_average_down is true.
5. When confidence is below 0.60, default to HOLD_AND_MONITOR or reduce risk — never expand.
6. When data is stale (is_stale=true), recommend PAUSE_MANAGEMENT only.
7. When portfolio risk is near the cap, do not recommend SCALE_IN.
8. FULL_EXIT is always safe to recommend when thesis is invalidated.

Management decision hierarchy (apply in order):
1. Protect capital (FULL_EXIT, TIGHTEN_SL, MOVE_SL_TO_BREAKEVEN)
2. Reduce exposure (TAKE_PARTIAL_PROFIT, SCALE_OUT)
3. Optimise remaining position (TRAIL_SL, TIGHTEN_SL structurally)
4. Expand only if justified (SCALE_IN — requires all conditions met)"""


def build_user_prompt(inp: PositionManagementInput, score: float, factors: list[str]) -> str:
    """Serialise the full PositionManagementInput as the LLM user message."""
    pos = inp.position
    market = inp.market
    wallet = inp.wallet
    portfolio = inp.portfolio
    constraints = inp.constraints

    payload = {
        "position": {
            "id": pos.position_id,
            "symbol": pos.symbol,
            "side": pos.side,
            "qty": pos.qty,
            "entry_price": pos.entry_price,
            "ltp": pos.ltp,
            "unrealized_pnl": round(pos.unrealized_pnl, 4),
            "unrealized_pnl_pct": round(pos.unrealized_pnl_pct * 100, 2),
            "stop_loss": pos.stop_loss,
            "take_profit": pos.take_profit,
            "tp_levels": pos.tp_levels,
            "trailing_active": pos.trailing_active,
            "trailing_stop_price": pos.trailing_stop_price,
            "age_hours": round(pos.age_hours, 1),
            "leverage": pos.leverage,
            "r_multiple": round(pos.r_multiple, 2),
            "playbook": pos.playbook,
        },
        "market": {
            "regime": market.regime,
            "trend": market.trend,
            "structure": market.structure,
            "volatility_state": market.volatility_state,
            "momentum": market.momentum,
            "atr_pct": round(market.atr_pct * 100, 3),
            "vwap_distance_pct": round(market.vwap_distance_pct * 100, 3),
            "liquidity": market.liquidity,
            "near_support": market.near_support,
            "near_resistance": market.near_resistance,
            "event_risk": market.event_risk,
            "funding_rate": market.funding_rate,
            "oi_change_pct": round(market.oi_change_pct, 2),
            "spread_pct": round(market.spread_pct * 100, 4),
            "is_stale": market.is_stale,
        },
        "wallet": {
            "equity": round(wallet.equity, 2),
            "free_capital": round(wallet.free_capital, 2),
            "blocked_capital": round(wallet.blocked_capital, 2),
            "used_margin": round(wallet.used_margin, 2),
            "margin_ratio_pct": round(wallet.margin_ratio * 100, 1),
        },
        "portfolio": {
            "open_positions": portfolio.open_positions,
            "correlation_risk": portfolio.correlation_risk,
            "current_total_risk": round(portfolio.current_total_risk, 2),
            "max_total_risk": round(portfolio.max_total_risk, 2),
            "risk_budget_remaining_pct": round(
                (1 - portfolio.current_total_risk / portfolio.max_total_risk) * 100
                if portfolio.max_total_risk else 100, 1
            ),
        },
        "constraints": {
            "allow_scale_in": constraints.allow_scale_in,
            "allow_partial_exit": constraints.allow_partial_exit,
            "allow_breakeven_trail": constraints.allow_breakeven_trail,
            "allow_average_down": constraints.allow_average_down,
            "allow_loosen_sl": constraints.allow_loosen_sl,
            "min_r_for_breakeven": constraints.min_r_for_breakeven,
            "min_r_for_partial_exit": constraints.min_r_for_partial_exit,
            "trail_atr_multiplier": constraints.trail_atr_multiplier,
        },
        "deterministic_score": {
            "score": round(score, 1),
            "top_factors": factors[:8],
            "interpretation": _score_band(score),
        },
    }
    return (
        "Assess the following open position and return a single JSON management decision.\n\n"
        + json.dumps(payload, indent=2)
    )


def _score_band(score: float) -> str:
    if score >= 80:
        return "STRONG — hold and optimise"
    if score >= 60:
        return "MODERATE — monitor and tighten"
    if score >= 40:
        return "WEAK — reduce risk"
    if score >= 20:
        return "POOR — exit or trim"
    return "CRITICAL — force exit"
