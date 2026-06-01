"""
crypto_trader.config_store — runtime config hot-reload.

A `ConfigStore` mtime-polls a JSON overrides file (written by the calibration
approver, Phase 3) and hands the live engine a dict of changed parameters once
per change. Mirrors the AdaptiveThresholdManager pattern: file-backed state,
poll-on-demand, last-good fallback on a corrupt read — no inotify, no thread.

Params are classified so the engine knows what is safe to swap live:

  SAFE_KEYS      apply any tick (thresholds, entry filters, risk caps).
  FLAT_ONLY_KEYS apply only when the symbol is flat — these define the stop and
                 changing them mid-position would desync the live venue SL.
  RESTART_KEYS   ignored with a warning — they change kline shape / stream
                 topology / strategy identity and need a full restart.

Anything not in these sets is ignored (whitelist semantics): an overrides file
can never reach a parameter the author didn't explicitly bless here, which keeps
safety gates (kill switch, etc.) out of reach of the hot-reload path.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("crypto_trader.config_store")

DATA_DIR = Path.home() / ".crypto_trader"


SAFE_KEYS = frozenset({
    # entry threshold + adaptive decay
    "final_score_threshold", "adaptive_min_threshold",
    "adaptive_decay_per_hour", "adaptive_target_trades_per_day",
    # supertrend2 entry filters / targets (read fresh each evaluation)
    "st2_factor", "st2_tp1_pct", "st2_use_tp",
    "st2_use_htf_filter", "st2_use_vol_filter", "st2_vol_multiplier",
    "st2_use_adx_filter", "st2_adx_threshold",
    "st2_entry_mode", "st2_retracement_pct", "st2_flip_lookback",
    # mean-reversion entry band
    "mr_entry_band",
    # SMC pipeline scoring/filter knobs (read fresh each evaluation)
    "smc_min_score", "smc_sweep_lookback", "smc_structure_lookback",
    "smc_oi_expansion_pct", "smc_volume_mult_target", "smc_rvol_target",
    "smc_liq_cascade_threshold", "smc_tp_rr",
    # Liquidity-sweep entry/target knobs (read fresh each evaluation)
    "lsw_sweep_lookback", "lsw_tp_r", "lsw_min_stop_frac", "lsw_use_htf_filter",
    # MTF-alignment entry/target knobs
    "mta_tp_r", "mta_break_lookback", "mta_min_macro_frames",
    # SMC-5C entry/filter knobs
    "smc5c_bos_lookback", "smc5c_fvg_window", "smc5c_atr_pctile", "smc5c_vol_pctile",
    # regime sizing / volatility gates
    "rvol_dead_threshold", "rvol_weak_threshold", "rvol_strong_threshold",
    "regime_size_multiplier_high", "regime_size_multiplier_low",
    "funding_extreme_threshold",
    # risk caps (tightening/loosening within whitelist; safety gates excluded)
    "max_daily_trades", "max_consecutive_losses", "max_orders_per_minute",
    "max_margin_ratio", "max_drawdown_pct", "max_daily_drawdown_pct",
    "cooldown_after_loss_minutes",
})

FLAT_ONLY_KEYS = frozenset({
    "st2_sl_mode", "st2_sl_pct", "st2_sl_atr_mult", "st2_max_hold_hours",
    "mr_stop_loss_pct",
    # SMC stop/hold definition — changing mid-position would desync the venue SL
    "smc_sl_buffer_atr", "smc_sl_atr_mult", "smc_atr_period", "smc_max_hold_hours",
    # Liquidity-sweep stop/hold definition (flat-only)
    "lsw_sl_buffer_atr", "lsw_atr_period", "lsw_max_hold_hours",
    # MTF-alignment stop/hold definition (flat-only)
    "mta_swing_lookback", "mta_min_stop_frac", "mta_atr_period", "mta_max_hold_hours",
    # SMC-5C stop/hold definition (flat-only)
    "smc5c_tp_pct", "smc5c_sl_pct", "smc5c_atr_period", "smc5c_vol_window", "smc5c_max_hold_hours",
})

RESTART_KEYS = frozenset({
    "strategy_mode", "mode", "max_leverage",
    "mr_timeframe", "st2_timeframe", "st2_htf_timeframe",
    "mr_sma_period", "st2_atr_period", "st2_use_adaptive_atr",
    # SMC topology — change kline shape / swing identity
    "smc_entry_timeframe", "smc_htf_timeframe", "smc_swing_window",
    # Liquidity-sweep topology + symbol scoping
    "lsw_timeframe", "lsw_htf_timeframe", "lsw_swing_window", "strategy_symbols",
    # MTF-alignment topology (changes kline shape / EMA identity)
    "mta_entry_timeframe", "mta_macro_tfs", "mta_ema_len",
    # SMC-5C topology
    "smc5c_entry_timeframe", "smc5c_ema_fast", "smc5c_ema_slow",
})

_KNOWN = SAFE_KEYS | FLAT_ONLY_KEYS | RESTART_KEYS


class ConfigStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else DATA_DIR / "runtime_overrides.json"
        self._mtime: float = -1.0
        self._cache: Dict = {}

    def _read_file(self) -> Optional[Dict]:
        try:
            with open(self.path, "r") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("[ConfigStore] bad overrides file, keeping last-good: %s", e)
            return None  # signal: keep previous cache
        # support both a flat dict and a {"overrides": {...}} wrapper
        if isinstance(data, dict) and "overrides" in data and isinstance(data["overrides"], dict):
            data = data["overrides"]
        return data if isinstance(data, dict) else {}

    def poll(self) -> Optional[Dict]:
        """Return the overrides dict ONLY when the file changed since the last
        poll (else None). Gives the engine clean apply-once-per-change semantics."""
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            if self._mtime != -1.0:  # file was deleted → revert signal (empty dict, once)
                self._mtime = -1.0
                self._cache = {}
                logger.info("[ConfigStore] overrides removed — reverting to base config")
                return {}
            return None
        if mtime == self._mtime:
            return None
        parsed = self._read_file()
        if parsed is None:  # corrupt; don't advance mtime so we retry next poll
            return None
        self._mtime = mtime
        self._cache = parsed
        logger.info("[ConfigStore] loaded %d override(s) from %s", len(parsed), self.path)
        return dict(parsed)

    def snapshot(self) -> Dict:
        return dict(self._cache)

    @staticmethod
    def classify(key: str) -> str:
        if key in SAFE_KEYS:
            return "safe"
        if key in FLAT_ONLY_KEYS:
            return "flat_only"
        if key in RESTART_KEYS:
            return "restart"
        return "unknown"
