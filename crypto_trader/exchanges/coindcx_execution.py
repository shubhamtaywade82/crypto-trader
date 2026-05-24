"""
crypto_trader.exchanges.coindcx_execution — CoinDCX futures execution adapter
==============================================================================
Implements the wallet's ``ExecutionEngine`` Protocol (place_order / cancel_order
/ sync_positions) against the CoinDCX futures REST API, plus the account-state
reads (balances / positions / open orders / fills) the reconciler needs.

Endpoint paths and payload field names follow CoinDCX's published futures API.
They are isolated as module constants so they can be confirmed against the live
account during the manual verification checklist (see plan: "signature
fragility" risk). All authenticated calls go through ``CoinDCXClient.post_signed``.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Dict, List, Optional

from ..wallet import Order, OrderStatus, OrderType, PositionSide
from .. import safe_mode
from .coindcx_client import CoinDCXClient, CoinDCXError
from .instrument_mapper import InstrumentMapper, coindcx_to_internal

logger = logging.getLogger("crypto_trader.exchanges.coindcx_execution")

# ── CoinDCX futures endpoints (confirmed against the live API) ──────────────
EP_CREATE_ORDER = "exchange/v1/derivatives/futures/orders/create"
EP_CANCEL_ORDER = "exchange/v1/derivatives/futures/orders/cancel"
EP_LIST_ORDERS = "exchange/v1/derivatives/futures/orders"
EP_POSITIONS = "exchange/v1/derivatives/futures/positions"
EP_BALANCES = "exchange/v1/users/balances"          # unified wallet (USDT margin)
EP_CROSS_MARGIN = "exchange/v1/derivatives/futures/positions/cross_margin_details"  # futures equity
EP_TRADES = "exchange/v1/derivatives/futures/trades"
EP_LEVERAGE = "exchange/v1/derivatives/futures/positions/edit_leverage"  # best-effort; leverage is also carried per-order

_SIDE_TO_CDCX = {PositionSide.LONG: "buy", PositionSide.SHORT: "sell"}
_ORDER_TYPE_TO_CDCX = {
    OrderType.MARKET: "market_order",
    OrderType.LIMIT: "limit_order",
    OrderType.STOP_MARKET: "stop_market",
    OrderType.TAKE_PROFIT: "take_profit_market",
}
_CDCX_STATUS_TO_WALLET = {
    "open": OrderStatus.NEW,
    "init": OrderStatus.NEW,
    "partial_fill": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "cancelled": OrderStatus.CANCELLED,
    "canceled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
    "expired": OrderStatus.EXPIRED,
}


class CoinDCXExecutionEngine:
    """Live execution venue. Conforms to ``wallet.ExecutionEngine``.

    Real-money order ops (place/cancel) are gated by ``safe_mode``: they require
    the constructor flag ``i_understand_real_money=True`` AND the environment
    gate (``LIVE_TRADING_ENABLED`` + ``LIVE_TRADING_ACK``) AND no HALT file.
    """

    VENUE = "CoinDCX"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        *,
        leverage: int = 2,
        client: Optional[CoinDCXClient] = None,
        mapper: Optional[InstrumentMapper] = None,
        margin_mode: str = "isolated",
        i_understand_real_money: bool = False,
    ):
        self.client = client or CoinDCXClient(api_key=api_key, api_secret=api_secret)
        self.mapper = mapper or InstrumentMapper(self.client)
        self.leverage = leverage
        self.margin_mode = margin_mode
        self._ack = bool(i_understand_real_money)
        if self._ack:
            logger.warning(
                "CoinDCXExecutionEngine constructed with i_understand_real_money=True. "
                "Orders still require LIVE_TRADING_ENABLED + LIVE_TRADING_ACK and no HALT file."
            )

    # ── ExecutionEngine Protocol ───────────────────────────────────────────
    def place_order(
        self,
        symbol: str,
        side: PositionSide,
        quantity: Decimal,
        order_type: OrderType,
        *,
        trigger_price: Optional[Decimal] = None,
        limit_price: Optional[Decimal] = None,
        reduce_only: bool = False,
        expires_at: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> Order:
        safe_mode.assert_live_allowed(
            "PLACE", venue=self.VENUE, constructor_ack=self._ack, symbol=symbol
        )
        spec = self.mapper.get_spec(symbol)
        qty = spec.round_qty(Decimal(str(quantity)))
        ref_price = limit_price or trigger_price
        if ref_price is not None:
            ref_price = spec.round_price(Decimal(str(ref_price)))
            err = spec.validate_order(qty, ref_price)
            if err:
                raise CoinDCXError(f"Order rejected pre-flight: {err}")

        order_obj: Dict[str, object] = {
            "pair": spec.pair,
            "side": _SIDE_TO_CDCX[side],
            "order_type": _ORDER_TYPE_TO_CDCX[order_type],
            "total_quantity": float(qty),
            "leverage": float(self.leverage),
            "margin_mode": self.margin_mode,
            "reduce_only": bool(reduce_only),
        }
        if limit_price is not None:
            order_obj["price"] = float(spec.round_price(Decimal(str(limit_price))))
        if trigger_price is not None:
            order_obj["stop_price"] = float(spec.round_price(Decimal(str(trigger_price))))
        if client_order_id:
            order_obj["client_order_id"] = client_order_id

        resp = self.client.post_signed(EP_CREATE_ORDER, {"order": order_obj})
        return self._parse_order_response(resp, symbol, side, qty, order_type, reduce_only, client_order_id)

    def cancel_order(self, order_id: str) -> bool:
        safe_mode.assert_live_allowed(
            "CANCEL", venue=self.VENUE, constructor_ack=self._ack, symbol=order_id
        )
        try:
            self.client.post_signed(EP_CANCEL_ORDER, {"id": order_id})
            return True
        except CoinDCXError as e:
            logger.warning("Cancel failed for %s: %s", order_id, e)
            return False

    def sync_positions(self) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for p in self.get_positions():
            sym = p["symbol"]
            if p["quantity"] and float(p["quantity"]) != 0.0:
                out[sym] = p
        return out

    # ── account-state reads (used by reconciler / account_sync) ─────────────
    def get_balances(self) -> Dict[str, float]:
        resp = self.client.post_signed(EP_BALANCES, {})
        balances: Dict[str, float] = {}
        for w in _as_list(resp):
            cur = w.get("currency_short_name") or w.get("currency") or "USDT"
            bal = w.get("balance", w.get("available_balance", 0))
            try:
                balances[cur] = float(bal)
            except (TypeError, ValueError):
                continue
        return balances

    def sync_balance(self) -> float:
        """Total futures account equity (USDT). Prefers cross-margin details,
        falls back to the unified wallet's USDT balance."""
        try:
            res = self.client.post_signed(EP_CROSS_MARGIN, {})
            if isinstance(res, dict):
                for key in ("total_account_equity", "total_wallet_balance"):
                    if res.get(key) is not None:
                        return float(res[key])
        except CoinDCXError as e:
            logger.debug("cross_margin_details unavailable (%s); using unified balance", e)
        return float(self.get_balances().get("USDT", 0.0))

    def get_positions(self) -> List[dict]:
        resp = self.client.post_signed(EP_POSITIONS, {"margin_currency_short_name": ["USDT"]})
        positions: List[dict] = []
        for raw in _as_list(resp):
            pair = raw.get("pair") or raw.get("symbol")
            if not pair:
                continue
            qty = raw.get("active_pos", raw.get("quantity", 0)) or 0
            entry = raw.get("avg_price", raw.get("entry_price", 0)) or 0
            positions.append({
                "symbol": coindcx_to_internal(pair),
                "pair": pair,
                "quantity": abs(float(qty)),
                "side": _infer_side(qty, raw.get("side")),
                "entry_price": float(entry),
                "leverage": int(float(raw.get("leverage", self.leverage) or self.leverage)),
                "raw": raw,
            })
        return positions

    def get_open_orders(self, symbol: Optional[str] = None) -> List[dict]:
        payload: Dict[str, object] = {"status": "open"}
        if symbol:
            payload["pair"] = self.mapper.get_spec(symbol).pair
        resp = self.client.post_signed(EP_LIST_ORDERS, payload)
        return [self._normalize_order_dict(o) for o in _as_list(resp)]

    def get_fills(self, symbol: Optional[str] = None) -> List[dict]:
        payload: Dict[str, object] = {}
        if symbol:
            payload["pair"] = self.mapper.get_spec(symbol).pair
        resp = self.client.post_signed(EP_TRADES, payload)
        fills: List[dict] = []
        for t in _as_list(resp):
            pair = t.get("pair", "")
            fills.append({
                "exchange_order_id": str(t.get("order_id", t.get("id", ""))),
                "symbol": coindcx_to_internal(pair) if pair else "",
                "fill_price": float(t.get("price", 0) or 0),
                "fill_quantity": float(t.get("quantity", 0) or 0),
                "fee": float(t.get("fee_amount", t.get("fee", 0)) or 0),
                "timestamp": int(t.get("timestamp", t.get("created_at", 0)) or 0),
            })
        return fills

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        spec = self.mapper.get_spec(symbol)
        lev = min(leverage, spec.max_leverage)
        try:
            self.client.post_signed(EP_LEVERAGE, {"pair": spec.pair, "leverage": float(lev)})
            self.leverage = lev
            return True
        except CoinDCXError as e:
            logger.warning("set_leverage failed: %s", e)
            return False

    # ── helpers ─────────────────────────────────────────────────────────────
    def _parse_order_response(self, resp, symbol, side, qty, order_type, reduce_only, client_order_id) -> Order:
        data = resp[0] if isinstance(resp, list) and resp else resp
        if not isinstance(data, dict):
            data = {}
        cdcx_status = str(data.get("status", "open")).lower()
        return Order(
            id=str(data.get("id", data.get("order_id", client_order_id or ""))),
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=qty,
            status=_CDCX_STATUS_TO_WALLET.get(cdcx_status, OrderStatus.NEW),
            created_at=int(data.get("created_at", 0) or 0),
            reduce_only=reduce_only,
            filled_quantity=Decimal(str(data.get("filled_quantity", "0") or "0")),
            avg_fill_price=Decimal(str(data.get("avg_price", "0") or "0")),
        )

    def _normalize_order_dict(self, o: dict) -> dict:
        pair = o.get("pair", "")
        return {
            "exchange_order_id": str(o.get("id", o.get("order_id", ""))),
            "client_order_id": o.get("client_order_id", ""),
            "symbol": coindcx_to_internal(pair) if pair else "",
            "status": str(o.get("status", "")).lower(),
            "quantity": float(o.get("total_quantity", 0) or 0),
            "filled_quantity": float(o.get("filled_quantity", 0) or 0),
            "side": o.get("side", ""),
            "reduce_only": bool(o.get("reduce_only", False)),
        }


def _as_list(resp) -> List[dict]:
    if isinstance(resp, list):
        return [r for r in resp if isinstance(r, dict)]
    if isinstance(resp, dict):
        for key in ("positions", "orders", "data", "wallets", "trades"):
            if isinstance(resp.get(key), list):
                return [r for r in resp[key] if isinstance(r, dict)]
        return [resp]
    return []


def _infer_side(qty, explicit) -> str:
    if explicit:
        e = str(explicit).lower()
        if e in ("buy", "long"):
            return "LONG"
        if e in ("sell", "short"):
            return "SHORT"
    try:
        return "LONG" if float(qty) >= 0 else "SHORT"
    except (TypeError, ValueError):
        return "LONG"
