"""
crypto_trader.exchanges.binance_market_data — Binance market data (primary)
============================================================================
Thin ``MarketDataSource`` wrapper over the existing ``BinanceDataFeed`` so the
router can treat Binance and CoinDCX uniformly. Binance is the preferred source
but is geo-blocked (HTTP 451) in some regions; on any failure the price simply
goes stale and the router fails over to CoinDCX.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional

from ..data_feed import BinanceDataFeed

logger = logging.getLogger("crypto_trader.exchanges.binance_market_data")


class BinanceMarketData:
    name = "binance"

    def __init__(self, feed: Optional[BinanceDataFeed] = None):
        self.feed = feed or BinanceDataFeed()
        self._symbol_update_ms: Dict[str, int] = {}

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def get_mark_price(self, symbol: str) -> float:
        price = self.feed.get_mark_price(symbol)
        if price > 0:
            self._symbol_update_ms[symbol.upper()] = self._now_ms()
        return price

    def last_update_ms(self, symbol: str) -> int:
        return self._symbol_update_ms.get(symbol.upper(), 0)

    def is_fresh(self, symbol: str, max_age_ms: int) -> bool:
        ts = self.last_update_ms(symbol)
        return ts > 0 and (self._now_ms() - ts) <= max_age_ms
