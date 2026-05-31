"""
crypto_trader.position_management.ai_assessor
==============================================
Layer 2 (AI): Sends a PositionManagementInput to Ollama via the existing
LLMRouter and parses a PositionManagementDecision.

The LLM is advisory only — the PolicyGuard (Layer 3) approves or rejects
every recommendation before execution.  If the LLM fails or produces invalid
output the scorer's deterministic recommendation is used as fallback.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

from .action_scorer import (
    compute_breakeven_stop,
    compute_trail_stop,
    score_position,
    score_to_candidate_actions,
)
from .prompts import SYSTEM_PROMPT, build_user_prompt
from .schemas import (
    ExecutionParams,
    PositionActionType,
    PositionManagementDecision,
    PositionManagementInput,
    RiskEffect,
)

logger = logging.getLogger("crypto_trader.position_management.ai_assessor")


class AIAssessor:
    """
    Wraps the existing LLMRouter to produce PositionManagementDecision objects.

    Falls back to the deterministic scorer output when:
      - the LLM provider is unavailable
      - the response fails JSON/schema validation
      - position data is stale
    """

    def __init__(
        self,
        llm_router=None,       # crypto_trader.ai.router.LLMRouter
        timeout_s: int = 15,
        min_confidence_for_expansion: float = 0.75,
    ):
        self.router = llm_router
        self.timeout_s = timeout_s
        self.min_confidence_for_expansion = min_confidence_for_expansion

    def assess(self, inp: PositionManagementInput) -> PositionManagementDecision:
        """Main entry point.  Always returns a valid PositionManagementDecision."""
        score, factors = score_position(inp)
        candidate_actions = score_to_candidate_actions(score)
        default_action = candidate_actions[0]

        # Stale data — always pause management, no AI call needed
        if inp.position.is_stale or inp.market.is_stale:
            return _pause_decision(score, factors, "stale_market_data")

        ai_decision = None
        if self.router is not None:
            ai_decision = self._call_llm(inp, score, factors)

        if ai_decision is None:
            return _deterministic_decision(inp, score, factors, default_action)

        # Augment with scorer data
        ai_decision = ai_decision.model_copy(
            update={"score": score, "scorer_top_factors": factors}
        )

        # If AI confidence is low for expansion actions, downgrade to hold
        expanding = ai_decision.action in (
            PositionActionType.SCALE_IN,
            PositionActionType.LOOSEN_SL,
            PositionActionType.REVERSE_AND_REOPEN,
        )
        if expanding and ai_decision.confidence < self.min_confidence_for_expansion:
            logger.info(
                "[AIAssessor] Downgrading %s (conf=%.2f < %.2f) → HOLD_AND_MONITOR",
                ai_decision.action, ai_decision.confidence, self.min_confidence_for_expansion,
            )
            return ai_decision.model_copy(
                update={
                    "action": PositionActionType.HOLD_AND_MONITOR,
                    "execution": ExecutionParams(type="keep"),
                    "reason": f"Confidence {ai_decision.confidence:.2f} insufficient for expansion; holding.",
                }
            )

        return ai_decision

    # ── Private ──────────────────────────────────────────────────────────

    def _call_llm(
        self, inp: PositionManagementInput, score: float, factors: list
    ) -> Optional[PositionManagementDecision]:
        user_prompt = build_user_prompt(inp, score, factors)
        try:
            t0 = time.time()
            # The router accepts MarketStatePayload, but we pass raw prompts
            # via the lower-level provider interface to avoid coupling to entry schemas.
            raw = self._raw_chat(user_prompt)
            latency = time.time() - t0
            logger.debug("[AIAssessor] LLM responded in %.1fs", latency)
            if not raw:
                return None
            return _parse_response(raw, inp)
        except Exception as exc:
            logger.warning("[AIAssessor] LLM call failed: %s", exc)
            return None

    def _raw_chat(self, user_prompt: str) -> Optional[str]:
        """Dispatch to router's local provider directly with position prompts."""
        if self.router is None:
            return None
        # Use local provider first (fast, cheap).  Escalate to cloud if local fails.
        for provider_attr in ("local", "cloud"):
            provider = getattr(self.router, provider_attr, None)
            if provider is None:
                continue
            try:
                if not provider.health():
                    continue
                return provider.chat(SYSTEM_PROMPT, user_prompt, self.timeout_s)
            except Exception as exc:
                logger.debug("[AIAssessor] provider=%s failed: %s", provider_attr, exc)
        return None


# ── Parsing ───────────────────────────────────────────────────────────────

def _parse_response(
    raw: str, inp: PositionManagementInput
) -> Optional[PositionManagementDecision]:
    """Extract and validate JSON from LLM response."""
    text = raw.strip()
    # Strip markdown code fences if present
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:]
            if part.startswith("{"):
                text = part
                break

    # Find first JSON object
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        logger.warning("[AIAssessor] No JSON object found in response")
        return None

    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError as exc:
        logger.warning("[AIAssessor] JSON parse error: %s", exc)
        return None

    return _dict_to_decision(data, inp)


def _dict_to_decision(
    data: dict, inp: PositionManagementInput
) -> Optional[PositionManagementDecision]:
    try:
        action_str = data.get("action", "HOLD_AND_MONITOR")
        action = PositionActionType(action_str)
    except ValueError:
        logger.warning("[AIAssessor] Unknown action: %s", data.get("action"))
        action = PositionActionType.HOLD_AND_MONITOR

    confidence = float(data.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))
    reason = str(data.get("reason", ""))[:500]

    risk_raw = data.get("risk_effect", {}) or {}
    risk_effect = RiskEffect(
        delta_risk=float(risk_raw.get("delta_risk", 0.0)),
        capital_freed=float(risk_raw.get("capital_freed", 0.0)),
    )

    exec_raw = data.get("execution", {}) or {}
    exec_type = str(exec_raw.get("type", "keep"))

    def _f(key):
        v = exec_raw.get(key)
        return float(v) if v is not None else None

    execution = ExecutionParams(
        type=exec_type,
        new_stop_loss=_f("new_stop_loss"),
        new_take_profit=_f("new_take_profit"),
        exit_qty_pct=_f("exit_qty_pct"),
        scale_qty_pct=_f("scale_qty_pct"),
        trail_distance_pct=_f("trail_distance_pct"),
    )

    # If action is TRAIL_SL but no new_stop_loss provided, compute it
    if action == PositionActionType.TRAIL_SL and execution.new_stop_loss is None:
        execution = execution.model_copy(
            update={"new_stop_loss": compute_trail_stop(inp, inp.constraints.trail_atr_multiplier)}
        )

    # If action is MOVE_SL_TO_BREAKEVEN but no new_stop_loss provided, compute it
    if action == PositionActionType.MOVE_SL_TO_BREAKEVEN and execution.new_stop_loss is None:
        execution = execution.model_copy(
            update={"new_stop_loss": compute_breakeven_stop(inp)}
        )

    fallback_str = data.get("fallback", "HOLD_AND_MONITOR")
    try:
        fallback = PositionActionType(fallback_str)
    except ValueError:
        fallback = PositionActionType.HOLD_AND_MONITOR

    return PositionManagementDecision(
        action=action,
        confidence=confidence,
        reason=reason,
        risk_effect=risk_effect,
        execution=execution,
        fallback=fallback,
    )


# ── Fallback decision builders ────────────────────────────────────────────

def _pause_decision(score: float, factors: list, reason: str) -> PositionManagementDecision:
    return PositionManagementDecision(
        action=PositionActionType.PAUSE_MANAGEMENT,
        confidence=1.0,
        reason=reason,
        execution=ExecutionParams(type="keep"),
        fallback=PositionActionType.PAUSE_MANAGEMENT,
        score=score,
        scorer_top_factors=factors,
    )


def _deterministic_decision(
    inp: PositionManagementInput,
    score: float,
    factors: list,
    action: PositionActionType,
) -> PositionManagementDecision:
    """Pure scorer output when LLM is unavailable."""
    execution = _action_to_execution(action, inp)
    return PositionManagementDecision(
        action=action,
        confidence=0.6,   # medium confidence for deterministic fallback
        reason=f"Deterministic scorer (score={score:.0f}): {', '.join(factors[:3])}",
        execution=execution,
        fallback=PositionActionType.HOLD_AND_MONITOR,
        score=score,
        scorer_top_factors=factors,
    )


def _action_to_execution(action: PositionActionType, inp: PositionManagementInput) -> ExecutionParams:
    if action == PositionActionType.TRAIL_SL:
        return ExecutionParams(
            type="modify_stop",
            new_stop_loss=compute_trail_stop(inp, inp.constraints.trail_atr_multiplier),
        )
    if action == PositionActionType.MOVE_SL_TO_BREAKEVEN:
        return ExecutionParams(
            type="modify_stop",
            new_stop_loss=compute_breakeven_stop(inp),
        )
    if action == PositionActionType.TAKE_PARTIAL_PROFIT:
        return ExecutionParams(type="partial_exit", exit_qty_pct=0.5)
    if action == PositionActionType.FULL_EXIT:
        return ExecutionParams(type="full_exit", exit_qty_pct=1.0)
    if action == PositionActionType.SCALE_OUT:
        return ExecutionParams(type="partial_exit", exit_qty_pct=0.3)
    return ExecutionParams(type="keep")
