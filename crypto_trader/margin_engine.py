"""
crypto_trader.margin_engine — Maintenance Margin & Liquidation Distance
========================================================================
Implements the core safety engines required for institutional-grade futures trading.
- MarginEngine: Checks margin utilization and liquidation buffers.
- LeverageEngine: Manages leverage tiers and volatility-adjusted leverage.
"""

import logging
from typing import Tuple, Optional, Dict

logger = logging.getLogger("crypto_trader.margin_engine")

class MarginEngine:
    """
    Enforces futures margin safety rules (G6).
    Responsibilities:
    - maintenance margin vs wallet balance check.
    - liquidation price distance validation.
    - isolated vs cross margin mode validation.
    """
    def __init__(
        self,
        max_margin_utilization: float = 0.25,
        min_liquidation_distance_pct: float = 0.03,
        require_isolated_margin: bool = True
    ):
        self.max_margin_utilization = max_margin_utilization
        self.min_liquidation_distance_pct = min_liquidation_distance_pct
        self.require_isolated_margin = require_isolated_margin

    def validate_margin_mode(self, is_isolated: bool) -> Tuple[bool, str]:
        """G6: Force isolated margin for safer liquidation boundaries."""
        if self.require_isolated_margin and not is_isolated:
            return False, "Cross margin is prohibited. Isolated margin required."
        return True, "OK"

    def check_margin_utilization(self, total_maintenance_margin: float, wallet_balance: float) -> Tuple[bool, str]:
        """G6: Rejects entries if margin utilization exceeds threshold."""
        if wallet_balance <= 0:
            return False, "Wallet balance is zero or negative."
        
        utilization = total_maintenance_margin / wallet_balance
        if utilization > self.max_margin_utilization:
            return False, f"Margin utilization too high: {utilization:.2%} > {self.max_margin_utilization:.2%}"
        return True, "OK"

    def check_liquidation_distance(
        self, 
        entry_price: float, 
        stop_loss_price: float, 
        liquidation_price: float, 
        side: str
    ) -> Tuple[bool, str]:
        """
        G6: Ensure SL is hit before liquidation.
        Rejects entry if SL is at/past liquidation or if liq distance is too thin.
        """
        if entry_price <= 0:
            return False, "Invalid entry price"

        side_lower = side.lower()
        if side_lower in ("long", "buy"):
            # For Long: Entry > Stop > Liq
            if stop_loss_price <= liquidation_price:
                return False, f"Stop loss {stop_loss_price:.4f} is BELOW liquidation {liquidation_price:.4f}"
            dist = (entry_price - liquidation_price) / entry_price
        elif side_lower in ("short", "sell"):
            # For Short: Liq > Stop > Entry
            if stop_loss_price >= liquidation_price:
                return False, f"Stop loss {stop_loss_price:.4f} is ABOVE liquidation {liquidation_price:.4f}"
            dist = (liquidation_price - entry_price) / entry_price
        else:
            return False, f"Unknown side: {side}"
            
        if dist < self.min_liquidation_distance_pct:
            return False, f"Liquidation distance too small: {dist:.2%} < {self.min_liquidation_distance_pct:.2%}"
        
        return True, "OK"


class LeverageEngine:
    """
    Enforces dynamic leverage rules (G7).
    Responsibilities:
    - leverage bracket tier validation.
    - effective portfolio leverage tracking.
    - volatility-adjusted leverage scaling.
    """
    def __init__(self, default_leverage: int = 2, hard_max_leverage: int = 20):
        self.default_leverage = default_leverage
        self.hard_max_leverage = hard_max_leverage

    def validate_leverage_tier(
        self, 
        requested_leverage: int, 
        position_notional: float, 
        max_notional_for_tier: float
    ) -> Tuple[bool, str]:
        """G7: Prevent exceeding exchange leverage brackets."""
        if requested_leverage > self.hard_max_leverage:
            return False, f"Requested leverage {requested_leverage}x exceeds hard max {self.hard_max_leverage}x"
            
        if position_notional > max_notional_for_tier:
            return False, f"Position notional {position_notional:.2f} exceeds tier max {max_notional_for_tier:.2f} for {requested_leverage}x"
            
        return True, "OK"

    def calculate_effective_leverage(self, total_notional: float, account_equity: float) -> float:
        """Effective leverage = total_notional / equity. Handles Decimal/float mix."""
        try:
            total_notional_f = float(total_notional)
            account_equity_f = float(account_equity)
            if account_equity_f <= 0:
                return float('inf')
            return total_notional_f / account_equity_f
        except (TypeError, ValueError):
            return 0.0

    def adjust_leverage_for_volatility(
        self, 
        base_leverage: int, 
        atr_pct: float, 
        high_vol_threshold: float = 0.05, 
        extreme_vol_threshold: float = 0.10
    ) -> int:
        """Dynamically scale down leverage as market volatility increases."""
        if atr_pct >= extreme_vol_threshold:
            logger.warning(f"[Leverage] Volatility {atr_pct:.2%} exceeds extreme threshold ({extreme_vol_threshold:.2%}). HALTING.")
            return 0  # Signal for no trading
        elif atr_pct >= high_vol_threshold:
            scaled = max(1, base_leverage // 2)
            logger.info(f"[Leverage] Scaling leverage {base_leverage}x -> {scaled}x due to high volatility ({atr_pct:.2%})")
            return scaled
        return base_leverage


class DynamicLeverageManager:
    """
    Context-aware per-symbol leverage scaler.

    Computes a dynamic leverage value for each symbol on every signal tick by
    combining account health (drawdown, margin ratio), market regime, and
    realised volatility (ATR%%).  The result is clamped to the venue's
    instrument cap and the user's hard maximum.

    Scaling rules (all configurable via TradingConfig):
      1. Volatility:  ATR%% above extreme → 0x (halt); above high → halved.
      2. Drawdown:    > severe threshold → halved; > moderate → ×0.7.
      3. Margin ratio:> high threshold → halved; > moderate → ×0.75.
      4. Regime:      TREND_EXPANSION → +1; LOW_VOL_CHOP / MEAN_REVERSION → −1.
    """

    def __init__(self, cfg=None):
        self.cfg = cfg
        # Defaults when no cfg is provided (tests / standalone usage)
        self.min_leverage = getattr(cfg, "dynamic_leverage_min", 5) if cfg else 5
        self.max_leverage = getattr(cfg, "dynamic_leverage_max", 20) if cfg else 20
        self.vol_atr_period = getattr(cfg, "dynamic_leverage_vol_atr_period", 14) if cfg else 14
        self.high_vol_threshold = getattr(cfg, "dynamic_leverage_high_vol_threshold", 0.05) if cfg else 0.05
        self.extreme_vol_threshold = getattr(cfg, "dynamic_leverage_extreme_vol_threshold", 0.10) if cfg else 0.10
        self.dd_severe = getattr(cfg, "dynamic_leverage_drawdown_severe", 0.10) if cfg else 0.10
        self.dd_moderate = getattr(cfg, "dynamic_leverage_drawdown_moderate", 0.05) if cfg else 0.05
        self.margin_high = getattr(cfg, "dynamic_leverage_margin_high", 0.50) if cfg else 0.50
        self.margin_moderate = getattr(cfg, "dynamic_leverage_margin_moderate", 0.25) if cfg else 0.25
        self.regime_boost = getattr(cfg, "dynamic_leverage_regime_boost", True) if cfg else True

    # Regime favourability factor (0 = stay at floor, 1 = full headroom).
    _REGIME_FACTOR = {
        "TREND_EXPANSION": 1.00,
        "BREAKOUT_ENVIRONMENT": 0.85,
        "ACCUMULATION": 0.50,
        "MEAN_REVERSION": 0.25,
        "LOW_VOL_CHOP": 0.20,
        "DEAD_MARKET": 0.0,
        "LIQUIDATION_EVENT": 0.0,
        "HIGH_RISK_CHAOS": 0.0,
    }

    def compute(
        self,
        symbol: str,
        base_leverage: int,
        venue_max_leverage: Optional[int],
        atr_pct: float = 0.0,
        drawdown: float = 0.0,
        margin_ratio: float = 0.0,
        regime: Optional[str] = None,
        conviction: Optional[float] = None,
    ) -> int:
        """Bidirectional dynamic leverage in [min_leverage, max_leverage].

        Starts at the FLOOR (min_leverage, e.g. 5x) and climbs toward the ceiling
        (min(max_leverage, venue cap), e.g. 20x) only when conditions are
        favourable — low volatility, strong trending regime, a healthy account,
        and (optionally) high signal conviction. Any single risk factor pulls it
        back toward the floor (factors are multiplicative). Returns 0 to halt on
        extreme volatility or a no-trade regime.

        ``base_leverage`` is treated as an additional upper clamp (the configured
        MAX_LEVERAGE ceiling); it no longer acts as the default operating point.
        ``conviction`` (0..1, e.g. LLM/technical confidence) is optional; when
        None it does not influence the result.
        """
        ceiling = min(self.max_leverage, base_leverage or self.max_leverage)
        if venue_max_leverage:
            ceiling = min(ceiling, venue_max_leverage)
        floor = min(self.min_leverage, ceiling)
        span = max(0, ceiling - floor)
        if span == 0:
            return ceiling

        # Hard halts.
        if atr_pct >= self.extreme_vol_threshold:
            logger.warning("[DYNAMIC-LEVERAGE] %s ATR%% %.2f%% >= extreme %.2f%% → 0 (halt)",
                           symbol, atr_pct * 100, self.extreme_vol_threshold * 100)
            return 0
        regime_up = (regime or "").upper()
        regime_factor = self._REGIME_FACTOR.get(regime_up, 0.5)  # unknown → neutral
        if regime_up in ("DEAD_MARKET", "LIQUIDATION_EVENT", "HIGH_RISK_CHAOS"):
            logger.warning("[DYNAMIC-LEVERAGE] %s regime %s → 0 (halt)", symbol, regime)
            return 0

        # Favourability factors, each in [0,1]; 1 = full headroom toward ceiling.
        # Volatility: full headroom below high threshold, 0 at extreme (linear).
        if atr_pct <= self.high_vol_threshold:
            vol_factor = 1.0
        else:
            vol_factor = max(0.0, 1.0 - (atr_pct - self.high_vol_threshold)
                             / max(1e-9, self.extreme_vol_threshold - self.high_vol_threshold))

        # Account health: drawdown.
        if drawdown <= self.dd_moderate:
            dd_factor = 1.0
        elif drawdown >= self.dd_severe:
            dd_factor = 0.0
        else:
            dd_factor = 1.0 - (drawdown - self.dd_moderate) / max(1e-9, self.dd_severe - self.dd_moderate)

        # Account health: margin utilization.
        if margin_ratio <= self.margin_moderate:
            mr_factor = 1.0
        elif margin_ratio >= self.margin_high:
            mr_factor = 0.0
        else:
            mr_factor = 1.0 - (margin_ratio - self.margin_moderate) / max(1e-9, self.margin_high - self.margin_moderate)

        conv_factor = 1.0 if conviction is None else max(0.0, min(1.0, float(conviction)))

        # Multiplicative: lever up only when ALL conditions are favourable.
        score = vol_factor * regime_factor * dd_factor * mr_factor * conv_factor
        lev = int(round(floor + score * span))
        lev = max(floor, min(lev, ceiling))
        logger.info(
            "[DYNAMIC-LEVERAGE] %s → %dx (floor=%d ceil=%d score=%.2f | vol=%.2f regime=%.2f dd=%.2f mr=%.2f conv=%.2f)",
            symbol, lev, floor, ceiling, score, vol_factor, regime_factor, dd_factor, mr_factor, conv_factor,
        )
        return lev
