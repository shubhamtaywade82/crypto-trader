"""crypto_trader.ai.schemas — Pydantic v2 market state and decision contracts."""

from typing import Dict, List, Optional
from pydantic import BaseModel, Field


# ──────── Market State Input Contract ────────

class MarketStatePayload(BaseModel):
    symbol: str = Field(..., description="Trading pair (e.g. SOLUSDT)")
    timeframe: str = Field(..., description="Interval (e.g. 15m)")
    mode: str = Field(..., description="Trading mode: 'intraday' or 'swing'")
    price: float = Field(..., description="Current mark price")
    htf_trend: str = Field(..., description="High-timeframe trend (e.g. bullish, bearish, chop)")
    market_structure: str = Field(..., description="SMC Structure (e.g. BOS_UP, CHOCH_UP, RANGE)")
    volatility_regime: str = Field(..., description="Regime classification (e.g. expanding, chop, compressed)")
    funding_rate: float = Field(..., description="Current funding rate percentage")
    open_interest_change: float = Field(..., description="24h open interest change percentage")
    volume_anomaly: bool = Field(..., description="True if volume > 2x standard deviation")
    liquidity_sweep: bool = Field(..., description="True if recent swing high/low swept")
    risk_budget_pct: float = Field(..., description="Available risk allocation slice (0.0 to 1.0)")


# ──────── LLM Decision Output Contract ────────

class EntryZone(BaseModel):
    low: float = Field(..., description="Lower bound of entry region")
    high: float = Field(..., description="Upper bound of entry region")


class LLMDecision(BaseModel):
    action: str = Field(..., description="Trade action: LONG, SHORT, or NO_TRADE")
    confidence: float = Field(..., description="Score from 0.0 (no confidence) to 1.0 (absolute)")
    setup_type: str = Field(..., description="Classification (e.g. Sweep Reversal, BOS Continuation)")
    entry_zone: EntryZone = Field(..., description="Calculated range of entry target prices")
    stop_loss: float = Field(..., description="Strict stop loss invalidation price")
    targets: List[float] = Field(..., description="Target profit scale-out prices")
    risk_reward: float = Field(..., description="Risk-reward ratio (e.g. 2.5)")
    invalidation: str = Field(..., description="Qualitative reasoning for SL placement")
    warnings: List[str] = Field(default_factory=list, description="Advisory warnings or traps detected")
    reason_codes: List[str] = Field(default_factory=list, description="Diagnostic tags (e.g. OB_RESISTANCE, HTF_CHOP)")
