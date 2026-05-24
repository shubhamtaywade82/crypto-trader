"""
crypto_trader.config — Typed runtime configuration & validation
=================================================================
Single source of truth for trading mode, exchange credentials, leverage caps,
the active symbol, data-source selection, and persistence backend.

Loaded from environment variables (the package's ``__init__`` already populates
``os.environ`` from a ``.env`` file). Nothing here ever logs secrets.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class TradingMode(str, Enum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


class DataSource(str, Enum):
    BINANCE = "binance"
    COINDCX = "coindcx"
    AUTO = "auto"            # Binance primary, CoinDCX fallback


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _get_bool(name: str, default: bool = False) -> bool:
    return _get(name, str(default)).lower() in ("1", "true", "yes", "on")


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    try:
        return int(float(_get(name, str(default))))
    except ValueError:
        return default


@dataclass
class TradingConfig:
    mode: TradingMode = TradingMode.PAPER
    symbol: str = "SOLUSDT"
    data_source: DataSource = DataSource.AUTO

    # Risk / sizing guardrails at launch
    max_leverage: int = 2
    initial_balance: float = 1000.0

    # CoinDCX credentials (never logged / committed)
    coindcx_api_key: str = ""
    coindcx_api_secret: str = ""
    coindcx_base_url: str = "https://api.coindcx.com"

    # Persistence
    database_url: str = ""          # postgresql://...; empty => SQLite dev fallback

    # Feed freshness budget (ms) before a source is considered stale
    feed_stale_ms: int = 15_000

    @classmethod
    def from_env(cls) -> "TradingConfig":
        mode = _get("MODE", "paper").lower()
        try:
            mode_enum = TradingMode(mode)
        except ValueError:
            mode_enum = TradingMode.PAPER

        ds = _get("DATA_SOURCE", "auto").lower()
        try:
            ds_enum = DataSource(ds)
        except ValueError:
            ds_enum = DataSource.AUTO

        return cls(
            mode=mode_enum,
            symbol=_get("TRADE_SYMBOL", "SOLUSDT").upper(),
            data_source=ds_enum,
            max_leverage=_get_int("MAX_LEVERAGE", 2),
            initial_balance=_get_float("INITIAL_BALANCE", 1000.0),
            coindcx_api_key=_get("COINDCX_API_KEY"),
            coindcx_api_secret=_get("COINDCX_API_SECRET"),
            coindcx_base_url=_get("COINDCX_BASE_URL", "https://api.coindcx.com"),
            database_url=_get("DATABASE_URL"),
            feed_stale_ms=_get_int("FEED_STALE_MS", 15_000),
        )

    @property
    def is_live(self) -> bool:
        return self.mode in (TradingMode.LIVE, TradingMode.TESTNET)

    @property
    def has_coindcx_credentials(self) -> bool:
        return bool(self.coindcx_api_key and self.coindcx_api_secret)

    def validate(self) -> List[str]:
        """Return a list of human-readable problems. Empty list == OK."""
        errs: List[str] = []
        if self.max_leverage < 1:
            errs.append("MAX_LEVERAGE must be >= 1")
        if self.max_leverage > 2:
            errs.append(
                f"MAX_LEVERAGE={self.max_leverage} exceeds the launch cap of 2x"
            )
        if not self.symbol:
            errs.append("TRADE_SYMBOL is required")
        if self.is_live and not self.has_coindcx_credentials:
            errs.append(
                "Live/testnet mode requires COINDCX_API_KEY and COINDCX_API_SECRET"
            )
        return errs

    def redacted(self) -> dict:
        """Safe-to-log view of the config (no secrets)."""
        def mask(v: str) -> str:
            return (v[:4] + "****") if v else "(unset)"

        return {
            "mode": self.mode.value,
            "symbol": self.symbol,
            "data_source": self.data_source.value,
            "max_leverage": self.max_leverage,
            "initial_balance": self.initial_balance,
            "coindcx_api_key": mask(self.coindcx_api_key),
            "coindcx_api_secret": mask(self.coindcx_api_secret),
            "database_url": "postgres" if self.database_url else "sqlite (dev)",
            "feed_stale_ms": self.feed_stale_ms,
        }


def load_config() -> TradingConfig:
    return TradingConfig.from_env()
