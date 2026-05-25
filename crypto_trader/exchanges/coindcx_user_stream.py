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

# CoinDCX futures account socket events (verified against docs.coindcx.com):
# the futures channel emits exactly these three. There is NO separate "fill"
# event — fills are derived from df-order-update (filled_quantity deltas).
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
        event_order: str = DEFAULT_EVENT_ORDER,
        event_position: str = DEFAULT_EVENT_POSITION,
        event_balance: str = DEFAULT_EVENT_BALANCE,
        on_reconnect: Optional[Callable[[], None]] = None,
        on_position: Optional[Callable[[dict], None]] = None,
        on_balance: Optional[Callable[[dict], None]] = None,
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
        self.on_position = on_position
        self.on_balance = on_balance
        self.event_order = event_order
        self.event_position = event_position
        self.event_balance = event_balance

        self.sio = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = False
        self._reconnect_attempts = 0
        from collections import OrderedDict
        # FIFO-bounded dedup: evict the OLDEST key, never an arbitrary (possibly
        # recent) one — a set.pop() could drop a just-seen fill and re-process it.
        self._seen_fills: "OrderedDict" = OrderedDict()
        self._filled_by_order: "OrderedDict" = OrderedDict()  # order_id -> cumulative filled_qty
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

    def _coerce_any(self, data):
        """Parse a socket payload that may be a JSON string, dict, or list."""
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                return None
        return data

    def _as_orders(self, data) -> list:
        """Normalize a df-order-update payload into a list of order dicts.
        CoinDCX may send a single order, a list, or {"orders": [...]}."""
        d = self._coerce_any(data)
        if isinstance(d, list):
            return [x for x in d if isinstance(x, dict)]
        if isinstance(d, dict):
            inner = d.get("orders")
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
            return [d]
        return []

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
            self._seen_fills[key] = None
            if len(self._seen_fills) > 5000:  # bound memory (evict oldest)
                self._seen_fills.popitem(last=False)
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
        """df-order-update carries the order object(s). A fill is a positive jump
        in ``filled_quantity`` vs the last seen value for that order id; emit the
        delta as an OrderFilledEvent (handles partial fills, dedupes repeats)."""
        for o in self._as_orders(data):
            try:
                self._handle_order_fill(o)
            except Exception as e:
                logger.error("CoinDCX order-update parse failed: %s", e)

    def _handle_order_fill(self, o: dict) -> None:
        oid = str(o.get("id") or o.get("order_id") or "")
        if not oid:
            return
        filled = float(o.get("filled_quantity", 0) or 0)
        if filled <= 0:
            return
        with self._lock:
            prev = self._filled_by_order.get(oid, 0.0)
            if filled <= prev:
                return  # no new fill (stale/duplicate status push)
            delta = filled - prev
            self._filled_by_order[oid] = filled
            if len(self._filled_by_order) > 5000:
                self._filled_by_order.pop(next(iter(self._filled_by_order)))
        pair = o.get("pair") or o.get("symbol") or ""
        fill = {
            "exchange_order_id": oid,
            "symbol": coindcx_to_internal(pair) if pair else "",
            "fill_price": float(o.get("avg_price", o.get("price_per_unit", 0)) or 0),
            "fill_quantity": delta,
            "fee": float(o.get("fee_amount", o.get("fee", 0)) or 0),
            "is_maker": bool(o.get("is_maker", False)),
            "side": o.get("side", ""),
            "status": o.get("status", ""),
            "timestamp": int(float(o.get("updated_at", o.get("created_at", 0)) or 0)),
        }
        if self.bus:
            self.bus.publish(OrderFilledEvent(
                symbol=fill["symbol"], side=str(fill["side"]),
                exchange_order_id=oid, fill_price=fill["fill_price"],
                fill_quantity=fill["fill_quantity"], fee=fill["fee"],
            ))
        if self.on_fill:
            try:
                self.on_fill(fill)
            except Exception as e:
                logger.error("CoinDCX stream on_fill handler failed: %s", e)

    def _on_position_update(self, data) -> None:
        d = self._coerce(data)
        logger.debug("CoinDCX position update: %s", d)
        if self.on_position and d:
            try:
                self.on_position(d)
            except Exception as e:
                logger.error("CoinDCX on_position handler failed: %s", e)

    def _on_balance_update(self, data) -> None:
        logger.debug("CoinDCX balance update raw: %s", data)
        parsed = self._coerce_any(data)
        if parsed is None:
            return
        
        updates = parsed if isinstance(parsed, list) else [parsed]
        for item in updates:
            if not isinstance(item, dict):
                continue
            if self.on_balance:
                try:
                    self.on_balance(item)
                except Exception as e:
                    logger.error("CoinDCX on_balance handler failed: %s", e)
