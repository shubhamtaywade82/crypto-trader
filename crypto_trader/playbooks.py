from __future__ import annotations

def evaluate_setup(regime: str, score: float):
    if regime.startswith("TRENDING") and score >= 0.60:
        return {"action": "LONG" if regime.endswith("UP") else "SHORT", "score": score}
    if regime in {"RANGING", "CHOP"} and score >= 0.55:
        return {"action": "LONG", "score": score}
    return None
