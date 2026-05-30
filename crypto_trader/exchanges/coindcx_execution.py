"""
crypto_trader.exchanges.coindcx_execution — CoinDCX futures execution adapter
==============================================================================
Implements the wallet's ``ExecutionEngine`` Protocol (place_order / cancel_order
/ sync_positions) against the CoinDCX futures REST API, plus the account-state
reads (balances / positions / open orders / fills) the reconciler needs.

Endpoint paths and payloads follow CoinDCX's official futures API:
  * orders/create, orders/cancel, orders, orders/edit  (POST signed)
  * positions, positions/exit, positions/update_leverage  (POST signed)
  * wallets, positions/cross_margin_details  (GET signed)
  * trades  (POST signed; requires pair + from_date/to_date)

Notes on CoinDCX semantics:
  * There is NO reduce_only flag and NO client_order_id on create order. Exits
    are done by placing an opposite-side order (one-way netting) or via the
    dedicated positions/exit endpoint. Idempotency is enforced locally by the
    order manager, not by the venue.
  * Order leverage must equal the position leverage or the order is rejected.
  * Margin currency may be USDT or INR depending on the funded futures wallet.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional

from ..wallet import Order, OrderStatus, OrderType, PositionSide
from .. import safe_mode
from .coindcx_client import CoinDCXClient, CoinDCXError
from .instrument_mapper import InstrumentMapper, coindcx_to_internal

logger = logging.getLogger("crypto_trader.exchanges.coindcx_execution")

# ── CoinDCX futures endpoints (per official API docs) ───────────────────────
EP_CREATE_ORDER = "exchange/v1/derivatives/futures/orders/create"
EP_CANCEL_ORDER = "exchange/v1/derivatives/futures/orders/cancel"
EP_EDIT_ORDER = "exchange/v1/derivatives/futures/orders/edit"
EP_LIST_ORDERS = "exchange/v1/derivatives/futures/orders"
EP_POSITIONS = "exchange/v1/derivatives/futures/positions"
EP_POSITION_EXIT = "exchange/v1/derivatives/futures/positions/exit"
EP_CREATE_TPSL = "exchange/v1/derivatives/futures/positions/create_tpsl"
EP_UPDATE_LEVERAGE = "exchange/v1/derivatives/futures/positions/update_leverage"
EP_WALLETS = "exchange/v1/derivatives/futures/wallets"                      # GET signed
EP_CROSS_MARGIN = "exchange/v1/derivatives/futures/positions/cross_margin_details"  # GET signed
EP_TRADES = "exchange/v1/derivatives/futures/trades"
EP_CONVERSIONS = "api/v1/derivatives/futures/data/conversions"             # GET signed (note: /api/v1)

_SIDE_TO_CDCX = {PositionSide.LONG: "buy", PositionSide.SHORT: "sell"}
_ORDER_TYPE_TO_CDCX = {
    OrderType.MARKET: "market_order",
    OrderType.LIMIT: "limit_order",
    OrderType.STOP_MARKET: "stop_market",
    OrderType.TAKE_PROFIT: "take_profit_market",
}
_CDCX_STATUS_TO_WALLET = {
    "initial": OrderStatus.NEW,
    "open": OrderStatus.NEW,
    "init": OrderStatus.NEW,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "partial_fill": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "cancelled": OrderStatus.CANCELLED,
    "canceled": OrderStatus.CANCELLED,
    "partially_cancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
    "untriggered": OrderStatus.PENDING,
}


class CoinDCXExecutionEngine:
    """Live execution venue. Conforms to ``wallet.ExecutionEngine``.

    Real-money order ops (place/cancel/exit) are gated by ``safe_mode``: they
    require the constructor flag ``i_understand_real_money=True`` AND the
    environment gate (``LIVE_TRADING_ENABLED`` + ``LIVE_TRADING_ACK``) AND no
    HALT file.
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
        margin_type: str = "isolated",
        margin_currency: str = "USDT",
        i_understand_real_money: bool = False,
    ):
        self.client = client or CoinDCXClient(api_key=api_key, api_secret=api_secret)
        # Expose the circuit breaker so the engine/risk-manager can read its state
        # (e.g. trip the kill-switch when the venue is OPEN for > N seconds).
        self.circuit_breaker = getattr(self.client, "_cb", None)
        self.mapper = mapper or InstrumentMapper(self.client)
        self.leverage = leverage
        self.margin_type = margin_type
        self.margin_currency = margin_currency.upper()
        # Read methods scan both margin currencies so positions/orders are never
        # missed when the funded wallet differs from the configured one (e.g.
        # USDT-denominated pairs margined in INR). Order placement still uses the
        # single configured margin_currency.
        self.read_margin_currencies = list(dict.fromkeys([self.margin_currency, "USDT", "INR"]))
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
        leverage: Optional[float] = None,
    ) -> Order:
        safe_mode.assert_live_allowed(
            "PLACE", venue=self.VENUE, constructor_ack=self._ack, symbol=symbol
        )
        # CoinDCX requires an order's leverage to equal the existing position's
        # leverage (422 otherwise). Callers placing reduce-only protective orders
        # against a position opened at a different leverage (e.g. an adopted
        # venue position) must pass that position's leverage here.
        order_leverage = float(leverage) if leverage else float(self.leverage)
        spec = self.mapper.get_spec(symbol)
        qty = spec.round_qty(Decimal(str(quantity)))
        ref_price = limit_price or trigger_price
        if ref_price is not None:
            ref_price = spec.round_price(Decimal(str(ref_price)))
            err = spec.validate_order(qty, ref_price)
            if err:
                raise CoinDCXError(f"Order rejected pre-flight: {err}")

        # CoinDCX has no reduce_only flag; an opposite-side order nets the
        # position. The order manager / wallet guarantee exact-qty exits.
        order_obj: Dict[str, object] = {
            "pair": spec.pair,
            "side": _SIDE_TO_CDCX[side],
            "order_type": _ORDER_TYPE_TO_CDCX[order_type],
            "total_quantity": float(qty),
            "leverage": order_leverage,
            "notification": "no_notification",
            "position_margin_type": self.margin_type,
            "margin_currency_short_name": self.margin_currency,
        }
        if limit_price is not None:
            order_obj["price"] = float(spec.round_price(Decimal(str(limit_price))))
            order_obj["time_in_force"] = "good_till_cancel"
        if trigger_price is not None:
            order_obj["stop_price"] = float(spec.round_price(Decimal(str(trigger_price))))

        # retry_safe=False: a duplicate create on timeout/5xx would double the
        # position (CoinDCX has no client_order_id idempotency).
        resp = self.client.post_signed(EP_CREATE_ORDER, {"order": order_obj}, retry_safe=False)
        order = self._parse_order_response(resp, symbol, side, qty, order_type, reduce_only)
        # CoinDCX market-order create responds before the fill settles, so the
        # avg price is usually absent (0). Booking a position at price 0 would
        # corrupt PnL/SL/TP/liquidation, so resolve the real fill before
        # returning. Resting protective orders (stop/TP) are NOT polled — they
        # fill later, by design.
        if order_type == OrderType.MARKET:
            order = self._resolve_market_fill(order, symbol)
        return order

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

    def exit_position(self, position_id: str) -> bool:
        """Close an entire position by id (CoinDCX positions/exit)."""
        safe_mode.assert_live_allowed(
            "EXIT", venue=self.VENUE, constructor_ack=self._ack, symbol=position_id
        )
        try:
            self.client.post_signed(EP_POSITION_EXIT, {"id": position_id}, retry_safe=False)
            return True
        except CoinDCXError as e:
            logger.warning("Exit failed for position %s: %s", position_id, e)
            return False

    def create_position_tpsl(
        self,
        position_id: str,
        *,
        stop_loss_price: Optional[Decimal] = None,
        take_profit_price: Optional[Decimal] = None,
    ) -> dict:
        """Attach a full-position stop-loss / take-profit to an existing position
        via CoinDCX's positions/create_tpsl endpoint.

        This is the CORRECT way to rest a protective stop on CoinDCX futures: it
        is attached to the position (stage ``tpsl_exit``) and consumes NO new
        margin — unlike placing a separate opposite-side order, which the venue
        treats as a fresh margin-locking order (→ "insufficient funds" when the
        wallet's free balance is 0 because it's all locked as position margin).

        Only ``stop_market`` / ``take_profit_market`` are supported by the venue.
        Returns the raw response dict (may contain per-leg ``success``/``error``).
        """
        safe_mode.assert_live_allowed(
            "TPSL", venue=self.VENUE, constructor_ack=self._ack, symbol=position_id
        )
        body: Dict[str, object] = {"id": position_id}
        if stop_loss_price is not None:
            body["stop_loss"] = {
                "stop_price": str(stop_loss_price),
                "order_type": "stop_market",
            }
        if take_profit_price is not None:
            body["take_profit"] = {
                "stop_price": str(take_profit_price),
                "order_type": "take_profit_market",
            }
        if "stop_loss" not in body and "take_profit" not in body:
            raise ValueError("create_position_tpsl requires at least one of SL/TP")
        # retry_safe=False: a duplicate create on timeout could double-apply; the
        # endpoint is idempotent-ish (updates existing TP/SL) but don't gamble.
        return self.client.post_signed(EP_CREATE_TPSL, body, retry_safe=False)

    def sync_positions(self) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for p in self.get_positions():
            if p["quantity"] and float(p["quantity"]) != 0.0:
                out[p["symbol"]] = p
        return out

    # ── account-state reads (used by reconciler / account_sync) ─────────────
    def get_balances(self) -> Dict[str, float]:
        """Available balance per currency from the futures wallet (signed GET)."""
        resp = self.client.get_signed(EP_WALLETS, {})
        balances: Dict[str, float] = {}
        for w in _as_list(resp):
            cur = w.get("currency_short_name") or w.get("currency")
            if not cur:
                continue
            try:
                balances[cur] = float(w.get("balance", 0) or 0)
            except (TypeError, ValueError):
                continue
        return balances

    def get_cross_margin_details(self) -> Optional[dict]:
        """Fetch full cross-margin account details (pnl, margin_ratio, equity, etc)."""
        try:
            res = self.client.get_signed(EP_CROSS_MARGIN, {})
            if isinstance(res, dict):
                return res
        except CoinDCXError as e:
            logger.debug("cross_margin_details fetch failed: %s", e)
        return None

    def sync_balance(self) -> float:
        """Available trading balance in the configured margin currency.

        USDT cross accounts: prefer ``cross_margin_details`` available balance.
        Otherwise (incl. INR isolated): the futures wallet ``balance`` field.
        """
        if self.margin_currency == "USDT":
            try:
                res = self.client.get_signed(EP_CROSS_MARGIN, {})
                if isinstance(res, dict):
                    for key in ("available_balance_cross", "total_account_equity"):
                        if res.get(key) is not None:
                            return float(res[key])
            except CoinDCXError as e:
                logger.debug("cross_margin_details unavailable (%s)", e)
        return float(self.get_balances().get(self.margin_currency, 0.0))

    def get_usdt_conversion(self) -> float:
        """USDT<>INR conversion price (INR per USDT). 1.0 when margin is USDT."""
        if self.margin_currency == "USDT":
            return 1.0
        try:
            res = self.client.get_signed(EP_CONVERSIONS, {})
            for row in _as_list(res):
                if str(row.get("target_currency_short_name", "")).upper() == "USDT":
                    return float(row.get("conversion_price", 0) or 0) or 1.0
        except CoinDCXError as e:
            logger.debug("conversions fetch failed (%s)", e)
        return 0.0

    def get_positions(self) -> List[dict]:
        resp = self.client.post_signed(EP_POSITIONS, {
            "page": "1", "size": "100",
            "margin_currency_short_name": self.read_margin_currencies,
        })
        positions: List[dict] = []
        for raw in _as_list(resp):
            pair = raw.get("pair") or raw.get("symbol")
            if not pair:
                continue
            qty = raw.get("active_pos", raw.get("quantity", 0)) or 0
            if float(qty or 0) == 0.0:
                continue  # skip flat/empty position rows
            positions.append({
                "symbol": coindcx_to_internal(pair),
                "pair": pair,
                "position_id": raw.get("id"),
                "quantity": abs(float(qty)),
                "side": _infer_side(qty, raw.get("side")),
                "entry_price": float(raw.get("avg_price", raw.get("entry_price", 0)) or 0),
                "mark_price": float(raw.get("mark_price", 0) or 0),
                "liquidation_price": float(raw.get("liquidation_price", 0) or 0),
                "leverage": int(float(raw.get("leverage", self.leverage) or self.leverage)),
                "margin_type": raw.get("margin_type"),
                "raw": raw,
            })
        return positions

    def get_open_orders(self, symbol: Optional[str] = None) -> List[dict]:
        resp = self.client.post_signed(EP_LIST_ORDERS, {
            "status": "open", "page": "1", "size": "100",
            "margin_currency_short_name": self.read_margin_currencies,
        })
        orders = [self._normalize_order_dict(o) for o in _as_list(resp)]
        if symbol:
            sym = symbol.upper()
            orders = [o for o in orders if o["symbol"] == sym]
        return orders

    def get_fills(self, symbol: Optional[str] = None, *, days: int = 7) -> List[dict]:
        if not symbol:
            return []  # CoinDCX trades query requires a pair
        spec = self.mapper.get_spec(symbol)
        today = datetime.now(timezone.utc).date()
        payload = {
            "pair": spec.pair,
            "from_date": (today - timedelta(days=days)).isoformat(),
            "to_date": today.isoformat(),
            "page": "1", "size": "100",
            "margin_currency_short_name": [self.margin_currency],
        }
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
                "is_maker": bool(t.get("is_maker", False)),
                "side": t.get("side", ""),
                "timestamp": int(float(t.get("timestamp", t.get("created_at", 0)) or 0)),
            })
        return fills

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        spec = self.mapper.get_spec(symbol)
        lev = min(leverage, spec.max_leverage)
        try:
            self.client.post_signed(EP_UPDATE_LEVERAGE, {
                "pair": spec.pair,
                "leverage": str(lev),
                "margin_currency_short_name": self.margin_currency,
            })
            self.leverage = lev
            return True
        except CoinDCXError as e:
            logger.warning("set_leverage failed: %s", e)
            return False

    def fetch_symbol_leverage(self, symbol: str) -> Optional[int]:
        """Returns the maximum leverage allowed by the venue instrument spec.

        Previously this scanned the positions list and returned the *current*
        position's leverage, which could be stale (e.g. a grandfathered 20×
        position when the venue now caps at 5×).  New orders must use the
        instrument max or they are rejected by the venue.
        """
        try:
            spec = self.mapper.get_spec(symbol)
            # Still sync margin_type from an existing position if one exists,
            # but return the instrument cap for leverage.
            try:
                resp = self.client.post_signed(EP_POSITIONS, {
                    "page": "1", "size": "100",
                    "margin_currency_short_name": self.read_margin_currencies,
                })
                pair = spec.pair
                for raw in _as_list(resp):
                    r_pair = raw.get("pair") or raw.get("symbol")
                    if r_pair == pair and raw.get("margin_type"):
                        self.margin_type = str(raw["margin_type"]).lower()
                        break
            except Exception:
                pass  # margin_type sync is best-effort
            return spec.max_leverage
        except Exception as e:
            logger.warning("fetch_symbol_leverage failed for %s: %s", symbol, e)
        return None

    def get_order_status(self, order_id: str, symbol: Optional[str] = None) -> dict:
        """Resolve a single order's terminal status (G5).

        CoinDCX has no documented per-order status endpoint, so this is
        best-effort: if the id is still in open orders it's working; otherwise we
        scan recent fills to tell ``filled`` from ``cancelled``/absent. Returns
        ``{"status", "filled_quantity", "source"}``.
        """
        oid = str(order_id)
        for o in self.get_open_orders(symbol):
            if str(o.get("exchange_order_id")) == oid:
                return {"status": o.get("status") or "open",
                        "filled_quantity": float(o.get("filled_quantity", 0) or 0),
                        "source": "open_orders"}
        if symbol:
            filled = sum(float(f.get("fill_quantity", 0) or 0)
                         for f in self.get_fills(symbol)
                         if str(f.get("exchange_order_id")) == oid)
            if filled > 0:
                return {"status": "filled", "filled_quantity": filled, "source": "fills"}
        return {"status": "unknown", "filled_quantity": 0.0, "source": "absent"}

    def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """Cancel every open order (optionally for one symbol). Returns count (G5).

        Used as a strict capital-protection sweep when the reconciler detects an
        unresolved desync. Each cancel inherits the ``safe_mode`` gate.
        """
        cancelled = 0
        for o in self.get_open_orders(symbol):
            oid = o.get("exchange_order_id")
            if oid and self.cancel_order(oid):
                cancelled += 1
        if cancelled:
            logger.warning("cancel_all_orders flattened %d venue order(s)", cancelled)
        return cancelled

    # ── helpers ─────────────────────────────────────────────────────────────
    def _parse_order_response(self, resp, symbol, side, qty, order_type, reduce_only) -> Order:
        data = resp[0] if isinstance(resp, list) and resp else resp
        if not isinstance(data, dict):
            data = {}
        cdcx_status = str(data.get("status", "open")).lower()
        return Order(
            id=str(data.get("id", data.get("order_id", ""))),
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=qty,
            status=_CDCX_STATUS_TO_WALLET.get(cdcx_status, OrderStatus.NEW),
            created_at=int(data.get("created_at", 0) or 0),
            reduce_only=reduce_only,
            filled_quantity=Decimal(str(data.get("total_quantity", "0") if cdcx_status == "filled" else "0")),
            avg_fill_price=Decimal(str(data.get("avg_price", data.get("price", "0")) or "0")),
        )

    def _resolve_market_fill(self, order: Order, symbol: str, *, attempts: int = 20,
                             delay_s: float = 0.8) -> Order:
        """Resolve a market order's real avg fill price + filled qty.

        The create response rarely carries the settled price, so poll recent
        fills for this order id (with short backoff). Raises ``CoinDCXError`` if
        the fill can't be confirmed — the caller must NOT book a zero-price
        position. A confirmed terminal cancel/reject is surfaced too.
        """
        if order.avg_fill_price and order.avg_fill_price > 0 and order.filled_quantity and order.filled_quantity > 0:
            return order
        oid = str(order.id or "")
        if not oid:
            raise CoinDCXError("market order create returned no order id — cannot confirm fill")
        last_status = "unknown"
        for i in range(attempts):
            time.sleep(delay_s)
            try:
                fills = [f for f in self.get_fills(symbol, days=1)
                         if str(f.get("exchange_order_id")) == oid]
            except CoinDCXError as e:
                logger.warning("fill lookup failed for %s (attempt %d): %s", oid, i + 1, e)
                fills = []
            filled = sum(float(f.get("fill_quantity", 0) or 0) for f in fills)
            if filled > 0:
                notional = sum(float(f.get("fill_price", 0) or 0) * float(f.get("fill_quantity", 0) or 0)
                               for f in fills)
                avg = notional / filled if filled > 0 else 0.0
                if avg > 0:
                    order.avg_fill_price = Decimal(str(avg))
                    order.filled_quantity = Decimal(str(filled))
                    order.status = OrderStatus.FILLED
                    return order
            # Not yet in fills — check if the order terminally failed.
            st = self.get_order_status(oid, symbol)
            last_status = st.get("status", "unknown")
            if last_status in ("cancelled", "canceled", "rejected"):
                raise CoinDCXError(f"market order {oid} terminal status '{last_status}' — no fill")
        raise CoinDCXError(
            f"market order {oid} fill unconfirmed after {attempts} polls "
            f"(last status '{last_status}') — refusing to book a zero-price position"
        )

    def _normalize_order_dict(self, o: dict) -> dict:
        pair = o.get("pair", "")
        return {
            "exchange_order_id": str(o.get("id", o.get("order_id", ""))),
            "symbol": coindcx_to_internal(pair) if pair else "",
            "status": str(o.get("status", "")).lower(),
            "quantity": float(o.get("total_quantity", 0) or 0),
            "filled_quantity": float(o.get("total_quantity", 0) or 0) - float(o.get("remaining_quantity", 0) or 0),
            "remaining_quantity": float(o.get("remaining_quantity", 0) or 0),
            "side": o.get("side", ""),
            "stage": o.get("stage", ""),
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
