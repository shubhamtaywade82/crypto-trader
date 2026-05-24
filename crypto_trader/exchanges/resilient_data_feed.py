"""
crypto_trader.exchanges.resilient_data_feed — Binance-primary / CoinDCX-fallback feed
======================================================================================
A drop-in replacement for ``BinanceDataFeed`` (same method surface) that the
strategy loop (``engine_ws._signal_tick``) consumes. It prefers Binance and
transparently falls back to CoinDCX for klines, mark price, and funding when
Binance is unavailable (e.g. HTTP 451 geo-block) — so the live signal tick keeps
working on a region-restricted VPS.

CoinDCX candles: GET https://public.coindcx.com/market_data/candles?pair=&interval=&limit=
  -> [{"open","high","low","close","volume","time"(ms)}, ...]
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

import pandas as pd
import requests

from ..data_feed import BinanceDataFeed
from .coindcx_market_data import CoinDCXMarketData
from .instrument_mapper import internal_to_coindcx

logger = logging.getLogger("crypto_trader.exchanges.resilient_data_feed")

_CANDLES_URL = "https://public.coindcx.com/market_data/candles"

# Binance kline DataFrame schema produced by BinanceDataFeed.get_klines
_KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
]
# Approximate ms duration per interval, to synthesize close_time
_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}


class StaleCandlesError(Exception):
    """Raised when CoinDCX candle data is too old to trade on safely."""


class CoinDCXDataFeed:
    """CoinDCX-backed feed exposing the BinanceDataFeed method surface.

    NOTE: CoinDCX's public candle endpoint has been observed serving frozen
    history. ``get_klines`` therefore enforces a freshness guard and raises
    ``StaleCandlesError`` when the latest candle is too old, so the strategy
    loop skips the tick rather than trading on stale data.
    """

    def __init__(self, timeout: int = 12, max_staleness_intervals: int = 3):
        self.timeout = timeout
        self.max_staleness_intervals = max_staleness_intervals
        self.session = requests.Session()
        self._market = CoinDCXMarketData()

    def get_klines(self, symbol: str, interval: str, limit: int = 150, **_) -> pd.DataFrame:
        pair = internal_to_coindcx(symbol)
        resp = self.session.get(
            _CANDLES_URL,
            params={"pair": pair, "interval": interval, "limit": limit},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"No CoinDCX candles for {pair} {interval}")

        # Freshness guard: refuse stale candle history.
        dur_ms = _INTERVAL_MS.get(interval, 0)
        latest_ms = max(int(r["time"]) for r in rows)
        age_ms = int(time.time() * 1000) - latest_ms
        if dur_ms and age_ms > self.max_staleness_intervals * dur_ms:
            raise StaleCandlesError(
                f"CoinDCX {pair} {interval} candles stale by {age_ms / 3_600_000:.1f}h "
                f"(latest {pd.to_datetime(latest_ms, unit='ms', utc=True)})"
            )

        rows = sorted(rows, key=lambda r: r["time"])  # ascending, like Binance
        dur = dur_ms
        df = pd.DataFrame({
            "open_time": [int(r["time"]) for r in rows],
            "open": [float(r["open"]) for r in rows],
            "high": [float(r["high"]) for r in rows],
            "low": [float(r["low"]) for r in rows],
            "close": [float(r["close"]) for r in rows],
            "volume": [float(r["volume"]) for r in rows],
        })
        df["close_time"] = df["open_time"] + (dur - 1 if dur else 0)
        # quote_volume unavailable from CoinDCX; approximate as base volume * close
        df["quote_volume"] = df["volume"] * df["close"]
        df["trades"] = 0
        df["taker_buy_base"] = 0.0
        df["taker_buy_quote"] = 0.0
        df["ignore"] = 0
        df = df[_KLINE_COLUMNS]
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        return df

    def get_mark_price(self, symbol: str) -> float:
        return self._market.get_mark_price(symbol)

    def get_funding_rate(self, symbol: str) -> float:
        return self._market.get_funding_rate(symbol)

    def get_open_interest(self, symbol: str) -> dict:
        # Not exposed by the public CoinDCX feed; neutral default (oi_delta is 0 in the loop).
        return {"open_interest": 0.0, "timestamp": int(time.time() * 1000)}

    def get_taker_ratio(self, symbol: str, period: str = "1h") -> float:
        return 1.0  # neutral when unavailable


class ResilientDataFeed:
    """Tries the primary feed, falls back to the secondary on failure/empty."""

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback
        self.active = "primary"

    @classmethod
    def from_config(cls, cfg) -> "ResilientDataFeed":
        from ..config import DataSource
        binance = BinanceDataFeed()
        coindcx = CoinDCXDataFeed()
        if cfg.data_source == DataSource.COINDCX:
            return cls(coindcx, coindcx)
        if cfg.data_source == DataSource.BINANCE:
            return cls(binance, binance)
        return cls(binance, coindcx)  # AUTO

    def _call(self, method: str, *args, **kwargs):
        try:
            result = getattr(self.primary, method)(*args, **kwargs)
            if result is not None and not (hasattr(result, "empty") and result.empty):
                if self.active != "primary":
                    logger.info("Data feed -> primary (%s)", type(self.primary).__name__)
                    self.active = "primary"
                return result
            raise ValueError("empty primary result")
        except Exception as e:
            logger.warning("Primary feed %s failed (%s); falling back", method, e)
            result = getattr(self.fallback, method)(*args, **kwargs)
            if self.active != "fallback":
                logger.info("Data feed -> fallback (%s)", type(self.fallback).__name__)
                self.active = "fallback"
            return result

    def get_klines(self, symbol, interval, limit=150, **kw):
        return self._call("get_klines", symbol, interval, limit=limit, **kw)

    def get_mark_price(self, symbol):
        return self._call("get_mark_price", symbol)

    def get_funding_rate(self, symbol):
        return self._call("get_funding_rate", symbol)

    def get_open_interest(self, symbol):
        return self._call("get_open_interest", symbol)

    def get_taker_ratio(self, symbol, period="1h"):
        return self._call("get_taker_ratio", symbol, period=period)
