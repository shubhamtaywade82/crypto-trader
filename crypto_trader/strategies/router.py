"""
StrategyRouter — regime-aware best-of playbook selection.

Responsibility
--------------
Given a regime context and a dict of named playbook evaluators (callables that
return a setup dict or None), the router:

  1. Looks up which playbooks are allowed for the current extended regime.
  2. Evaluates each allowed playbook in order.
  3. LLM-fuses each candidate's technical score.
  4. Ranks candidates by bias-adjusted score.
  5. Returns the highest-scoring candidate that clears the dynamic threshold,
     or None if none qualifies.

The router is intentionally stateless: every input arrives as an argument so
it can be unit-tested in isolation without an engine instance.

Usage::

    router = StrategyRouter()

    result = router.select(
        regime_ctx=regime_ctx,
        playbook_evaluators={
            "playbook_a":    lambda: playbook_a.evaluate(df_1h, regime),
            "playbook_ares": lambda: playbook_ares.evaluate(df_15m, adx_1h=adx),
        },
        advisor=advisor,
        dynamic_threshold=adaptive_threshold.get_threshold(),
        regime=regime,
        regime_score=regime_score,
    )

    if result:
        execute_entry(result.setup, result.final_score, ...)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("crypto_trader.strategies.router")

# Kept outside the class so it can be imported for display / tests without
# instantiating an engine.
PLAYBOOK_LABELS: Dict[str, str] = {
    "playbook_ares":  "PLAYBOOK ARES",
    "playbook_volx":  "PLAYBOOK VOLX",
    "playbook_a":     "PLAYBOOK A",
    "playbook_b":     "PLAYBOOK B",
    "playbook_sweep": "PLAYBOOK SWEEP",
}


@dataclass
class RouterResult:
    """All context needed downstream to execute or log the winning entry."""
    setup: dict
    final_score: float
    tech_score: float
    llm_weight: float
    explanation: str
    playbook_key: str
    advice: Any = None
    dynamic_threshold: float = 0.75


class StrategyRouter:
    """
    Regime-aware best-of playbook selector.

    The regime → allowed-playbooks mapping lives here; adding a new playbook
    only requires updating ``REGIME_STRATEGY_MAP`` and passing the evaluator
    lambda from the engine.
    """

    # Regime → priority-ordered list of allowed playbook keys.
    # Empty list = no entries allowed in this regime.
    REGIME_STRATEGY_MAP: Dict[Any, List[str]] = {}  # populated in __init_subclass__ or at runtime

    # Default fallback when regime is unknown
    _FALLBACK_ALLOWED: List[str] = ["playbook_ares", "playbook_a", "playbook_b"]

    def __init__(self, regime_strategy_map: Optional[Dict[Any, List[str]]] = None) -> None:
        """
        Parameters
        ----------
        regime_strategy_map:
            Override the default REGIME_STRATEGY_MAP. If None, the class-level
            dict is used. Pass a custom map to support different trading profiles.
        """
        self._map = regime_strategy_map if regime_strategy_map is not None else self.REGIME_STRATEGY_MAP

    # ── Core API ──────────────────────────────────────────────────────────────

    def select(
        self,
        regime_ctx,
        playbook_evaluators: Dict[str, Callable[[], Optional[dict]]],
        advisor,
        dynamic_threshold: float,
        regime,
        regime_score: float,
        fallback_allowed: Optional[List[str]] = None,
    ) -> Optional[RouterResult]:
        """
        Evaluate all allowed playbooks, fuse scores, and return the best.

        Returns ``None`` if no candidate qualifies (no setups generated, or all
        below the dynamic threshold).

        Parameters
        ----------
        regime_ctx:
            Current ``RegimeContext`` (or None for legacy path).
        playbook_evaluators:
            Mapping of playbook-key → zero-argument callable returning a setup
            dict (with at least ``score`` and ``side`` keys) or None.
        advisor:
            LLM advisor with ``get_last_advice()`` and ``compute_final_score()``.
        dynamic_threshold:
            Minimum ``final_score`` required for a candidate to be selected.
        regime:
            Current ``MarketRegime`` enum (used for LLM fusion).
        regime_score:
            Numeric regime confidence score (used for LLM fusion).
        fallback_allowed:
            Override the fallback playbook list when the regime is unknown.
        """
        allowed = self._allowed_playbooks(regime_ctx, fallback_allowed)
        if not allowed:
            regime_name = regime_ctx.extended.value if regime_ctx else "unknown"
            logger.info("[ROUTER] no strategies allowed for regime=%s", regime_name)
            return None

        logger.info("[ROUTER] allowed=%s", allowed)

        advice = advisor.get_last_advice() if advisor else None
        candidates = self._build_candidates(
            allowed, playbook_evaluators, advisor, advice, regime, regime_score
        )

        if not candidates:
            return None

        candidates.sort(key=lambda c: c[0], reverse=True)
        rank_score, final_score, llm_weight, explanation, pb_key, setup, tech_score = candidates[0]

        logger.info(
            "[SELECT] winner=%s %s | final=%.2f | %d candidate(s)",
            PLAYBOOK_LABELS.get(pb_key, pb_key),
            setup["side"].value if hasattr(setup["side"], "value") else setup["side"],
            final_score,
            len(candidates),
        )

        if final_score < dynamic_threshold:
            logger.info(
                "[BLOCKED] final_score=%.2f < threshold=%.2f", final_score, dynamic_threshold
            )
            return None

        return RouterResult(
            setup=setup,
            final_score=final_score,
            tech_score=tech_score,
            llm_weight=llm_weight,
            explanation=explanation,
            playbook_key=pb_key,
            advice=advice,
            dynamic_threshold=dynamic_threshold,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _allowed_playbooks(
        self, regime_ctx, fallback: Optional[List[str]]
    ) -> List[str]:
        fb = fallback if fallback is not None else self._FALLBACK_ALLOWED
        if regime_ctx is None:
            return fb
        return self._map.get(regime_ctx.extended, fb)

    def _build_candidates(
        self, allowed, evaluators, advisor, advice, regime, regime_score
    ):
        candidates = []
        for pb_key in allowed:
            evaluator = evaluators.get(pb_key)
            if evaluator is None:
                continue
            try:
                setup = evaluator()
            except Exception as exc:
                logger.warning(
                    "[%s] evaluate failed: %s",
                    PLAYBOOK_LABELS.get(pb_key, pb_key), exc,
                )
                continue
            if not setup:
                continue

            tech_score = setup["score"]
            if advisor and advice:
                final_score, llm_weight, explanation = advisor.compute_final_score(
                    technical_score=tech_score,
                    regime=regime.value if hasattr(regime, "value") else str(regime),
                    regime_score=regime_score,
                    advice=advice,
                )
            else:
                final_score, llm_weight, explanation = tech_score, 0.0, "LLM unavailable"

            rank_score = final_score * self._bias_factor(setup["side"], advice)
            logger.info(
                "[%s] %s | tech=%.2f final=%.2f rank=%.2f",
                PLAYBOOK_LABELS.get(pb_key, pb_key),
                setup["side"].value if hasattr(setup["side"], "value") else setup["side"],
                tech_score, final_score, rank_score,
            )
            candidates.append(
                (rank_score, final_score, llm_weight, explanation, pb_key, setup, tech_score)
            )
        return candidates

    @staticmethod
    def _bias_factor(side, advice) -> float:
        """Ranking tiebreak: penalise candidates whose side contradicts the LLM bias.

        Never applied to the threshold gate — only to ranking among candidates.
        Returns a multiplier in [0.5, 1.0].
        """
        if advice is None:
            return 1.0
        from ..wallet import PositionSide  # local import to avoid circular at module load
        rec = getattr(advice, "recommended_bias", "any")
        if rec == "none":
            return 0.5
        if rec == "long_only" and side == PositionSide.SHORT:
            return 0.6
        if rec == "short_only" and side == PositionSide.LONG:
            return 0.6
        return 1.0


def build_legacy_router() -> StrategyRouter:
    """Return the default regime → playbook map wired to the legacy multi-playbook path."""
    from ..regime import ExtendedRegime
    regime_map: Dict[Any, List[str]] = {
        ExtendedRegime.TREND_EXPANSION:      ["playbook_a", "playbook_b"],
        ExtendedRegime.BREAKOUT_ENVIRONMENT: ["playbook_b", "playbook_a", "playbook_sweep"],
        ExtendedRegime.ACCUMULATION:         ["playbook_a", "playbook_sweep"],
        ExtendedRegime.MEAN_REVERSION:       ["playbook_ares", "playbook_volx", "playbook_sweep"],
        ExtendedRegime.LOW_VOL_CHOP:         ["playbook_ares", "playbook_volx"],
        ExtendedRegime.LIQUIDATION_EVENT:    [],
        ExtendedRegime.DEAD_MARKET:          [],
        ExtendedRegime.HIGH_RISK_CHAOS:      [],
    }
    return StrategyRouter(regime_map)
