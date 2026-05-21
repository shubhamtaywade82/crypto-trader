from __future__ import annotations
import logging, time
from dataclasses import dataclass
import pandas as pd
import requests

logger = logging.getLogger(__name__)

@dataclass
class BinanceDataFeed:
    base_url: str = "https://fapi.binance.com"
    timeout: int = 15

    def __post_init__(self):
        self.base_url = self.base_url.rstrip("/")
        self.session = requests.Session()

    def _get(self, endpoint: str, params: dict | None = None):
        url = f"{self.base_url}/fapi/{endpoint}"
        try:
            r = self.session.get(url, params=params, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            sc = e.response.status_code if e.response is not None else None
            if sc == 418:
                logger.critical("Binance IP banned (418). Sleeping 120s")
                time.sleep(120)
            elif sc == 429:
                logger.warning("Binance rate limited (429). Sleeping 60s")
                time.sleep(60)
            raise

    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 150) -> pd.DataFrame:
        data = self._get("v1/klines", {"symbol": symbol.upper(), "interval": interval, "limit": limit})
        cols = ["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_buy_base","taker_buy_quote","ignore"]
        df = pd.DataFrame(data, columns=cols)
        num = ["open","high","low","close","volume","quote_volume","taker_buy_base","taker_buy_quote"]
        df[num] = df[num].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        return df

    def get_mark_price(self, symbol: str) -> float:
        return float(self._get("v1/premiumIndex", {"symbol": symbol.upper()})["markPrice"])
