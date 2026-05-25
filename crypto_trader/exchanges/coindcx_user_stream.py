"""
crypto_trader.exchanges.coindcx_user_stream — CoinDCX private/user stream (F5)
==============================================================================
Real-time account events (order updates, fills, position/balance updates) over
CoinDCX's authenticated socket.io stream. This *augments* — never replaces — the
REST polling in ``coindcx_execution.get_fills`` + the reconciler: if the socket
dependency is missing or the connection drops, the system keeps working on REST.

Transport: CoinDCX uses socket.io (v2 protocol), not raw WebSocket. Auth reuses
the same HMAC-SHA256 scheme as the REST client: the ``join`` payload signs a JSON
body with the API secret. CoinDCX does not publish the exact channel/event names
in a machine-readable form, so they are config-overridable constants with the
documented community defaults below.

Refs:
  * https://docs.coindcx.com/
  * https://coindcx.com/api/
  * https://github.com/YashwantSE/CoinDCX-WebSocket-API-t  (reference impl)
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

from ..events import EventBus, OrderFilledEvent
from .coindcx_client import CoinDCXClient
from .instrument_mapper import coindcx_to_internal

logger = logging.getLogger("crypto_trader.exchanges.coindcx_user_stream")

# Documented community defaults; override via config if CoinDCX changes them.
DEFAULT_EVENT_TRADE = "df-trade"        # new fill on a futures position
DEFAULT_EVENT_ORDER = "df-order-update"
DEFAULT_EVENT_POSITION = "df-position-update"
DEFAULT_EVENT_BALANCE = "balance-update"


class CoinDCXUserStream:
    """Authenticated CoinDCX account stream. Daemon-threaded, auto-reconnecting.

    On a fill it publishes an :class:`OrderFilledEvent` on the bus (and invokes an
    optional ``on_fill`` callback) so the wallet/reconciler update without polling.
    Fills are de-duplicated by ``(exchange_order_id, timestamp)``.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        bus: Optional[EventBus] = None,
        url: str = "wss://stream.coindcx.com",
        channel: str = "coindcx",
        reconnect_base: float = 1.0,
        reconnect_max: float = 60.0,
        on_fill: Optional[Callable[[dict], None]] = None,
        event_trade: str = DEFAULT_EVENT_TRADE,
        event_order: str = DEFAULT_EVENT_ORDER,
        event_position: str = DEFAULT_EVENT_POSITION,
        event_balance: str = DEFAULT_EVENT_BALANCE,
        on_reconnect: Optional[Callable[[], None]] = None,
    ):
        self._signer = CoinDCXClient(api_key=api_key, api_secret=api_secret)
        self.api_key = api_key
        self.bus = bus
        self.url = url
        self.channel = channel
        self.reconnect_base = reconnect_base
        self.reconnect_max = reconnect_max
        self.on_fill = on_fill
        self.on_reconnect = on_reconnect
        self.event_trade = event_trade
        self.event_order = event_order
        self.event_position = event_position
        self.event_balance = event_balance

        self.sio = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = False
        self._reconnect_attempts = 0
        self._seen_fills: set = set()
        self._lock = threading.Lock()

    # ── auth ──────────────────────────────────────────────────────────────────
    def _auth_payload(self) -> dict:
        """The ``join`` payload: HMAC-SHA256 of the channel body, plus the key."""
        body = json.dumps({"channel": self.channel}, separators=(",", ":"))
        return {
            "channelName": self.channel,
            "authSignature": self._signer._sign(body),
            "apiKey": self.api_key,
        }

    # ── lifecycle ───────────────────────────────────────────────────────────────
    def is_connected(self) -> bool:
        return self._connected

    def start(self) -> bool:
        """Spawn the stream thread. Returns False (REST-only) if unavailable."""
        try:
            import socketio  # python-socketio<5 speaks the v2 protocol
        except Exception as e:  # dependency not installed
            logger.warning(
                "CoinDCX user stream disabled (python-socketio not available: %s); "
                "falling back to REST polling", e
            )
            return False
        self.sio = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
        self._bind_handlers()
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="coindcx-user-stream", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        try:
            if self.sio is not None:
                self.sio.disconnect()
        except Exception:
            pass

    def _bind_handlers(self) -> None:
        sio = self.sio

        @sio.event
        def connect():  # noqa: ANN202
            self._connected = True
            self._reconnect_attempts = 0
            try:
                sio.emit("join", self._auth_payload())
            except Exception as e:
                logger.error("CoinDCX stream join failed: %s", e)
            logger.info("CoinDCX user stream connected")
            if self.on_reconnect:
                try:
                    self.on_reconnect()
                except Exception:
                    pass

        @sio.event
        def disconnect():  # noqa: ANN202
            self._connected = False
            logger.warning("CoinDCX user stream disconnected")

        sio.on(self.event_trade, self._on_trade)
        sio.on(self.event_order, self._on_order_update)
        sio.on(self.event_position, self._on_position_update)
        sio.on(self.event_balance, self._on_balance_update)

    def _run_loop(self) -> None:
        while self._running:
            try:
                self.sio.connect(self.url, transports=["websocket"])
                self.sio.wait()
            except Exception as e:
                logger.error("CoinDCX stream connection error: %s", e)
            if not self._running:
                break
            self._reconnect_attempts += 1
            sleep_s = min(self.reconnect_base * (2 ** (self._reconnect_attempts - 1)), self.reconnect_max)
            logger.warning("CoinDCX stream reconnecting in %.1fs (attempt %d)", sleep_s, self._reconnect_attempts)
            time.sleep(sleep_s)

    # ── event handlers (also called directly by tests) ──────────────────────────
    def _coerce(self, data) -> dict:
        if isinstance(data, str):
            try:
                return json.loads(data)
            except Exception:
                return {}
        return data if isinstance(data, dict) else {}

    def _on_trade(self, data) -> None:
        """Normalize a fill to the ``get_fills`` shape, dedupe, and publish."""
        d = self._coerce(data)
        pair = d.get("pair") or d.get("symbol") or ""
        order_id = str(d.get("order_id", d.get("id", "")))
        ts = int(float(d.get("timestamp", d.get("created_at", 0)) or 0))
        key = (order_id, ts)
        with self._lock:
            if key in self._seen_fills:
                return
            self._seen_fills.add(key)
            if len(self._seen_fills) > 5000:  # bound memory
                self._seen_fills.pop()
        fill = {
            "exchange_order_id": order_id,
            "symbol": coindcx_to_internal(pair) if pair else "",
            "fill_price": float(d.get("price", 0) or 0),
            "fill_quantity": float(d.get("quantity", 0) or 0),
            "fee": float(d.get("fee_amount", d.get("fee", 0)) or 0),
            "is_maker": bool(d.get("is_maker", False)),
            "side": d.get("side", ""),
            "timestamp": ts,
        }
        if self.bus:
            self.bus.publish(OrderFilledEvent(
                symbol=fill["symbol"],
                side=str(fill["side"]),
                exchange_order_id=order_id,
                fill_price=fill["fill_price"],
                fill_quantity=fill["fill_quantity"],
                fee=fill["fee"],
            ))
        if self.on_fill:
            try:
                self.on_fill(fill)
            except Exception as e:
                logger.error("CoinDCX stream on_fill handler failed: %s", e)

    def _on_order_update(self, data) -> None:
        logger.debug("CoinDCX order update: %s", self._coerce(data))

    def _on_position_update(self, data) -> None:
        logger.debug("CoinDCX position update: %s", self._coerce(data))

    def _on_balance_update(self, data) -> None:
        logger.debug("CoinDCX balance update: %s", self._coerce(data))
