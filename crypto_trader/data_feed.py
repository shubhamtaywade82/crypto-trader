"""
crypto_trader.data_feed — Binance USD-M Futures Market Data Client
=====================================================================
Fetches OHLCV, mark price, funding rate, and open interest.
Handles rate limits, IP bans, and retries with exponential backoff.
"""

import os
import time
import logging
from typing import Optional

import requests
import pandas as pd

logger = logging.getLogger("crypto_trader.data_feed")

BINANCE_FAPI_BASE = "https://fapi.binance.com"


class BinanceDataFeed:
    """Production-grade Binance Futures REST client."""

    def __init__(
        self,
        base_url: str = BINANCE_FAPI_BASE,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        log_responses: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.log_responses = log_responses or os.getenv("LOG_BINANCE_RESPONSES", "false").lower() in ("true", "1", "yes")
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "crypto-trader/4.0",
        })

    def _request(self, method: str, endpoint: str, params: dict = None) -> dict:
        """Make a request with retry logic and rate-limit handling.
        For Binance Futures API, the endpoint path differs:
        - Endpoints starting with "v1/" are under the standard '/fapi' namespace.
        - Endpoints starting with "futures/" already include the full path and should not be prefixed.
        """
        if endpoint.startswith("v1/"):
            # Classic Binance Futures endpoints (e.g., klines, mark price)
            url = f"{self.base_url}/fapi/{endpoint}"
        else:
            # Full path endpoints such as 'futures/data/...'
            url = f"{self.base_url}/{endpoint}"
        last_exception = None

        if self.log_responses:
            logger.info(f"[REST API Request] {method} {url} | Params: {params}")

        for attempt in range(self.max_retries):
            try:
                if method == "GET":
                    resp = self.session.get(url, params=params, timeout=15)
                else:
                    resp = self.session.request(method, url, params=params, timeout=15)

                # Handle IP ban (418)
                if resp.status_code == 418:
                    sleep_sec = 120 * (attempt + 1)
                    logger.critical(f"IP BANNED by Binance. Sleeping {sleep_sec}s...")
                    time.sleep(sleep_sec)
                    continue

                # Handle rate limit (429)
                if resp.status_code == 429:
                    sleep_sec = 60 * (attempt + 1)
                    logger.warning(f"Rate limited. Backing off {sleep_sec}s...")
                    time.sleep(sleep_sec)
                    continue

                resp.raise_for_status()
                data = resp.json()
                if self.log_responses:
                    resp_str = str(data)
                    if len(resp_str) > 1000:
                        resp_str = resp_str[:1000] + "... (truncated)"
                    logger.info(f"[REST API Response] {method} {url} | Data: {resp_str}")
                return data

            except requests.Timeout:
                sleep_sec = self.backoff_base ** attempt
                logger.warning(f"Request timeout (attempt {attempt+1}). Retrying in {sleep_sec}s...")
                time.sleep(sleep_sec)
                last_exception = requests.Timeout

            except requests.HTTPError as e:
                if e.response.status_code in (418, 429):
                    continue  # Already handled above
                if e.response.status_code in (401, 403):
                    logger.debug(f"HTTP unauthorized/forbidden: {e}")
                else:
                    logger.error(f"HTTP error: {e}")
                raise

            except Exception as e:
                logger.error(f"Request failed: {e}")
                last_exception = e
                time.sleep(self.backoff_base ** attempt)

        raise last_exception if last_exception else RuntimeError("Max retries exceeded")

    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 150,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> pd.DataFrame:
        """Fetch klines and return a clean DataFrame."""
        params = {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": limit,
        }
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        data = self._request("GET", "v1/klines", params)
        if not data:
            raise ValueError("No kline data returned")

        df = pd.DataFrame(data, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ])
        numeric_cols = ["open", "high", "low", "close", "volume",
                        "quote_volume", "taker_buy_base", "taker_buy_quote"]
        df[numeric_cols] = df[numeric_cols].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        return df

    def get_mark_price(self, symbol: str) -> float:
        """Get current mark price."""
        data = self._request("GET", "v1/premiumIndex", {"symbol": symbol.upper()})
        return float(data["markPrice"])

    def get_funding_rate(self, symbol: str) -> float:
        """Get current funding rate."""
        data = self._request("GET", "v1/fundingRate", {
            "symbol": symbol.upper(),
            "limit": 1,
        })
        return float(data[0]["fundingRate"]) if data else 0.0

    def get_open_interest(self, symbol: str) -> dict:
        """Get open interest stats."""
        data = self._request("GET", "v1/openInterest", {"symbol": symbol.upper()})
        return {
            "open_interest": float(data["openInterest"]),
            "timestamp": data["time"],
        }

    def get_taker_ratio(self, symbol: str, period: str = "1h") -> float:
        """Get taker buy/sell volume ratio. >1 means more buyers."""
        params = {
            "symbol": symbol.upper(),
            "period": period,
            "limit": 1,
        }
        try:
            data = self._request("GET", "futures/data/takerlongshortRatio", params)
            if not data:
                logger.warning("Empty response for taker ratio; defaulting to 1.0")
                return 1.0
            # Support both buySellRatio and buyRatio just in case
            val = data[0].get("buySellRatio") or data[0].get("buyRatio", 1.0)
            return float(val)
        except Exception as e:
            logger.error(f"Failed to fetch or parse taker ratio: {e}")
            return 1.0

    def get_liquidation_orders(self, symbol: str, limit: int = 100) -> pd.DataFrame:
        """Get recent liquidation orders."""
        data = self._request("GET", "v1/forceOrders", {
            "symbol": symbol.upper(),
            "limit": limit,
        })
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        df["price"] = df["price"].astype(float)
        df["qty"] = df["qty"].astype(float)
        return df
