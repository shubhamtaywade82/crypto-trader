"""
crypto_trader.exchanges.coindcx_market_data — CoinDCX market data (fallback)
=============================================================================
Reachable everywhere CoinDCX is, including regions where Binance is geo-blocked.
Mark price comes from the public real-time futures prices endpoint::

    GET https://public.coindcx.com/market_data/v3/current_prices/futures/rt
    -> {"prices": {"B-SOL_USDT": {"mp": <mark>, "ls": <last>, "fr": <funding>, ...}}}
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional

import requests

from .instrument_mapper import internal_to_coindcx

logger = logging.getLogger("crypto_trader.exchanges.coindcx_market_data")

_RT_PRICES_URL = "https://public.coindcx.com/market_data/v3/current_prices/futures/rt"


class CoinDCXMarketData:
    name = "coindcx"

    def __init__(self, cache_ttl_ms: int = 1000, timeout: int = 8):
        self.cache_ttl_ms = cache_ttl_ms
        self.timeout = timeout
        self.session = requests.Session()
        self._prices: Dict[str, dict] = {}
        self._fetched_ms: int = 0
        self._symbol_update_ms: Dict[str, int] = {}

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _refresh(self) -> None:
        if self._now_ms() - self._fetched_ms < self.cache_ttl_ms:
            return
        try:
            resp = self.session.get(_RT_PRICES_URL, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            self._prices = data.get("prices", {}) if isinstance(data, dict) else {}
            self._fetched_ms = self._now_ms()
        except Exception as e:
            logger.warning("CoinDCX price refresh failed: %s", e)

    def get_mark_price(self, symbol: str) -> float:
        self._refresh()
        pair = internal_to_coindcx(symbol)
        entry = self._prices.get(pair)
        if not entry:
            raise ValueError(f"No CoinDCX price for {pair}")
        price = float(entry.get("mp") or entry.get("ls") or 0.0)
        if price > 0:
            self._symbol_update_ms[symbol.upper()] = self._fetched_ms
        return price

    def get_funding_rate(self, symbol: str) -> float:
        self._refresh()
        entry = self._prices.get(internal_to_coindcx(symbol), {})
        return float(entry.get("fr", 0.0) or 0.0)

    def last_update_ms(self, symbol: str) -> int:
        return self._symbol_update_ms.get(symbol.upper(), 0)

    def is_fresh(self, symbol: str, max_age_ms: int) -> bool:
        ts = self.last_update_ms(symbol)
        return ts > 0 and (self._now_ms() - ts) <= max_age_ms
