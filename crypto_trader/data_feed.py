from __future__ import annotations
import logging, time
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

logger = logging.getLogger(__name__)

@dataclass
class BinanceDataFeed:
    base_url: str = "https://fapi.binance.com"
    timeout: int = 15

    def __post_init__(self):
        self.base_url = self.base_url.rstrip("/")

    def _get(self, endpoint: str, params: dict | None = None):
        query = f"?{urlencode(params)}" if params else ""
        url = f"{self.base_url}/fapi/{endpoint}{query}"
        try:
            req = Request(url, headers={"Accept": "application/json", "User-Agent": "crypto-trader-v4/1.0"})
            with urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            sc = e.code
            if sc == 418:
                logger.critical("Binance IP banned (418). Sleeping 120s")
                time.sleep(120)
            elif sc == 429:
                logger.warning("Binance rate limited (429). Sleeping 60s")
                time.sleep(60)
            raise

    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 150) -> list[dict]:
        data = self._get("v1/klines", {"symbol": symbol.upper(), "interval": interval, "limit": limit})
        rows: list[dict] = []
        for r in data:
            rows.append({
                "open_time": datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
                "close_time": datetime.fromtimestamp(r[6] / 1000, tz=timezone.utc),
                "quote_volume": float(r[7]),
                "trades": int(r[8]),
                "taker_buy_base": float(r[9]),
                "taker_buy_quote": float(r[10]),
            })
        return rows

    def get_mark_price(self, symbol: str) -> float:
        return float(self._get("v1/premiumIndex", {"symbol": symbol.upper()})["markPrice"])
