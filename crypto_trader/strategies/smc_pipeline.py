"""
crypto_trader.strategies.smc_pipeline — Production SMC 10-stage pipeline
========================================================================
Implements the strategy described in
``docs/smc_crypto_futures_guide_refined/smc_crypto_futures_guide_refined.md``:
a sequential filter chain (Stages 1-10) that fuses Smart-Money-Concept
*structure* with *participation* data (OI / volume / RVOL / liquidation) into a
single weighted **confidence score (0-100)**. A trade is taken only when every
hard gate passes AND the score clears ``min_score`` (default 70).

Why a separate module
---------------------
The legacy playbooks read SMC structure in isolation (an OB here, an FVG there)
— exactly the retail failure mode the guide warns about. This strategy is the
guide's thesis made executable: standalone structure is necessary but not
sufficient; the edge (if any) comes from requiring *confluence* and *grading*
each setup before sizing.

The 9 scored factors and their weights (guide §4.3):

    HTF trend align        15%   (hard gate: must align)
    Liquidity sweep        15%   (hard gate: must occur, in-direction, fresh)
    Structure shift        15%   (hard gate: BOS/CHoCH in-direction, fresh)
    Order-block strength    12%
    FVG quality            10%
    OI expansion           12%   (graded; dropped+renormalised if unavailable)
    Volume confirmation    10%
    RVOL                    8%
    Liquidation context     3%   (hard gate: active cascade ⇒ reject)

Score = Σ(weightᵢ · scoreᵢ) / Σ(available weights) · 100.

Setup dict schema (same shape the engine's ``_execute_entry`` expects)
---------------------------------------------------------------------
{
    "side"            : PositionSide,
    "entry_price"     : float,
    "sl_price"        : float,
    "tp_price"        : float,
    "score"           : float,   # confidence/100, for threshold compatibility
    "confidence_score": float,   # 0-100 raw
    "size_multiplier" : float,   # 0.5 / 1.0 / 1.5 per guide score bands
    "factors"         : dict,    # per-factor 0-1 breakdown (observability)
    "reason"          : str,
    "name"            : str,
    "strategy"        : "smc",
    "playbook"        : Playbook.INTRADAY,
}

NOTE — edge is UNPROVEN. This is a faithful implementation of the guide, not a
validated money-maker. Backtest it (the harness reuses live strategy code) and
prove out-of-sample expectancy before allocating real capital.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

import numpy as np
import pandas as pd

from ..regime import compute_atr, compute_ema
from ..structure import MarketStructureAnalyzer

logger = logging.getLogger("crypto_trader.strategies.smc_pipeline")


class PlaybookSMC:
    """Production SMC pipeline — structure + participation → weighted score."""

    name = "smc"

    def __init__(
        self,
        *,
        entry_timeframe: str = "15m",
        htf_timeframe: str = "4h",
        swing_window: int = 3,
        # freshness windows (candles)
        sweep_lookback: int = 12,
        structure_lookback: int = 12,
        # participation targets
        oi_expansion_pct: float = 3.0,      # OI %Δ for full OI factor
        volume_mult_target: float = 3.0,    # displacement vol / 20-avg for full vol factor
        rvol_target: float = 1.2,           # RVOL for full RVOL factor
        liq_cascade_threshold: float = 0.5,  # liquidation_intensity ≥ this ⇒ active cascade ⇒ reject
        # scoring / sizing
        min_score: float = 70.0,
        atr_period: int = 14,
        sl_buffer_atr: float = 0.5,         # ATR padding beyond the sweep wick
        sl_atr_mult: float = 1.5,           # ATR cap on stop distance (keeps RR sane)
        tp_rr: float = 2.0,                 # TP risk-reward ceiling / fallback
        max_hold_hours: int = 24,
        # factor weights (guide §4.3); kept as attrs so they're tunable/inspectable
        w_htf: float = 0.15,
        w_sweep: float = 0.15,
        w_structure: float = 0.15,
        w_ob: float = 0.12,
        w_fvg: float = 0.10,
        w_oi: float = 0.12,
        w_vol: float = 0.10,
        w_rvol: float = 0.08,
        w_liq: float = 0.03,
    ) -> None:
        self.entry_timeframe = entry_timeframe
        self.htf_timeframe = htf_timeframe
        self.swing_window = max(2, swing_window)
        self.sweep_lookback = max(1, sweep_lookback)
        self.structure_lookback = max(1, structure_lookback)
        self.oi_expansion_pct = max(0.1, oi_expansion_pct)
        self.volume_mult_target = max(1.0, volume_mult_target)
        self.rvol_target = max(0.1, rvol_target)
        self.liq_cascade_threshold = max(0.0, liq_cascade_threshold)
        self.min_score = max(0.0, min_score)
        self.atr_period = max(2, atr_period)
        self.sl_buffer_atr = max(0.0, sl_buffer_atr)
        self.sl_atr_mult = max(0.5, sl_atr_mult)
        self.tp_rr = max(0.5, tp_rr)
        self.max_hold_hours = max(1, max_hold_hours)

        self.w_htf = w_htf
        self.w_sweep = w_sweep
        self.w_structure = w_structure
        self.w_ob = w_ob
        self.w_fvg = w_fvg
        self.w_oi = w_oi
        self.w_vol = w_vol
        self.w_rvol = w_rvol
        self.w_liq = w_liq

        # Last evaluation diagnostic (set every call, signal or not) for the
        # engine's per-tick heartbeat log.
        self.last_eval: Optional[dict] = None

    # ── tuning rail ───────────────────────────────────────────────────────────

    def apply_overrides(self, ov: dict) -> None:
        """Live-update tunable params from a hot-reload overrides dict (keys are
        prefixed ``smc_``). Only attributes that already exist are touched."""
        for key, val in ov.items():
            if not key.startswith("smc_"):
                continue
            attr = key[len("smc_"):]
            if not hasattr(self, attr):
                continue
            existing = getattr(self, attr)
            try:
                typed_val = type(existing)(val) if existing is not None else val
            except (TypeError, ValueError):
                typed_val = val
            setattr(self, attr, typed_val)

    def _min_bars(self) -> int:
        return max(self.swing_window * 2 + 1, self.atr_period + 2, 30)

    # ── HTF bias (Stage 1) ─────────────────────────────────────────────────────

    @staticmethod
    def _htf_bias(df_htf: pd.DataFrame) -> int:
        """Return +1 bull / -1 bear / 0 undecided from EMA alignment on the HTF.

        Bullish when fast>slow and price above the slow EMA (and vice-versa);
        anything else is undecided (no trade — Stage 1 eliminates ~50% per guide).
        """
        if df_htf is None or len(df_htf) < 30:
            return 0
        close = df_htf["close"]
        ema_fast = compute_ema(close, 9).iloc[-1]
        ema_slow = compute_ema(close, 21).iloc[-1]
        price = float(close.iloc[-1])
        if pd.isna(ema_fast) or pd.isna(ema_slow):
            return 0
        if ema_fast > ema_slow and price > ema_slow:
            return 1
        if ema_fast < ema_slow and price < ema_slow:
            return -1
        return 0

    # ── public API ──────────────────────────────────────────────────────────────

    def evaluate(
        self,
        df_entry: pd.DataFrame,
        df_htf: Optional[pd.DataFrame] = None,
        *,
        oi_delta: Optional[float] = None,
        oi_available: bool = False,
        rvol: float = 1.0,
        liquidation_intensity: float = 0.0,
    ) -> Optional[Dict]:
        """Run the 10-stage pipeline on the latest bar.

        Parameters
        ----------
        df_entry : OHLCV on the entry timeframe (15m default), ascending.
        df_htf   : OHLCV on the higher timeframe (4h default) for bias.
        oi_delta : open-interest %Δ over the recent window in the trade
                   direction; pass ``oi_available=False`` when the engine cannot
                   compute a real delta (the OI factor is then dropped and the
                   remaining weights renormalised, rather than silently scored 0).
        rvol     : relative volume (current / 20-bar avg).
        liquidation_intensity : 0-1 normalised recent liquidation volume.

        Returns a setup dict, or ``None`` when any hard gate fails / score < min.
        """
        from ..wallet import PositionSide, Playbook  # local import: avoids circular dep

        self.last_eval = None
        if df_entry is None or len(df_entry) < self._min_bars():
            return None

        # ── Stage 1: HTF directional bias ───────────────────────────────────────
        bias = self._htf_bias(df_htf if df_htf is not None else df_entry)
        if bias == 0:
            self.last_eval = {"stage": "htf_bias", "detail": "no clear HTF trend"}
            logger.debug("[SMC] reject: no HTF bias")
            return None
        is_long = bias == 1
        want_type = "BULLISH" if is_long else "BEARISH"

        # ── Market structure on entry TF (Stages 2-5 inputs) ───────────────────
        struct = MarketStructureAnalyzer(df_entry, swing_window=self.swing_window).analyze()

        n = len(df_entry)
        entry_px = float(df_entry["close"].iloc[-1])
        atr = float(compute_atr(df_entry, self.atr_period).iloc[-1])
        if not np.isfinite(atr) or atr <= 0:
            atr = entry_px * 0.005  # defensive fallback

        # ── Stage 2 (gate): liquidity sweep in-direction and fresh ──────────────
        sweep = struct.get("recent_sweep")
        sweep_ok = (
            sweep is not None
            and sweep.get("type") == want_type
            and sweep.get("candles_ago", 10 ** 9) <= self.sweep_lookback
        )
        if not sweep_ok:
            self.last_eval = {"stage": "sweep", "want": want_type,
                              "have": (sweep or {}).get("type")}
            logger.debug("[SMC] reject: no fresh in-direction sweep")
            return None

        # ── Stage 3 (gate): structure shift (BOS/CHoCH) in-direction and fresh ──
        brk = struct.get("recent_bos")
        structure_ok = (
            brk is not None
            and brk.get("type") == want_type
            and brk.get("candles_ago", 10 ** 9) <= self.structure_lookback
        )
        if not structure_ok:
            self.last_eval = {"stage": "structure", "want": want_type,
                              "have": (brk or {}).get("type")}
            logger.debug("[SMC] reject: no fresh in-direction BOS/CHoCH")
            return None

        # ── Stage 9 (gate): no active liquidation cascade ───────────────────────
        if liquidation_intensity >= self.liq_cascade_threshold:
            self.last_eval = {"stage": "liquidation",
                              "intensity": liquidation_intensity}
            logger.debug("[SMC] reject: active liquidation cascade %.2f", liquidation_intensity)
            return None

        # ── Per-factor 0-1 scores ───────────────────────────────────────────────
        f: Dict[str, float] = {}

        # Stage 1 quality: graded by EMA separation on the entry TF.
        ema_f = compute_ema(df_entry["close"], 9).iloc[-1]
        ema_s = compute_ema(df_entry["close"], 21).iloc[-1]
        if pd.notna(ema_f) and pd.notna(ema_s) and entry_px > 0:
            sep = abs(float(ema_f) - float(ema_s)) / entry_px
            f["htf"] = float(np.clip(sep / 0.01, 0.4, 1.0))  # 1% sep ⇒ full
        else:
            f["htf"] = 0.5

        # Stage 2 quality: sweep freshness (recent = stronger).
        f["sweep"] = float(np.clip(
            1.0 - sweep["candles_ago"] / (self.sweep_lookback + 1), 0.3, 1.0))

        # Stage 3 quality: BOS (continuation) scores higher than a fresh CHoCH
        # (reversal, lower base probability per guide §2.3), scaled by freshness.
        base = 1.0 if brk.get("event") == "BOS" else 0.6
        fresh = 1.0 - brk["candles_ago"] / (self.structure_lookback + 1)
        f["structure"] = float(np.clip(base * (0.5 + 0.5 * fresh), 0.3, 1.0))

        # Stage 4: fresh unmitigated OB in-direction, bonus if price is in/near it.
        f["ob"] = self._ob_score(struct, want_type, entry_px, atr)

        # Stage 5: unmitigated in-direction FVG, bonus if it overlaps an OB.
        f["fvg"] = self._fvg_score(struct, want_type, entry_px)

        # Stage 7: OI expansion (graded). Dropped from the weighted mean when the
        # engine cannot supply a real delta (oi_available=False).
        oi_factor_used = oi_available and oi_delta is not None
        if oi_factor_used:
            # Guide §3.4: OI *rising* during displacement confirms genuine
            # participation. Grade positive expansion against the target.
            f["oi"] = float(np.clip(max(oi_delta, 0.0) / self.oi_expansion_pct, 0.0, 1.0))

        # Stage 8: displacement-candle volume vs 20-bar average.
        f["vol"] = self._volume_score(df_entry)

        # Stage 6: RVOL of the session.
        f["rvol"] = float(np.clip(rvol / self.rvol_target, 0.0, 1.0))

        # Stage 9 quality: distance below cascade threshold (calmer = better).
        f["liq"] = float(np.clip(
            1.0 - liquidation_intensity / max(self.liq_cascade_threshold, 1e-9), 0.0, 1.0))

        # ── Weighted composite (renormalise over available weights) ─────────────
        weighted = (
            self.w_htf * f["htf"]
            + self.w_sweep * f["sweep"]
            + self.w_structure * f["structure"]
            + self.w_ob * f["ob"]
            + self.w_fvg * f["fvg"]
            + self.w_vol * f["vol"]
            + self.w_rvol * f["rvol"]
            + self.w_liq * f["liq"]
        )
        total_w = (self.w_htf + self.w_sweep + self.w_structure + self.w_ob
                   + self.w_fvg + self.w_vol + self.w_rvol + self.w_liq)
        if oi_factor_used:
            weighted += self.w_oi * f["oi"]
            total_w += self.w_oi
        confidence = (weighted / total_w) * 100.0 if total_w > 0 else 0.0

        self.last_eval = {
            "side": want_type, "confidence": round(confidence, 1),
            "min_score": self.min_score, "factors": {k: round(v, 2) for k, v in f.items()},
            "oi_used": oi_factor_used,
        }

        if confidence < self.min_score:
            logger.debug("[SMC] reject: score %.1f < %.1f", confidence, self.min_score)
            return None

        # ── SL / TP / sizing ────────────────────────────────────────────────────
        # Ideal SMC enters on the FVG/OB retrace via a limit order, with the stop
        # just beyond the sweep wick. This engine executes at market on the signal
        # bar, so the raw sweep-wick stop can be very wide (we may be entering near
        # the top of the displacement). Cap the stop to an ATR band: take the
        # TIGHTER of the sweep-wick distance and ``sl_atr_mult × ATR`` so RR stays
        # tradeable regardless of where in the leg we entered. (Known limitation:
        # see module docstring — backtest before allocating capital.)
        side = PositionSide.LONG if is_long else PositionSide.SHORT
        swept = float(sweep["swept_price"])
        atr_dist = self.sl_atr_mult * atr
        floor_dist = max(self.sl_buffer_atr, 0.5) * atr
        if is_long:
            sweep_dist = entry_px - (swept - self.sl_buffer_atr * atr)
            sl_dist = max(min(sweep_dist, atr_dist), floor_dist)
            sl = entry_px - sl_dist
        else:
            sweep_dist = (swept + self.sl_buffer_atr * atr) - entry_px
            sl_dist = max(min(sweep_dist, atr_dist), floor_dist)
            sl = entry_px + sl_dist

        tp = self._take_profit(struct, is_long, entry_px, sl)
        size_mult = self._size_multiplier(confidence)

        setup: Dict = {
            "side": side,
            "entry_price": entry_px,
            "sl_price": float(sl),
            "tp_price": float(tp),
            "score": round(confidence / 100.0, 4),
            "confidence_score": round(confidence, 1),
            "size_multiplier": size_mult,
            "factors": {k: round(v, 3) for k, v in f.items()},
            "time_stop_hours": self.max_hold_hours,
            "name": self.name,
            "strategy": "smc",
            "playbook": Playbook.INTRADAY,
            "reason": (
                f"SMC {side.value} score={confidence:.0f}/100 "
                f"(sweep {sweep['candles_ago']}c ago, {brk.get('event')} "
                f"{brk['candles_ago']}c ago, size_x={size_mult})"
            ),
        }
        logger.info(
            "[SMC SIGNAL] %s | score=%.0f | sl=%.4f tp=%.4f size_x=%.1f | factors=%s",
            side.value, confidence, sl, tp, size_mult, setup["factors"],
        )
        return setup

    # ── factor helpers ──────────────────────────────────────────────────────────

    def _ob_score(self, struct: dict, want_type: str, price: float, atr: float) -> float:
        """Fresh unmitigated in-direction OB; full credit when price sits in/near it."""
        obs = [o for o in struct.get("unmitigated_order_blocks", [])
               if o.get("type") == want_type]
        if not obs:
            return 0.0
        ob = obs[-1]
        lo, hi = float(ob["low"]), float(ob["high"])
        if lo <= price <= hi:
            return 1.0
        dist = (lo - price) if price < lo else (price - hi)
        return float(np.clip(1.0 - dist / (2.0 * atr), 0.4, 0.9))

    def _fvg_score(self, struct: dict, want_type: str, price: float) -> float:
        """Unmitigated in-direction FVG; bonus when it overlaps an in-dir OB."""
        fvgs = [g for g in struct.get("unmitigated_fvgs", [])
                if g.get("type") == want_type]
        if not fvgs:
            return 0.0
        g = fvgs[-1]
        score = 0.7
        obs = [o for o in struct.get("unmitigated_order_blocks", [])
               if o.get("type") == want_type]
        for ob in obs:
            # overlap test between [bottom,top] and [low,high]
            if float(g["bottom"]) <= float(ob["high"]) and float(g["top"]) >= float(ob["low"]):
                score = 1.0
                break
        return score

    def _volume_score(self, df: pd.DataFrame) -> float:
        col = "quote_volume" if "quote_volume" in df.columns else (
            "volume" if "volume" in df.columns else None)
        if col is None or len(df) < 21:
            return 0.0
        avg = df[col].rolling(20).mean().iloc[-2]  # avg of the 20 bars before last
        cur = float(df[col].iloc[-1])
        if pd.isna(avg) or float(avg) <= 0:
            return 0.0
        ratio = cur / float(avg)
        # full credit at volume_mult_target× (default 3×); linear below.
        return float(np.clip(ratio / self.volume_mult_target, 0.0, 1.0))

    def _take_profit(self, struct: dict, is_long: bool, entry: float, sl: float) -> float:
        """Target the next liquidity pool (opposite active swing), clamped to a
        tradeable RR band of [1R, tp_rr·R] so a distant pool can't produce an
        un-hittable target nor a too-tight one."""
        risk = abs(entry - sl)
        if risk <= 0:
            return entry * (1.01 if is_long else 0.99)
        if is_long:
            lo_tp, hi_tp = entry + risk, entry + self.tp_rr * risk
            highs = [float(s["price"]) for s in struct.get("active_swing_highs", [])
                     if float(s["price"]) > entry]
            pool = min(highs) if highs else None
            target = pool if (pool and pool > entry) else hi_tp
            return float(min(max(target, lo_tp), hi_tp))
        lo_tp, hi_tp = entry - risk, entry - self.tp_rr * risk
        lows = [float(s["price"]) for s in struct.get("active_swing_lows", [])
                if float(s["price"]) < entry]
        pool = max(lows) if lows else None
        target = pool if (pool and pool < entry) else hi_tp
        return float(max(min(target, lo_tp), hi_tp))

    @staticmethod
    def _size_multiplier(confidence: float) -> float:
        """Guide §3.7 score→size bands."""
        if confidence >= 85:
            return 1.5
        if confidence >= 70:
            return 1.0
        if confidence >= 55:
            return 0.5
        return 0.0
