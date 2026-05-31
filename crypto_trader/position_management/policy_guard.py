"""
crypto_trader.position_management.policy_guard
===============================================
Layer 3 (rules): Hard invariant enforcement using the Specification pattern.

The PolicyGuard is the final gate before any order reaches the exchange.
It approves, rejects, or modifies AI recommendations.  No AI decision can
bypass this layer.

Non-negotiable invariants:
  1. No SL removal
  2. No SL widening beyond ATR buffer
  3. No SCALE_IN if margin headroom < configured threshold
  4. No SCALE_IN if portfolio risk would exceed cap
  5. No averaging down unless explicitly allowed
  6. No LOOSEN_SL unless strategy permits
  7. No action on stale / disconnected data
  8. FULL_EXIT is always allowed (protecting capital is highest priority)
  9. No TP widening without adequate risk budget
 10. REVERSE_AND_REOPEN requires confidence ≥ 0.85
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from ..patterns.specification import Specification
from .schemas import (
    ExecutionParams,
    PolicyDecision,
    PositionActionType,
    PositionManagementDecision,
    PositionManagementInput,
)

logger = logging.getLogger("crypto_trader.position_management.policy_guard")

# Hard thresholds
_MIN_MARGIN_HEADROOM_FOR_SCALE_IN = 0.25   # 25% free capital required
_MAX_RISK_UTILISATION_FOR_SCALE_IN = 0.80  # portfolio risk must be < 80% of cap
_MIN_CONFIDENCE_REVERSE = 0.85
_MAX_SL_WIDEN_FACTOR = 1.20               # allow at most 20% wider SL
_MAX_TP_WIDEN_FACTOR = 1.50               # allow at most 50% wider TP


# ── Specification objects ─────────────────────────────────────────────────

@dataclass
class _PolicyCtx:
    inp: PositionManagementInput
    decision: PositionManagementDecision


class _StaleDataSpec(Specification["_PolicyCtx"]):
    def is_satisfied_by(self, ctx: "_PolicyCtx") -> Tuple[bool, str]:
        if ctx.inp.position.is_stale or ctx.inp.market.is_stale:
            if ctx.decision.action != PositionActionType.PAUSE_MANAGEMENT:
                return False, "stale_data: feed disconnected or data > 60s"
        return True, ""


class _NoSLRemovalSpec(Specification["_PolicyCtx"]):
    def is_satisfied_by(self, ctx: "_PolicyCtx") -> Tuple[bool, str]:
        exec_p = ctx.decision.execution
        action = ctx.decision.action
        modifies_sl = action in (
            PositionActionType.TIGHTEN_SL,
            PositionActionType.LOOSEN_SL,
            PositionActionType.MOVE_SL_TO_BREAKEVEN,
            PositionActionType.TRAIL_SL,
        )
        if modifies_sl and exec_p.new_stop_loss is None:
            return False, "no_sl_removal: SL-modification action must supply new_stop_loss"
        return True, ""


class _NoSLWideningSpec(Specification["_PolicyCtx"]):
    def is_satisfied_by(self, ctx: "_PolicyCtx") -> Tuple[bool, str]:
        exec_p = ctx.decision.execution
        pos = ctx.inp.position
        if exec_p.new_stop_loss is None:
            return True, ""
        current_sl = pos.stop_loss
        if current_sl == 0:
            return True, ""
        entry = pos.entry_price
        # Distance from entry to current SL
        current_dist = abs(entry - current_sl)
        new_dist = abs(entry - exec_p.new_stop_loss)
        # Widening = new distance is larger than old distance
        if new_dist > current_dist * _MAX_SL_WIDEN_FACTOR:
            return False, f"sl_widening: new SL widens risk by {new_dist/current_dist:.2f}x (max {_MAX_SL_WIDEN_FACTOR}x)"
        # Also reject LOOSEN_SL if strategy does not permit it
        if ctx.decision.action == PositionActionType.LOOSEN_SL:
            if not ctx.inp.constraints.allow_loosen_sl:
                return False, "loosen_sl_not_allowed: strategy does not permit SL loosening"
        return True, ""


class _ScaleInSafetySpec(Specification["_PolicyCtx"]):
    def is_satisfied_by(self, ctx: "_PolicyCtx") -> Tuple[bool, str]:
        if ctx.decision.action != PositionActionType.SCALE_IN:
            return True, ""
        constraints = ctx.inp.constraints
        wallet = ctx.inp.wallet
        portfolio = ctx.inp.portfolio
        pos = ctx.inp.position

        # Strategy must allow scale-in
        if not constraints.allow_scale_in:
            return False, "scale_in_not_allowed: strategy does not permit SCALE_IN"

        # Position must be profitable
        if pos.r_multiple < 0.5:
            return False, f"scale_in_not_profitable: r_multiple={pos.r_multiple:.2f} < 0.5"

        # No averaging down
        if not constraints.allow_average_down and pos.unrealized_pnl <= 0:
            return False, "no_avg_down: position is not in profit"

        # Margin headroom check
        free_ratio = wallet.free_capital / wallet.equity if wallet.equity else 0.0
        if free_ratio < _MIN_MARGIN_HEADROOM_FOR_SCALE_IN:
            return False, f"margin_headroom: free={free_ratio:.1%} < {_MIN_MARGIN_HEADROOM_FOR_SCALE_IN:.1%}"

        # Portfolio risk cap check
        risk_ratio = portfolio.current_total_risk / portfolio.max_total_risk if portfolio.max_total_risk else 1.0
        if risk_ratio >= _MAX_RISK_UTILISATION_FOR_SCALE_IN:
            return False, f"portfolio_risk_cap: utilisation={risk_ratio:.1%} ≥ {_MAX_RISK_UTILISATION_FOR_SCALE_IN:.1%}"

        return True, ""


class _ReverseAndReopenSpec(Specification["_PolicyCtx"]):
    def is_satisfied_by(self, ctx: "_PolicyCtx") -> Tuple[bool, str]:
        if ctx.decision.action != PositionActionType.REVERSE_AND_REOPEN:
            return True, ""
        if ctx.decision.confidence < _MIN_CONFIDENCE_REVERSE:
            return False, (
                f"reverse_confidence: {ctx.decision.confidence:.2f} < {_MIN_CONFIDENCE_REVERSE}"
            )
        return True, ""


class _NoPortfolioRiskBreachSpec(Specification["_PolicyCtx"]):
    def is_satisfied_by(self, ctx: "_PolicyCtx") -> Tuple[bool, str]:
        action = ctx.decision.action
        expands_risk = action in (
            PositionActionType.SCALE_IN,
            PositionActionType.LOOSEN_SL,
        )
        if not expands_risk:
            return True, ""
        portfolio = ctx.inp.portfolio
        if portfolio.current_total_risk >= portfolio.max_total_risk:
            return False, (
                f"portfolio_risk_breach: current={portfolio.current_total_risk:.0f} "
                f">= cap={portfolio.max_total_risk:.0f}"
            )
        return True, ""


class _ExitAlwaysAllowedSpec(Specification["_PolicyCtx"]):
    """FULL_EXIT is always approved — capital protection takes priority over all rules."""
    def is_satisfied_by(self, ctx: "_PolicyCtx") -> Tuple[bool, str]:
        return True, ""   # will short-circuit before other specs if action==FULL_EXIT


# ── PolicyGuard ───────────────────────────────────────────────────────────

class PolicyGuard:
    """
    Validates a PositionManagementDecision against hard invariants.

    Returns a PolicyDecision with:
      - approved=True  → execute as-is (or with minor parameter adjustments)
      - approved=False → use fallback action (HOLD_AND_MONITOR or KEEP_OPEN)
    """

    def __init__(self):
        self._specs = [
            _StaleDataSpec(),
            _NoSLRemovalSpec(),
            _NoSLWideningSpec(),
            _ScaleInSafetySpec(),
            _ReverseAndReopenSpec(),
            _NoPortfolioRiskBreachSpec(),
        ]

    def validate(
        self,
        inp: PositionManagementInput,
        decision: PositionManagementDecision,
    ) -> PolicyDecision:
        action = decision.action
        original_action = action

        # FULL_EXIT is always allowed — skip all other checks
        if action == PositionActionType.FULL_EXIT:
            return PolicyDecision(
                approved=True,
                action=action,
                original_action=original_action,
            )

        ctx = _PolicyCtx(inp=inp, decision=decision)
        for spec in self._specs:
            ok, reason = spec.is_satisfied_by(ctx)
            if not ok:
                logger.warning(
                    "[PolicyGuard] REJECTED %s for %s — %s",
                    action, inp.position.symbol, reason,
                )
                fallback = decision.fallback or PositionActionType.HOLD_AND_MONITOR
                return PolicyDecision(
                    approved=False,
                    action=fallback,
                    original_action=original_action,
                    rejection_reason=reason,
                    hard_rule_triggered=spec.__class__.__name__,
                    modified_params=ExecutionParams(type="keep"),
                )

        # Passed all rules — also apply minor parameter safety adjustments
        safe_params = self._clamp_params(inp, decision)
        return PolicyDecision(
            approved=True,
            action=action,
            original_action=original_action,
            modified_params=safe_params if safe_params != decision.execution else None,
        )

    def _clamp_params(
        self,
        inp: PositionManagementInput,
        decision: PositionManagementDecision,
    ) -> ExecutionParams:
        """Apply parameter safety bounds post-approval."""
        exec_p = decision.execution
        pos = inp.position
        params = exec_p.model_copy()

        # Clamp exit_qty_pct to [0.01, 1.0]
        if params.exit_qty_pct is not None:
            params = params.model_copy(
                update={"exit_qty_pct": max(0.01, min(1.0, params.exit_qty_pct))}
            )

        # For long: new SL must be below LTP; for short: above LTP
        if params.new_stop_loss is not None:
            ltp = pos.ltp
            if pos.side == "LONG" and params.new_stop_loss >= ltp:
                # Would trigger immediately — snap to just below LTP
                params = params.model_copy(update={"new_stop_loss": ltp * 0.999})
                logger.warning(
                    "[PolicyGuard] Clamped new_stop_loss to %.4f for LONG %s",
                    params.new_stop_loss, pos.symbol,
                )
            elif pos.side == "SHORT" and params.new_stop_loss <= ltp:
                params = params.model_copy(update={"new_stop_loss": ltp * 1.001})
                logger.warning(
                    "[PolicyGuard] Clamped new_stop_loss to %.4f for SHORT %s",
                    params.new_stop_loss, pos.symbol,
                )

            # Never move SL further from entry than current for TIGHTEN/TRAIL
            if decision.action in (
                PositionActionType.TIGHTEN_SL,
                PositionActionType.TRAIL_SL,
                PositionActionType.MOVE_SL_TO_BREAKEVEN,
            ):
                current_sl = pos.stop_loss
                entry = pos.entry_price
                current_dist = abs(entry - current_sl)
                new_dist = abs(entry - params.new_stop_loss)
                if new_dist > current_dist:
                    params = params.model_copy(update={"new_stop_loss": current_sl})
                    logger.warning(
                        "[PolicyGuard] Reverted SL widening for %s action on %s",
                        decision.action, pos.symbol,
                    )

        return params
