from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class WSState:
    mark_price: float | None = None
    funding_rate: float | None = None
    bid: float | None = None
    ask: float | None = None
    last_trade_price: float | None = None
    ticker_24h: dict[str, Any] = field(default_factory=dict)
    kline_1h_last: dict[str, Any] | None = None
    kline_4h_last: dict[str, Any] | None = None
    wick_buffer: deque[float] = field(default_factory=lambda: deque(maxlen=5))


class BinanceFuturesWebSocket:
    """Thread-safe websocket collector with reconnect/backoff.

    Uses websocket-client if available. Streams:
    markPrice@1s, kline_1h, kline_4h, bookTicker, aggTrade, ticker.
    """

    def __init__(self, symbol: str):
        self.symbol = symbol.lower()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self.state = WSState()
        self._failures = 0
        self.rest_fallback_mode = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _streams(self) -> str:
        return "/".join([
            f"{self.symbol}@markPrice@1s",
            f"{self.symbol}@kline_1h",
            f"{self.symbol}@kline_4h",
            f"{self.symbol}@bookTicker",
            f"{self.symbol}@aggTrade",
            f"{self.symbol}@ticker",
        ])

    def _run(self) -> None:
        try:
            import websocket  # type: ignore
        except Exception:
            logger.warning("websocket-client not installed; WS engine disabled")
            return

        url = f"wss://fstream.binance.com/stream?streams={self._streams()}"
        while not self._stop.is_set():
            backoff = min(60, 2 ** min(self._failures, 6))
            try:
                ws = websocket.WebSocket()
                ws.connect(url, timeout=10)
                self._failures = 0
                while not self._stop.is_set():
                    raw = ws.recv()
                    if not raw:
                        continue
                    self._handle(raw)
            except Exception as exc:
                self._failures += 1
                if self._failures >= 10:
                    self.rest_fallback_mode = True
                    logger.warning("WS failed %s times; switching to REST fallback mode", self._failures)
                logger.warning("WS reconnect in %ss after error: %s", backoff, exc)
                time.sleep(backoff)
            finally:
                try:
                    ws.close()
                except Exception:
                    pass

    def _handle(self, raw: str) -> None:
        payload = json.loads(raw)
        data = payload.get("data", {})
        event = data.get("e")
        with self._lock:
            if event == "markPriceUpdate":
                self.state.mark_price = float(data.get("p", 0.0))
                self.state.funding_rate = float(data.get("r", 0.0))
                if self.state.mark_price:
                    self.state.wick_buffer.append(self.state.mark_price)
            elif event == "24hrTicker":
                self.state.ticker_24h = data
            elif event == "aggTrade":
                self.state.last_trade_price = float(data.get("p", 0.0))
                if self.state.last_trade_price:
                    self.state.wick_buffer.append(self.state.last_trade_price)
            elif event == "bookTicker":
                self.state.bid = float(data.get("b", 0.0))
                self.state.ask = float(data.get("a", 0.0))
            elif event == "kline":
                k = data.get("k", {})
                interval = k.get("i")
                if interval == "1h":
                    self.state.kline_1h_last = k
                elif interval == "4h":
                    self.state.kline_4h_last = k

    def mid_price(self) -> float | None:
        with self._lock:
            if self.state.bid is None or self.state.ask is None:
                return None
            return (self.state.bid + self.state.ask) / 2

    def snapshot(self) -> WSState:
        with self._lock:
            return WSState(
                mark_price=self.state.mark_price,
                funding_rate=self.state.funding_rate,
                bid=self.state.bid,
                ask=self.state.ask,
                last_trade_price=self.state.last_trade_price,
                ticker_24h=dict(self.state.ticker_24h),
                kline_1h_last=dict(self.state.kline_1h_last) if self.state.kline_1h_last else None,
                kline_4h_last=dict(self.state.kline_4h_last) if self.state.kline_4h_last else None,
                wick_buffer=deque(self.state.wick_buffer, maxlen=5),
            )


# Backward-compatible names matching the integration guide terminology
WSMarketData = WSState
BinanceWebSocketFeed = BinanceFuturesWebSocket
