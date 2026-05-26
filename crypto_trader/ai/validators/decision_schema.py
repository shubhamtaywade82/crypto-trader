"""crypto_trader.ai.validators.decision_schema — Output validator and post-LLM rule checks."""

import json
from typing import Tuple, Optional

from crypto_trader.ai.schemas import MarketStatePayload, LLMDecision, EntryZone


class DecisionValidator:
    @staticmethod
    def validate(raw_json: str, state: MarketStatePayload) -> Tuple[LLMDecision, Optional[str]]:
        # 1. Parse JSON
        try:
            clean_text = raw_json.strip()
            if "```json" in clean_text:
                clean_text = clean_text.split("```json")[1].split("```")[0].strip()
            parsed = json.loads(clean_text)
        except Exception as e:
            return DecisionValidator.fallback_no_trade(), f"JSONDecodeError: {e}"

        # 2. Pydantic schema validation
        try:
            decision = LLMDecision(**parsed)
        except Exception as e:
            return DecisionValidator.fallback_no_trade(), f"ValidationError: {e}"

        # 3. Hard Safety Gate Boundaries
        if decision.action != "NO_TRADE":
            # Rule A: Invalidation Stop Loss price direction sanity
            if decision.action == "LONG" and decision.stop_loss >= state.price:
                return DecisionValidator.fallback_no_trade(), "RiskGate: LONG stop_loss must be below current price"
            if decision.action == "SHORT" and decision.stop_loss <= state.price:
                return DecisionValidator.fallback_no_trade(), "RiskGate: SHORT stop_loss must be above current price"

            # Rule B: Risk-reward safety cutoff
            if decision.risk_reward < 1.5:
                return DecisionValidator.fallback_no_trade(), f"RiskGate: Risk Reward ratio {decision.risk_reward} below minimum 1.5"

            # Rule C: Conflicting structures
            if decision.action == "LONG" and state.htf_trend == "bearish" and state.mode == "swing":
                return DecisionValidator.fallback_no_trade(), "RiskGate: LONG swing entry blocked on bearish HTF trend"

            # Rule D: Stale target structures
            if len(decision.targets) == 0:
                return DecisionValidator.fallback_no_trade(), "RiskGate: Missing profit targets"

        return decision, None

    @staticmethod
    def fallback_no_trade() -> LLMDecision:
        return LLMDecision(
            action="NO_TRADE",
            confidence=0.0,
            setup_type="FALLBACK_SAFETY",
            entry_zone=EntryZone(low=0.0, high=0.0),
            stop_loss=0.0,
            targets=[],
            risk_reward=0.0,
            invalidation="Failed validation gate checks",
            warnings=["Risk gate fallback activated"],
            reason_codes=["GATED_BY_SYSTEM"],
        )
