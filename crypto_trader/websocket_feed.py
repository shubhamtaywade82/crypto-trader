from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

WS_BASE = "wss://fstream.binance.com/stream"


def _ws_import():
    try:
        import websocket  # type: ignore
        return websocket
    except Exception:
        return None


@dataclass
class RealtimeTicker:
    symbol: str
    stream: str = "@bookTicker"
    reconnect_delay: int = 5

    def __post_init__(self):
        self.symbol = self.symbol.lower()
        self.last_price: Optional[float] = None
        self.last_bid: Optional[float] = None
        self.last_ask: Optional[float] = None
        self.last_event_time: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.is_running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self):
        ws = _ws_import()
        if ws is None:
            logger.warning("websocket-client not installed; realtime stream disabled")
            return

        stream_name = f"{self.symbol}{self.stream}"
        query = urlencode({"streams": stream_name})
        url = f"{WS_BASE}?{query}"

        while not self._stop.is_set():
            try:
                ws_app = ws.WebSocket()
                ws_app.connect(url, timeout=10)
                while not self._stop.is_set():
                    raw = ws_app.recv()
                    if not raw:
                        continue
                    self._handle_message(raw)
            except Exception as e:
                logger.warning("WS disconnected (%s). reconnecting in %ss", e, self.reconnect_delay)
                time.sleep(self.reconnect_delay)
            finally:
                try:
                    ws_app.close()
                except Exception:
                    pass

    def _handle_message(self, raw: str):
        msg = json.loads(raw)
        data = msg.get("data", msg)

        if "b" in data and "a" in data:
            self.last_bid = float(data.get("b"))
            self.last_ask = float(data.get("a"))
            self.last_price = (self.last_bid + self.last_ask) / 2
            self.last_event_time = int(data.get("E", 0))
        elif "p" in data:
            self.last_price = float(data.get("p"))
            self.last_event_time = int(data.get("E", 0))


class PublicWebSocketProbe:
    @staticmethod
    def ping_rest_time(base_url: str = "https://fapi.binance.com") -> int:
        req = Request(f"{base_url.rstrip('/')}/fapi/v1/time", headers={"Accept": "application/json"})
        with urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return int(payload["serverTime"])
