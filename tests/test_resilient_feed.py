"""ResilientDataFeed failover + CoinDCX stale-candle guard."""
import time

import pandas as pd
import pytest

from crypto_trader.exchanges.resilient_data_feed import (
    CoinDCXDataFeed,
    ResilientDataFeed,
    StaleCandlesError,
)


class _Primary:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def get_klines(self, symbol, interval, limit=150, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("451 geo-block")
        return pd.DataFrame({"close": [1, 2, 3]})

    def get_mark_price(self, symbol):
        if self.fail:
            raise RuntimeError("451")
        return 100.0


class _Fallback:
    def get_klines(self, symbol, interval, limit=150, **kw):
        return pd.DataFrame({"close": [9, 9]})

    def get_mark_price(self, symbol):
        return 85.0


def test_uses_primary_when_healthy():
    feed = ResilientDataFeed(_Primary(fail=False), _Fallback())
    df = feed.get_klines("SOLUSDT", "4h")
    assert list(df["close"]) == [1, 2, 3]
    assert feed.active == "primary"
    assert feed.get_mark_price("SOLUSDT") == 100.0


def test_falls_back_when_primary_fails():
    feed = ResilientDataFeed(_Primary(fail=True), _Fallback())
    df = feed.get_klines("SOLUSDT", "4h")
    assert list(df["close"]) == [9, 9]
    assert feed.active == "fallback"
    assert feed.get_mark_price("SOLUSDT") == 85.0


def test_falls_back_on_empty_primary():
    class _EmptyPrimary:
        def get_klines(self, *a, **k):
            return pd.DataFrame()

    feed = ResilientDataFeed(_EmptyPrimary(), _Fallback())
    df = feed.get_klines("SOLUSDT", "4h")
    assert list(df["close"]) == [9, 9]


def test_coindcx_klines_stale_guard(monkeypatch):
    feed = CoinDCXDataFeed(max_staleness_intervals=3)

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            # a single 4h candle ~100 days old -> must be rejected
            old_ms = int(time.time() * 1000) - 100 * 86_400_000
            return [{"open": 1, "high": 2, "low": 0.5, "close": 1.5,
                     "volume": 10, "time": old_ms}]

    monkeypatch.setattr(feed.session, "get", lambda *a, **k: _Resp())
    with pytest.raises(StaleCandlesError):
        feed.get_klines("SOLUSDT", "4h", limit=10)


def test_coindcx_klines_fresh_builds_binance_schema(monkeypatch):
    feed = CoinDCXDataFeed(max_staleness_intervals=3)

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            now = int(time.time() * 1000)
            return [
                {"open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10, "time": now - 3_600_000},
                {"open": 1.5, "high": 2.2, "low": 1.0, "close": 2.0, "volume": 12, "time": now},
            ]

    monkeypatch.setattr(feed.session, "get", lambda *a, **k: _Resp())
    df = feed.get_klines("SOLUSDT", "1h", limit=10)
    for col in ["open_time", "open", "high", "low", "close", "volume", "quote_volume", "close_time"]:
        assert col in df.columns
    assert df["close"].iloc[-1] == 2.0
    assert str(df["open_time"].dtype).startswith("datetime64")
