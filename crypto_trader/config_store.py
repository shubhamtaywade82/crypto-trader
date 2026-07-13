"""
Backward-compatibility shim — ``from crypto_trader.config_store import ConfigStore``
still works. New code should import from ``crypto_trader.config`` instead.
"""
from __future__ import annotations

from .config import (
    ConfigStore as ConfigStore,
    SAFE_KEYS as SAFE_KEYS,
    FLAT_ONLY_KEYS as FLAT_ONLY_KEYS,
    RESTART_KEYS as RESTART_KEYS,
)

__all__ = ["ConfigStore", "SAFE_KEYS", "FLAT_ONLY_KEYS", "RESTART_KEYS"]
