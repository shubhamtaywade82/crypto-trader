"""
crypto_trader.exchanges.market_data_router — Primary/fallback data selection
=============================================================================
Prefers Binance, falls back to CoinDCX when Binance is unavailable or stale.
If *all* sources are stale, ``get_mark_price`` raises ``AllFeedsStale`` and the
caller (orchestrator) must disable trading for that tick.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

from ..config import DataSource
from ..events import EventBus, FeedStaleEvent

logger = logging.getLogger("crypto_trader.exchanges.market_data_router")


class AllFeedsStale(Exception):
    pass


class MarketDataRouter:
    def __init__(
        self,
        sources: List,
        *,
        max_age_ms: int = 15_000,
        bus: Optional[EventBus] = None,
    ):
        if not sources:
            raise ValueError("MarketDataRouter requires at least one source")
        self.sources = sources           # ordered by preference
        self.max_age_ms = max_age_ms
        self.bus = bus
        self.active_source: Optional[str] = None

    @classmethod
    def from_config(cls, cfg, *, bus: Optional[EventBus] = None) -> "MarketDataRouter":
        from .binance_market_data import BinanceMarketData
        from .coindcx_market_data import CoinDCXMarketData
        binance = BinanceMarketData()
        coindcx = CoinDCXMarketData()
        if cfg.data_source == DataSource.BINANCE:
            sources = [binance]
        elif cfg.data_source == DataSource.COINDCX:
            sources = [coindcx]
        else:  # AUTO: Binance primary, CoinDCX fallback
            sources = [binance, coindcx]
        return cls(sources, max_age_ms=cfg.feed_stale_ms, bus=bus)

    def get_mark_price(self, symbol: str) -> float:
        """Return a fresh mark price from the first source that yields one."""
        last_err: Optional[Exception] = None
        for src in self.sources:
            try:
                price = src.get_mark_price(symbol)
                if price and price > 0:
                    if self.active_source != src.name:
                        logger.info("Market-data source -> %s", src.name)
                        self.active_source = src.name
                    return price
            except Exception as e:
                last_err = e
                logger.debug("source %s failed: %s", src.name, e)
                continue
        # nothing returned a live price
        self._announce_all_stale(symbol)
        raise AllFeedsStale(f"no live price for {symbol}: {last_err}")

    def is_any_fresh(self, symbol: str) -> bool:
        return any(s.is_fresh(symbol, self.max_age_ms) for s in self.sources)

    def _announce_all_stale(self, symbol: str):
        if self.bus:
            self.bus.publish(FeedStaleEvent(source="all", trading_disabled=True))
        logger.critical("ALL market-data feeds stale for %s — trading disabled", symbol)
