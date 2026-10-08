"""
Fail-closed truncation gate in DecisionValidator (LLM_STRICT_TRUNCATION).

A repaired (truncated) payload means the model ran out of tokens mid-object:
every surviving field came out of a salvage heuristic, not from the model.
Actionable decisions built on salvaged output are rejected fail-closed;
NO_TRADE is the safe direction and still passes. Complete JSON — even inside
markdown fences — is not affected.
"""
import pytest

from crypto_trader.ai.schemas import MarketStatePayload
from crypto_trader.ai.validators.decision_schema import DecisionValidator


def _state(price=100.0):
    return MarketStatePayload(
        symbol="SOLUSDT",
        timeframe="15m",
        mode="intraday",
        price=price,
        htf_trend="bullish",
        market_structure="BOS_UP",
        volatility_regime="expanding",
        funding_rate=0.0001,
        open_interest_change=5.0,
        volume_anomaly=False,
        liquidity_sweep=False,
        risk_budget_pct=0.02,
    )


def _complete_long(reason="Structure break"):
    return (
        '{"action":"LONG","confidence":0.82,"setup_type":"Sweep Reversal",'
        '"entry_zone":{"low":99.5,"high":100.2},"stop_loss":99.0,'
        '"targets":[105.0],"risk_reward":2.5,"invalidation":"' + reason + '",'
        '"warnings":[],"reason_codes":["OB_SUPPORT"]}'
    )


# truncated mid-string inside the LAST required field — repair closes the
# string and object, all fields survive, numbers intact. Pre-gate this passed
# and traded; now it must be rejected fail-closed.
_TRUNCATED_LONG = (
    '{"action":"LONG","confidence":0.82,"setup_type":"Sweep Reversal",'
    '"entry_zone":{"low":99.5,"high":100.2},"stop_loss":99.0,'
    '"targets":[105.0],"risk_reward":2.5,"invalidation":"Structure br'
)


def test_complete_long_passes_unchanged():
    decision, err = DecisionValidator.validate(_complete_long(), _state())
    assert err is None
    assert decision.action == "LONG"


def test_fenced_complete_long_is_not_repair():
    """Code fences are formatting, not truncation — must still pass."""
    wrapped = "```json\n" + _complete_long() + "\n```"
    decision, err = DecisionValidator.validate(wrapped, _state())
    assert err is None
    assert decision.action == "LONG"


def test_truncated_long_rejected_fail_closed():
    decision, err = DecisionValidator.validate(_TRUNCATED_LONG, _state())
    assert decision.action == "NO_TRADE"
    assert "repair" in err
    assert decision.reason_codes == ["GATED_BY_SYSTEM"]


def test_truncated_no_trade_still_passes():
    """NO_TRADE is the conservative direction — salvage is acceptable there."""
    truncated_no_trade = (
        '{"action":"NO_TRADE","confidence":0.3,"setup_type":"Chop",'
        '"entry_zone":{"low":0.0,"high":0.0},"stop_loss":0.0,'
        '"targets":[],"risk_reward":0.0,"invalidation":"4H cho'
    )
    decision, err = DecisionValidator.validate(truncated_no_trade, _state())
    assert err is None
    assert decision.action == "NO_TRADE"


def test_strict_gate_disabled_restores_salvage(monkeypatch):
    monkeypatch.setenv("LLM_STRICT_TRUNCATION", "false")
    decision, err = DecisionValidator.validate(_TRUNCATED_LONG, _state())
    assert err is None
    assert decision.action == "LONG"


def test_strict_gate_accepts_0_and_no_literal(monkeypatch):
    monkeypatch.setenv("LLM_STRICT_TRUNCATION", "0")
    decision, _ = DecisionValidator.validate(_TRUNCATED_LONG, _state())
    assert decision.action == "LONG"


def test_unrepairable_garbage_still_falls_back():
    decision, err = DecisionValidator.validate("garbage-not-json-at-all", _state())
    assert decision.action == "NO_TRADE"
    assert "JSONDecodeError" in err


def test_repair_missing_required_field_still_fails_schema():
    """Truncation that DROPS required fields fails pydantic regardless of gate."""
    dropped = (
        '{"action":"LONG","confidence":0.82,"setup_type":"Sweep Reversal",'
        '"entry_zone":{"low":99.5,"high":100.2},'
    )
    decision, err = DecisionValidator.validate(dropped, _state())
    assert decision.action == "NO_TRADE"
    assert "ValidationError" in err
