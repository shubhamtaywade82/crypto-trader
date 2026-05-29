"""crypto_trader.ai.validators.decision_schema — Output validator and post-LLM rule checks."""

import json
from typing import Tuple, Optional

from crypto_trader.ai.schemas import MarketStatePayload, LLMDecision, EntryZone


class DecisionValidator:
    @staticmethod
    def validate(raw_json: str, state: MarketStatePayload) -> Tuple[LLMDecision, Optional[str]]:
        # 1. Parse JSON (with salvage for truncated / fenced output)
        try:
            parsed = DecisionValidator._parse_json(raw_json)
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
    def _parse_json(raw_json: str) -> dict:
        """Parse model JSON, tolerating markdown fences and truncated output.

        The local model can stop mid-object when it hits num_predict, leaving
        an unterminated string or unclosed brackets. We first try a clean parse,
        then fall back to extracting the outer object and repairing it.
        """
        text = raw_json.strip()

        # Strip ```json ... ``` or bare ``` fences.
        if "```" in text:
            fenced = text.split("```")
            # pick the largest fenced block, drop a leading 'json' language tag
            blocks = [b[4:].strip() if b.strip().lower().startswith("json") else b.strip()
                      for b in fenced[1::2]]
            if blocks:
                text = max(blocks, key=len)

        # Trim to the outermost JSON object.
        start = text.find("{")
        if start > 0:
            text = text[start:]

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return json.loads(DecisionValidator._repair_truncated(text))

    @staticmethod
    def _repair_truncated(text: str) -> str:
        """Best-effort recover a JSON object from truncated model output.

        Repeatedly close the dangling structure and parse; if that fails, drop
        the trailing incomplete element (dangling key / partial value) at the
        last structural boundary and retry. Bounded by the input length.
        """
        s = text
        while s:
            candidate = DecisionValidator._close(s)
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                s = DecisionValidator._drop_trailing(s)
                if s is None:
                    break
        # Let the caller surface the original decode error.
        return json.loads(text)

    @staticmethod
    def _close(text: str) -> str:
        """Close any unterminated string and unbalanced brackets in ``text``."""
        in_str = False
        escaped = False
        stack = []
        for ch in text:
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]" and stack:
                stack.pop()

        repaired = text
        if in_str:
            repaired += '"'
        repaired = repaired.rstrip().rstrip(",")
        for closer in reversed(stack):
            repaired += closer
        return repaired

    @staticmethod
    def _drop_trailing(text: str) -> Optional[str]:
        """Cut the trailing incomplete element at the last structural boundary.

        Returns the prefix up to (and including, for an opening bracket) the
        last top-level ``,`` ``{`` or ``[`` that is not inside a string, or
        ``None`` if no boundary remains.
        """
        in_str = False
        escaped = False
        boundary = None
        keep_bracket = False
        for i, ch in enumerate(text):
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == ",":
                boundary, keep_bracket = i, False
            elif ch in "{[":
                boundary, keep_bracket = i, True
        if boundary is None:
            return None
        return text[: boundary + 1] if keep_bracket else text[:boundary]

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
