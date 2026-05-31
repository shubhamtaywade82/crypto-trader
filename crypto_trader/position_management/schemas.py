"""
crypto_trader.position_management.schemas
=========================================
Pydantic v2 contracts for the 3-layer AI Position Management System.

Layer 1 (State)    → PositionSnapshot, MarketContext, WalletContext, PortfolioContext
Layer 2 (Decision) → PositionManagementInput, PositionManagementDecision
Layer 3 (Execution) → PolicyDecision, ExecutionParams
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class PositionActionType(str, Enum):
    KEEP_OPEN            = "KEEP_OPEN"
    TIGHTEN_SL           = "TIGHTEN_SL"
    LOOSEN_SL            = "LOOSEN_SL"            # only when strategy explicitly permits
    MOVE_SL_TO_BREAKEVEN = "MOVE_SL_TO_BREAKEVEN"
    TRAIL_SL             = "TRAIL_SL"
    TAKE_PARTIAL_PROFIT  = "TAKE_PARTIAL_PROFIT"
    FULL_EXIT            = "FULL_EXIT"
    SCALE_IN             = "SCALE_IN"
    SCALE_OUT            = "SCALE_OUT"
    REVERSE_AND_REOPEN   = "REVERSE_AND_REOPEN"
    HOLD_AND_MONITOR     = "HOLD_AND_MONITOR"
    PAUSE_MANAGEMENT     = "PAUSE_MANAGEMENT"      # stale feed or invalid state


# ── Layer 1: State objects ──────────────────────────────────────────────────

class PositionSnapshot(BaseModel):
    """Canonical per-position state — output of PositionStateEngine."""

    position_id: str
    symbol: str
    side: str                        # "LONG" | "SHORT"
    qty: float
    entry_price: float
    ltp: float                       # last traded / mark price
    unrealized_pnl: float
    unrealized_pnl_pct: float        # as fraction of entry notional
    stop_loss: float
    take_profit: Optional[float] = None
    tp_levels: List[Dict] = Field(default_factory=list)
    trailing_active: bool = False
    trailing_stop_price: Optional[float] = None
    open_time_ms: int = 0
    age_hours: float = 0.0
    leverage: int = 1
    margin_used: float = 0.0
    notional: float = 0.0
    r_multiple: float = 0.0          # (ltp-entry)/(entry-sl) for longs; sign-adjusted
    playbook: str = "INTRADAY"
    mode: str = "paper"

    # Market context — populated by MarketContextEngine
    market_regime: str = "UNKNOWN"
    volatility_state: str = "UNKNOWN"
    liquidity_state: str = "UNKNOWN"
    trend_direction: str = "UNKNOWN"
    market_structure: str = "UNKNOWN"
    atr_pct: float = 0.0
    vwap_distance_pct: float = 0.0
    momentum_state: str = "NEUTRAL"
    event_risk: str = "none"
    near_support: bool = False
    near_resistance: bool = False
    spread_pct: float = 0.0
    funding_rate: float = 0.0
    oi_change_pct: float = 0.0

    # Wallet context
    wallet_balance: float = 0.0
    blocked_capital: float = 0.0
    free_capital: float = 0.0
    margin_used_portfolio: float = 0.0

    # Portfolio context
    total_open_positions: int = 0
    correlation_risk: str = "low"
    total_portfolio_risk: float = 0.0
    max_portfolio_risk: float = 0.0
    risk_budget_remaining: float = 0.0
    risk_per_trade: float = 0.0

    # Data freshness
    data_age_ms: int = 0
    is_stale: bool = False


class MarketContext(BaseModel):
    """Per-symbol market environment — sub-object of PositionSnapshot."""

    symbol: str
    trend: str = "neutral"           # "up" | "down" | "neutral"
    regime: str = "UNKNOWN"          # ExtendedRegime value
    atr: float = 0.0
    atr_pct: float = 0.0
    vwap: Optional[float] = None
    vwap_distance_pct: float = 0.0
    structure: str = "UNKNOWN"       # "BOS_UP", "BOS_DOWN", "CHOCH_UP", "CHOCH_DOWN", "RANGE"
    liquidity: str = "UNKNOWN"       # "strong" | "moderate" | "thin"
    momentum: str = "NEUTRAL"        # "STRONG_UP" | "WEAK_UP" | "NEUTRAL" | "WEAK_DOWN" | "STRONG_DOWN"
    volatility_state: str = "UNKNOWN"  # "expanding" | "compressing" | "normal"
    event_risk: str = "none"         # "none" | "low" | "high" (expiry, macro, news)
    spread_pct: float = 0.0
    funding_rate: float = 0.0
    oi_change_pct: float = 0.0
    near_support: bool = False
    near_resistance: bool = False
    data_age_ms: int = 0
    is_stale: bool = False           # True if feed is disconnected or data > 60s old


class WalletContext(BaseModel):
    """Real-time wallet / margin state."""

    equity: float = 0.0
    free_capital: float = 0.0
    blocked_capital: float = 0.0
    used_margin: float = 0.0
    unrealized_pnl_total: float = 0.0
    margin_ratio: float = 0.0        # used_margin / equity; > 0.8 is danger zone


class PortfolioContext(BaseModel):
    """Portfolio-wide risk aggregation — prevents managing positions in isolation."""

    open_positions: int = 0
    correlation_risk: str = "low"    # "low" | "moderate" | "high"
    max_total_risk: float = 0.0
    current_total_risk: float = 0.0
    concentration_pct: float = 0.0   # this position's notional / total portfolio notional
    net_delta_exposure: float = 0.0  # signed sum of all position notionals


class PositionConstraints(BaseModel):
    """Per-strategy / per-playbook management rules."""

    allow_scale_in: bool = False
    allow_partial_exit: bool = True
    allow_breakeven_trail: bool = True
    allow_average_down: bool = False     # NEVER True by default
    allow_loosen_sl: bool = False        # NEVER True by default
    min_r_for_breakeven: float = 1.0
    min_r_for_partial_exit: float = 2.0
    trail_atr_multiplier: float = 1.5
    max_scale_in_count: int = 1
    stale_data_max_age_ms: int = 60_000  # 60s — pause management if exceeded


# ── Layer 2: Decision objects ───────────────────────────────────────────────

class PositionManagementInput(BaseModel):
    """Complete structured input sent to the AI assessor."""

    position: PositionSnapshot
    market: MarketContext
    wallet: WalletContext
    portfolio: PortfolioContext
    constraints: PositionConstraints


class RiskEffect(BaseModel):
    """How the recommended action changes portfolio risk."""

    delta_risk: float = 0.0          # negative = risk reduction
    capital_freed: float = 0.0
    new_margin_used: float = 0.0
    new_sl: Optional[float] = None
    new_tp: Optional[float] = None


class ExecutionParams(BaseModel):
    """Concrete instructions for the execution layer."""

    type: str                            # "keep" | "modify_stop" | "partial_exit" | "full_exit" | "scale_in"
    new_stop_loss: Optional[float] = None
    new_take_profit: Optional[float] = None
    exit_qty_pct: Optional[float] = None   # 0.0–1.0 fraction to exit
    scale_qty_pct: Optional[float] = None  # fraction of current qty to add
    trail_distance_pct: Optional[float] = None


class PositionManagementDecision(BaseModel):
    """Structured output from the AI assessor — Layer 2 output."""

    action: PositionActionType
    confidence: float = Field(..., ge=0.0, le=1.0)
    reason: str
    risk_effect: RiskEffect = Field(default_factory=RiskEffect)
    execution: ExecutionParams = Field(default_factory=lambda: ExecutionParams(type="keep"))
    fallback: PositionActionType = PositionActionType.HOLD_AND_MONITOR
    score: float = 0.0                           # ActionScorer's deterministic score (0–100)
    scorer_top_factors: List[str] = Field(default_factory=list)
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))


# ── Layer 3: Policy / execution objects ────────────────────────────────────

class PolicyDecision(BaseModel):
    """Output of PolicyGuard — approved or overridden recommendation."""

    approved: bool
    action: PositionActionType
    original_action: PositionActionType
    rejection_reason: Optional[str] = None
    modified_params: Optional[ExecutionParams] = None
    hard_rule_triggered: Optional[str] = None
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))


class AuditRecord(BaseModel):
    """Full audit trail entry written after every assessment cycle."""

    position_id: str
    symbol: str
    cycle_id: str                            # uuid for this evaluation run
    trigger: str                             # "tick" | "candle" | "fill" | "shock" | "scheduled"
    input: PositionManagementInput
    ai_decision: PositionManagementDecision
    policy_decision: PolicyDecision
    execution_outcome: Optional[str] = None  # "ok" | "error: <msg>" | "skipped"
    latency_ms: int = 0
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
