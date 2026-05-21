from __future__ import annotations

def evaluate_setup(regime: str, score: float):
    """Return a trade setup dict only for high-conviction trending regimes.

    CHOP / RANGING markets are skipped entirely — no directional edge exists.
    """
    if regime == "TRENDING_UP" and score >= 0.60:
        return {"action": "LONG", "score": score}
    if regime == "TRENDING_DOWN" and score >= 0.60:
        return {"action": "SHORT", "score": score}
    return None
