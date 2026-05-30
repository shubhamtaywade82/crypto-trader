"""
crypto_trader.strategies.regime_switch — Volatility Regime Switcher
====================================================================
Trades Supertrend2 only when the market is in a clear trend regime.
Sits flat (no entries) during chop, low-volatility, or mean-reversion regimes.

Uses the existing RegimeClassifier / ExtendedRegime logic from the engine,
so the regime labels are byte-for-byte what the live bot computes.

Regimes that ALLOW entries:
  TREND_EXPANSION, BREAKOUT_ENVIRONMENT

Regimes that BLOCK entries:
  LOW_VOL_CHOP, DEAD_MARKET, MEAN_REVERSION, ACCUMULATION,
  LIQUIDATION_EVENT, HIGH_RISK_CHAOS
"""
from __future__ import annotations

import logging
from typing import Optional, Dict

import pandas as pd

from ..regime import ExtendedRegime, RegimeClassifier, RegimeContext
from ..strategies.supertrend2 import PlaybookSupertrend2
from ..wallet import PositionSide

logger = logging.getLogger("crypto_trader.strategies.regime_switch")


class PlaybookRegimeSwitch:
    """
    Supertrend2 gated by regime.

    Parameters match Supertrend2 where applicable so the two playbooks are
    drop-in replacements for each other.
    """

    name = "playbook_regime_switch"

    def __init__(
        self,
        atr_period: int = 10,
        factor: float = 3.0,
        use_adaptive_atr: bool = True,
        timeframe: str = "1h",
        htf_timeframe: str = "4h",
        entry_mode: str = "flip",
        retracement_pct: float = 1.0,
        flip_lookback: int = 5,
        use_htf_filter: bool = True,
        use_vol_filter: bool = False,
        vol_multiplier: float = 1.2,
        use_adx_filter: bool = False,
        adx_threshold: int = 20,
        tp1_pct: float = 1.0,
        use_tp: bool = True,
        sl_mode: str = "supertrend",
        sl_pct: float = 1.5,
        sl_atr_mult: float = 2.5,
        max_hold_hours: int = 168,
        # Regime gating
        allowed_regimes: Optional[list] = None,
    ):
        self.st2 = PlaybookSupertrend2(
            atr_period=atr_period, factor=factor, use_adaptive_atr=use_adaptive_atr,
            timeframe=timeframe, htf_timeframe=htf_timeframe,
            entry_mode=entry_mode, retracement_pct=retracement_pct,
            flip_lookback=flip_lookback, use_htf_filter=use_htf_filter,
            use_vol_filter=use_vol_filter, vol_multiplier=vol_multiplier,
            use_adx_filter=use_adx_filter, adx_threshold=adx_threshold,
            tp1_pct=tp1_pct, use_tp=use_tp, sl_mode=sl_mode,
            sl_pct=sl_pct, sl_atr_mult=sl_atr_mult, max_hold_hours=max_hold_hours,
        )
        self.allowed_regimes = set(allowed_regimes or [
            ExtendedRegime.TREND_EXPANSION.value,
            ExtendedRegime.BREAKOUT_ENVIRONMENT.value,
        ])
        self.classifier = RegimeClassifier()
        logger.info(
            "[REGIME-SWITCH] initialised | allowed=%s",
            sorted(self.allowed_regimes),
        )

    def apply_overrides(self, ov: dict) -> None:
        self.st2.apply_overrides(ov)

    # ── public API ──

    def evaluate(
        self,
        df_1h: pd.DataFrame,
        df_htf: Optional[pd.DataFrame] = None,
        regime_ctx: Optional[RegimeContext] = None,
    ) -> Optional[Dict]:
        """
        Generate a trade setup ONLY when the regime permits.

        ``regime_ctx`` is passed from the engine (computed by
        _compute_regime_context).  When None, we fall back to a simple
        ADX-based gate so the backtest can run independently.
        """
        regime_ok = self._regime_allows(df_1h, regime_ctx)
        if not regime_ok:
            return None
        return self.st2.evaluate(df_1h, df_htf)

    def get_current_direction(self, df_1h: pd.DataFrame) -> Optional[int]:
        return self.st2.get_current_direction(df_1h)

    # ── regime gate ──

    def _regime_allows(self, df: pd.DataFrame, regime_ctx: Optional[RegimeContext]) -> bool:
        if regime_ctx is not None:
            allowed = regime_ctx.extended.value in self.allowed_regimes
            if not allowed:
                logger.debug(
                    "[REGIME-SWITCH] blocked: regime=%s", regime_ctx.extended.value
                )
            return allowed

        # Fallback: compute a simple ADX-based gate for backtest independence
        if len(df) < 30 or "adx" not in df.columns:
            return True  # insufficient data → permissive
        adx = float(df["adx"].iloc[-1])
        if pd.isna(adx):
            return True
        allowed = adx >= 25.0
        if not allowed:
            logger.debug("[REGIME-SWITCH] blocked: adx=%.1f < 25", adx)
        return allowed
