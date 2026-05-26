"""
crypto_trader.exchanges.coindcx_spot_execution — CoinDCX SPOT execution adapter
================================================================================
The directional stack is futures-only; delta-neutral funding arbitrage needs a
SPOT leg too. This module adds a spot order engine that reuses the existing
``CoinDCXClient`` transport and the ``safe_mode`` live gate, hitting CoinDCX's
spot REST endpoints (distinct from the ``derivatives/futures/*`` paths):

    POST exchange/v1/orders/create     (spot order)
    POST exchange/v1/orders/status     (single order status — real endpoint)
    POST exchange/v1/orders/cancel
    GET  exchange/v1/users/balances    (signed)
    GET  exchange/v1/markets_details   (public; tick/step/min-notional per market)

Spot order create keys off a ``market`` id (e.g. ``SOLUSDT``), NOT the ``B-``-
prefixed futures pair, and ``price_per_unit`` for limit orders. Unlike futures
it supports a ``client_order_id`` and a real per-order status endpoint.

A ``PaperSpotExecutionEngine`` mirrors the live shape for Phase-0 paper sims.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Callable, Dict, List, Optional

from ..wallet import Order, OrderStatus, OrderType, PositionSide
from .. import safe_mode
from .coindcx_client import CoinDCXClient, CoinDCXError

logger = logging.getLogger("crypto_trader.exchanges.coindcx_spot_execution")

EP_SPOT_CREATE = "exchange/v1/orders/create"
EP_SPOT_STATUS = "exchange/v1/orders/status"
EP_SPOT_CANCEL = "exchange/v1/orders/cancel"
EP_SPOT_ACTIVE = "exchange/v1/orders/active_orders"
EP_BALANCES = "exchange/v1/users/balances"
EP_MARKETS_DETAILS = "exchange/v1/markets_details"

_SIDE_TO_CDCX = {PositionSide.LONG: "buy", PositionSide.SHORT: "sell"}
_CDCX_STATUS_TO_WALLET = {
    "init": OrderStatus.NEW, "open": OrderStatus.NEW, "initial": OrderStatus.NEW,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "cancelled": OrderStatus.CANCELLED, "canceled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
}


def internal_to_spot_market(symbol: str) -> str:
    """``SOLUSDT`` -> CoinDCX spot ``market`` id (``SOLUSDT``). Idempotent."""
    s = symbol.upper()
    if s.startswith("B-") and "_" in s:  # futures pair -> strip to internal
        s = s[2:].replace("_", "")
    return s


def _split_symbol(symbol: str) -> tuple[str, str]:
    s = internal_to_spot_market(symbol)
    for quote in ("USDT", "USDC", "INR", "BTC"):
        if s.endswith(quote):
            return s[: -len(quote)], quote
    return s, "USDT"


def _round_down(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        return value
    return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment


@dataclass
class SpotInstrumentSpec:
    market: str
    base_currency: str
    quote_currency: str
    price_increment: Decimal
    quantity_increment: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    maker_fee_rate: float = 0.0
    taker_fee_rate: float = 0.0

    def round_price(self, price: Decimal) -> Decimal:
        return _round_down(Decimal(str(price)), self.price_increment)

    def round_qty(self, qty: Decimal) -> Decimal:
        return _round_down(Decimal(str(qty)), self.quantity_increment)

    def validate_order(self, qty: Decimal, price: Decimal) -> Optional[str]:
        qty = Decimal(str(qty))
        price = Decimal(str(price))
        if qty < self.min_quantity:
            return f"quantity {qty} below spot min_quantity {self.min_quantity}"
        if qty * price < self.min_notional:
            return f"notional {qty * price} below spot min_notional {self.min_notional}"
        return None


class SpotInstrumentMapper:
    """Loads spot precision/limits from the public ``markets_details`` endpoint."""

    def __init__(self, client: Optional[CoinDCXClient] = None):
        self.client = client or CoinDCXClient()
        self._cache: Dict[str, SpotInstrumentSpec] = {}
        self._raw: Optional[List[dict]] = None

    def _load(self) -> List[dict]:
        if self._raw is None:
            raw = self.client.get_public(EP_MARKETS_DETAILS)
            self._raw = raw if isinstance(raw, list) else []
        return self._raw

    def get_spec(self, symbol: str, *, refresh: bool = False) -> SpotInstrumentSpec:
        market = internal_to_spot_market(symbol)
        if not refresh and market in self._cache:
            return self._cache[market]
        base, quote = _split_symbol(symbol)
        row = None
        for m in self._load():
            name = str(m.get("coindcx_name") or m.get("symbol") or "").upper()
            tcur = str(m.get("target_currency_short_name") or "").upper()
            bcur = str(m.get("base_currency_short_name") or "").upper()
            # CoinDCX labels the QUOTE as base_currency and the asset as target.
            if name == market or (tcur == base and bcur == quote):
                row = m
                break
        if row is None:
            raise CoinDCXError(f"No spot market details for {symbol} ({market})")

        def _pdec(prec_key, default="0.00000001"):
            prec = row.get(prec_key)
            if prec is None:
                return Decimal(default)
            return Decimal(1).scaleb(-int(prec))

        spec = SpotInstrumentSpec(
            market=str(row.get("coindcx_name") or row.get("symbol") or market),
            base_currency=base,
            quote_currency=quote,
            price_increment=_pdec("base_currency_precision"),
            quantity_increment=_pdec("target_currency_precision"),
            min_quantity=Decimal(str(row.get("min_quantity", "0") or "0")),
            min_notional=Decimal(str(row.get("min_notional", "0") or "0")),
            maker_fee_rate=float(row.get("maker_fee") or row.get("min_maker_fee") or 0.0) / 100.0,
            taker_fee_rate=float(row.get("taker_fee") or row.get("min_taker_fee") or 0.0) / 100.0,
        )
        self._cache[market] = spec
        logger.info("Loaded spot market %s: step=%s minNotional=%s",
                    spec.market, spec.quantity_increment, spec.min_notional)
        return spec


class CoinDCXSpotExecutionEngine:
    """Live SPOT execution venue. Real-money ops are gated by ``safe_mode``."""

    VENUE = "CoinDCX-Spot"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        *,
        client: Optional[CoinDCXClient] = None,
        spot_mapper: Optional[SpotInstrumentMapper] = None,
        i_understand_real_money: bool = False,
    ):
        self.client = client or CoinDCXClient(api_key=api_key, api_secret=api_secret)
        self.spot_mapper = spot_mapper or SpotInstrumentMapper(self.client)
        self._ack = bool(i_understand_real_money)

    def place_order(
        self,
        symbol: str,
        side: PositionSide,
        quantity: Decimal,
        order_type: OrderType,
        *,
        limit_price: Optional[Decimal] = None,
        client_order_id: Optional[str] = None,
    ) -> Order:
        safe_mode.assert_live_allowed("PLACE", venue=self.VENUE, constructor_ack=self._ack, symbol=symbol)
        spec = self.spot_mapper.get_spec(symbol)
        qty = spec.round_qty(Decimal(str(quantity)))
        ref_price = spec.round_price(Decimal(str(limit_price))) if limit_price is not None else None
        if ref_price is not None:
            err = spec.validate_order(qty, ref_price)
            if err:
                raise CoinDCXError(f"Spot order rejected pre-flight: {err}")

        order_obj: Dict[str, object] = {
            "market": spec.market,
            "side": _SIDE_TO_CDCX[side],
            "order_type": "market_order" if order_type == OrderType.MARKET else "limit_order",
            "total_quantity": float(qty),
        }
        if client_order_id:
            order_obj["client_order_id"] = client_order_id
        if order_type == OrderType.LIMIT and ref_price is not None:
            order_obj["price_per_unit"] = float(ref_price)

        resp = self.client.post_signed(EP_SPOT_CREATE, {"order": order_obj}, retry_safe=False)
        order = self._parse_order_response(resp, symbol, side, qty, order_type)
        if order_type == OrderType.MARKET:
            order = self._resolve_spot_fill(order, symbol)
        return order

    def cancel_order(self, order_id: str) -> bool:
        safe_mode.assert_live_allowed("CANCEL", venue=self.VENUE, constructor_ack=self._ack, symbol=order_id)
        try:
            self.client.post_signed(EP_SPOT_CANCEL, {"id": order_id}, retry_safe=False)
            return True
        except CoinDCXError as e:
            logger.warning("Spot cancel failed for %s: %s", order_id, e)
            return False

    def get_order_status(self, order_id: str, symbol: Optional[str] = None) -> dict:
        try:
            resp = self.client.post_signed(EP_SPOT_STATUS, {"id": order_id})
            data = resp[0] if isinstance(resp, list) and resp else resp
            if not isinstance(data, dict):
                data = {}
            return {
                "status": str(data.get("status", "unknown")).lower(),
                "filled_quantity": float(data.get("total_quantity", 0) or 0)
                - float(data.get("remaining_quantity", 0) or 0),
                "avg_price": float(data.get("avg_price", data.get("price_per_unit", 0)) or 0),
            }
        except CoinDCXError as e:
            logger.debug("spot order status fetch failed for %s: %s", order_id, e)
            return {"status": "unknown", "filled_quantity": 0.0, "avg_price": 0.0}

    def get_spot_balances(self) -> Dict[str, dict]:
        resp = self.client.get_signed(EP_BALANCES, {})
        out: Dict[str, dict] = {}
        rows = resp if isinstance(resp, list) else resp.get("balances", []) if isinstance(resp, dict) else []
        for w in rows:
            cur = str(w.get("currency", "")).upper()
            if cur:
                out[cur] = {
                    "balance": float(w.get("balance", 0) or 0),
                    "locked": float(w.get("locked_balance", 0) or 0),
                }
        return out

    def get_spot_balance(self, currency: str) -> Decimal:
        return Decimal(str(self.get_spot_balances().get(currency.upper(), {}).get("balance", 0.0)))

    def _parse_order_response(self, resp, symbol, side, qty, order_type) -> Order:
        data = resp[0] if isinstance(resp, list) and resp else resp
        if isinstance(data, dict) and "orders" in data and isinstance(data["orders"], list):
            data = data["orders"][0] if data["orders"] else {}
        if not isinstance(data, dict):
            data = {}
        status = str(data.get("status", "open")).lower()
        return Order(
            id=str(data.get("id", data.get("order_id", ""))),
            symbol=symbol, side=side, order_type=order_type, quantity=qty,
            status=_CDCX_STATUS_TO_WALLET.get(status, OrderStatus.NEW),
            created_at=int(data.get("created_at", 0) or 0),
            filled_quantity=Decimal(str(data.get("total_quantity", "0") if status == "filled" else "0")),
            avg_fill_price=Decimal(str(data.get("avg_price", data.get("price_per_unit", "0")) or "0")),
        )

    def _resolve_spot_fill(self, order: Order, symbol: str, *, attempts: int = 6,
                           delay_s: float = 0.4) -> Order:
        if order.avg_fill_price and order.avg_fill_price > 0 and order.filled_quantity > 0:
            return order
        oid = str(order.id or "")
        if not oid:
            raise CoinDCXError("spot market order returned no id — cannot confirm fill")
        for _ in range(attempts):
            time.sleep(delay_s)
            st = self.get_order_status(oid, symbol)
            if st["filled_quantity"] > 0 and st["avg_price"] > 0:
                order.avg_fill_price = Decimal(str(st["avg_price"]))
                order.filled_quantity = Decimal(str(st["filled_quantity"]))
                order.status = OrderStatus.FILLED
                return order
            if st["status"] in ("cancelled", "canceled", "rejected"):
                raise CoinDCXError(f"spot order {oid} terminal '{st['status']}' — no fill")
        raise CoinDCXError(f"spot order {oid} fill unconfirmed — refusing zero-price book")


class PaperSpotExecutionEngine:
    """Paper spot venue for Phase-0 sims. Fills at a mark supplied by a price
    callback, applying a configurable taker fee. No network, no safe_mode gate."""

    VENUE = "CoinDCX-Spot-Paper"

    def __init__(self, price_fn: Callable[[str], float], *, taker_fee_rate: float = 0.001):
        self.price_fn = price_fn
        self.taker_fee_rate = taker_fee_rate
        self._orders: Dict[str, Order] = {}
        self._seq = 0

    def place_order(self, symbol, side, quantity, order_type, *,
                    limit_price=None, client_order_id=None) -> Order:
        self._seq += 1
        oid = client_order_id or f"paper-spot-{self._seq}"
        px = Decimal(str(limit_price)) if (order_type == OrderType.LIMIT and limit_price) \
            else Decimal(str(self.price_fn(symbol)))
        qty = Decimal(str(quantity))
        order = Order(
            id=oid, symbol=symbol, side=side, order_type=order_type, quantity=qty,
            status=OrderStatus.FILLED, created_at=int(time.time() * 1000),
            filled_quantity=qty, avg_fill_price=px,
        )
        self._orders[oid] = order
        return order

    def cancel_order(self, order_id: str) -> bool:
        return True

    def get_order_status(self, order_id: str, symbol: Optional[str] = None) -> dict:
        o = self._orders.get(order_id)
        if not o:
            return {"status": "unknown", "filled_quantity": 0.0, "avg_price": 0.0}
        return {"status": "filled", "filled_quantity": float(o.filled_quantity),
                "avg_price": float(o.avg_fill_price)}

    def get_spot_balance(self, currency: str) -> Decimal:
        return Decimal("1000000")  # paper: effectively unlimited
