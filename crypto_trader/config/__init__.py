"""
crypto_trader.config — Typed runtime configuration & hot-reload overrides
=======================================================================
This package merges the original `config.py` (settings) and `config_store.py`
(hot-reload) into one namespace. All public names are re-exported here so that
both ``from crypto_trader.config import TradingConfig`` and
``from crypto_trader.config_store import ConfigStore`` continue to work.

Sub-modules:
    _settings   — TradingConfig, TradingMode, DataSource, load_config, etc.
    _store      — ConfigStore, SAFE_KEYS, FLAT_ONLY_KEYS, RESTART_KEYS
"""
from __future__ import annotations

from ._settings import (
    TradingMode,
    DataSource,
    TradingProfile,
    StrategyMode,
    _ProfileDefaults,
    _TRADING_PROFILES,
    _SPEC_RISK_PER_TRADE_PCT,
    _SPEC_MAX_CONSECUTIVE_LOSSES,
    DEFAULT_FINAL_SCORE_THRESHOLD,
    _get,
    _get_bool,
    _get_float,
    _get_int,
    _VALID_KLINE_INTERVALS,
    _parse_symbol_float_map,
    _valid_interval,
    TradingConfig,
    load_config,
)

from ._store import (
    ConfigStore,
    SAFE_KEYS,
    FLAT_ONLY_KEYS,
    RESTART_KEYS,
)

__all__ = [
    "TradingMode",
    "DataSource",
    "TradingProfile",
    "StrategyMode",
    "TradingConfig",
    "load_config",
    "ConfigStore",
    "SAFE_KEYS",
    "FLAT_ONLY_KEYS",
    "RESTART_KEYS",
    "DEFAULT_FINAL_SCORE_THRESHOLD",
    "_ProfileDefaults",
    "_TRADING_PROFILES",
    "_parse_symbol_float_map",
    "_valid_interval",
]
