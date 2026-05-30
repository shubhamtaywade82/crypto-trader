"""
crypto_trader.wallet — Futures Position & Account Manager
==========================================================
Tracks positions, PnL, margin, and persists state to disk.
Supports partial closes, trailing stops, and time stops.
"""

import json
import os
import time
import logging
import threading
import gzip
import base64
import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Literal, Tuple, Callable, Any
from typing import Protocol
from pathlib import Path
from enum import Enum
from decimal import Decimal, getcontext

logger = logging.getLogger("crypto_trader.wallet")

DATA_DIR = Path.home() / ".crypto_trader"
DATA_DIR.mkdir(exist_ok=True)
STATE_SCHEMA_VERSION = 2
getcontext().prec = 28


class PositionSide(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class Playbook(Enum):
    INTRADAY = "INTRADAY"
    SWING = "SWING"


class OrderStatus(Enum):
    NEW = "NEW"
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class OrderStateMachine:
    """Deterministic Order State Machine enforcing PRD §11.4 lifecycle rules."""
    
    VALID_TRANSITIONS = {
        OrderStatus.NEW.value: {OrderStatus.PENDING.value, OrderStatus.CANCELLED.value, OrderStatus.REJECTED.value, OrderStatus.EXPIRED.value},
        OrderStatus.PENDING.value: {OrderStatus.PARTIALLY_FILLED.value, OrderStatus.FILLED.value, OrderStatus.CANCELLED.value, OrderStatus.REJECTED.value, OrderStatus.EXPIRED.value},
        OrderStatus.PARTIALLY_FILLED.value: {OrderStatus.PARTIALLY_FILLED.value, OrderStatus.FILLED.value, OrderStatus.CANCELLED.value, OrderStatus.EXPIRED.value},
        OrderStatus.FILLED.value: set(),
        OrderStatus.CANCELLED.value: set(),
        OrderStatus.REJECTED.value: set(),
        OrderStatus.EXPIRED.value: set(),
    }

    @classmethod
    def can_transition(cls, current: str, target: str) -> bool:
        if current == target:
            return True
        allowed = cls.VALID_TRANSITIONS.get(current, set())
        return target in allowed

    @classmethod
    def transition(cls, order_dict: dict, target_status: str) -> bool:
        current = order_dict.get("status")
        if cls.can_transition(current, target_status):
            order_dict["status"] = target_status
            return True
        logger.error(f"[STATE MACHINE] Invalid transition rejected: {current} -> {target_status}")
        return False


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT = "TAKE_PROFIT"


@dataclass
class Order:
    id: str
    symbol: str
    side: PositionSide
    order_type: OrderType
    quantity: Decimal
    status: OrderStatus
    created_at: int
    reduce_only: bool = False
    trigger_price: Optional[Decimal] = None
    limit_price: Optional[Decimal] = None
    expires_at: Optional[int] = None
    filled_quantity: Decimal = Decimal("0")
    avg_fill_price: Decimal = Decimal("0")


@dataclass
class Fill:
    order_id: str
    symbol: str
    side: PositionSide
    quantity: Decimal
    fill_price: Decimal
    fee: Decimal
    timestamp: int
    sequence: int = 1


@dataclass
class PortfolioState:
    wallet_balance: Decimal = Decimal("0")
    realized_pnl_total: Decimal = Decimal("0")
    open_positions: Dict[str, dict] = field(default_factory=dict)
    orders: Dict[str, dict] = field(default_factory=dict)
    fills: List[dict] = field(default_factory=list)
    funding_total: Decimal = Decimal("0")
    violations: List[dict] = field(default_factory=list)
    last_ts: int = 0
    events_applied: int = 0

class PortfolioReducer:
    """Canonical event reducer for replay-derived portfolio state."""

    def apply(self, state: PortfolioState, event: dict):
        et = event.get("event_type")
        payload = event.get("payload", {})
        state.last_ts = int(event.get("ts", state.last_ts))
        state.events_applied += 1

        if et == "WALLET_INITIALIZED":
            state.wallet_balance = Decimal(str(payload.get("initial_balance", "0")))
            state.realized_pnl_total = Decimal("0")
            return

        if et == "ORDER_CREATED":
            oid = payload.get("order_id")
            if oid:
                state.orders[oid] = {
                    "symbol": payload.get("symbol"),
                    "side": payload.get("side"),
                    "quantity": Decimal(str(payload.get("quantity", "0"))),
                    "filled_quantity": Decimal("0"),
                    "remaining_quantity": Decimal(str(payload.get("quantity", "0"))),
                    "avg_fill_price": Decimal("0"),
                    "status": payload.get("status", OrderStatus.NEW.value),
                    "order_type": payload.get("order_type", OrderType.MARKET.value),
                    "reduce_only": bool(payload.get("reduce_only", False)),
                    "trigger_price": Decimal(str(payload.get("trigger_price"))) if payload.get("trigger_price") is not None else None,
                    "limit_price": Decimal(str(payload.get("limit_price"))) if payload.get("limit_price") is not None else None,
                    "expires_at": int(payload.get("expires_at")) if payload.get("expires_at") is not None else None,
                }
            return

        if et in ("ORDER_FILLED", "ORDER_PARTIALLY_FILLED"):
            oid = payload.get("order_id")
            fill_qty = Decimal(str(payload.get("quantity", "0")))
            fee = Decimal(str(payload.get("fee", "0")))
            if oid and oid in state.orders:
                order_ref = state.orders[oid]
                order_ref["filled_quantity"] += fill_qty
                order_ref["remaining_quantity"] = max(
                    Decimal("0"),
                    order_ref["quantity"] - order_ref["filled_quantity"],
                )
                order_ref["avg_fill_price"] = Decimal(str(payload.get("fill_price", "0")))
                target_status = payload.get("status", OrderStatus.FILLED.value)
                OrderStateMachine.transition(order_ref, target_status)
            state.fills.append(payload)
            state.wallet_balance -= fee
            state.realized_pnl_total -= fee
            return

        if et == "ORDER_CANCELLED":
            oid = payload.get("order_id")
            if oid and oid in state.orders:
                OrderStateMachine.transition(state.orders[oid], OrderStatus.CANCELLED.value)
            return

        if et == "ORDER_REJECTED":
            oid = payload.get("order_id")
            if oid and oid in state.orders:
                OrderStateMachine.transition(state.orders[oid], OrderStatus.REJECTED.value)
            return
        
        if et == "ORDER_EXPIRED":
            oid = payload.get("order_id")
            if oid and oid in state.orders:
                OrderStateMachine.transition(state.orders[oid], OrderStatus.EXPIRED.value)
            return

        if et == "POSITION_OPENED":
            symbol = payload.get("symbol")
            if symbol:
                tp_levels = []
                for tp in payload.get("tp_levels", []):
                    tp_levels.append({
                        "price": Decimal(str(tp.get("price", "0"))),
                        "pct": float(tp.get("pct", 0.0)),
                        "hit": bool(tp.get("hit", False)),
                        "label": tp.get("label", ""),
                    })
                state.open_positions[symbol] = {
                    "entry_price": Decimal(str(payload.get("execution_price", payload.get("entry_price", "0")))),
                    "quantity": Decimal(str(payload.get("quantity", "0"))),
                    "margin": Decimal(str(payload.get("margin", "0"))),
                    "side": payload.get("side"),
                    "playbook": payload.get("playbook", "INTRADAY"),
                    "sl_price": Decimal(str(payload.get("sl_price", "0"))),
                    "leverage": int(payload.get("leverage", 1)),
                    "open_time": int(payload.get("open_time", 0)),
                    "tp_levels": tp_levels,
                    "trailing_active": bool(payload.get("trailing_active", False)),
                    "protective_orders": {},
                }
            return

        if et == "POSITION_PARTIALLY_CLOSED":
            symbol = payload.get("symbol")
            pnl_str = payload.get("pnl", payload.get("remaining_pnl", "0"))
            pnl = Decimal(str(pnl_str))
            fee = Decimal(str(payload.get("fee", "0")))
            state.wallet_balance += (pnl - fee)
            state.realized_pnl_total += (pnl - fee)
            if symbol in state.open_positions:
                qty_closed = Decimal(str(payload.get("closed_qty", "0")))
                state.open_positions[symbol]["quantity"] = max(
                    Decimal("0"),
                    state.open_positions[symbol]["quantity"] - qty_closed,
                )
                tp_label = payload.get("tp_label")
                if tp_label:
                    for tp in state.open_positions[symbol].get("tp_levels", []):
                        if tp["label"] == tp_label:
                            tp["hit"] = True
            return

        if et == "SL_ADJUSTED":
            symbol = payload.get("symbol")
            sl_price = Decimal(str(payload.get("sl_price", "0")))
            if symbol in state.open_positions:
                state.open_positions[symbol]["sl_price"] = sl_price
            return

        if et == "TRAILING_STOP_ACTIVATED":
            symbol = payload.get("symbol")
            if symbol in state.open_positions:
                state.open_positions[symbol]["trailing_active"] = True
            return

        if et in ("POSITION_CLOSED", "LIQUIDATION"):
            symbol = payload.get("symbol")
            pnl = Decimal(str(payload.get("remaining_pnl", "0")))
            fee = Decimal(str(payload.get("fee", "0")))
            state.wallet_balance += (pnl - fee)
            state.realized_pnl_total += (pnl - fee)
            if symbol in state.open_positions:
                del state.open_positions[symbol]
            return

        if et == "FUNDING_APPLIED":
            amt = Decimal(str(payload.get("amount", "0")))
            state.wallet_balance += amt
            state.realized_pnl_total += amt
            state.funding_total += amt
            return

        if et == "FEE_CHARGED":
            amt = Decimal(str(payload.get("amount", "0")))
            state.wallet_balance -= amt
            state.realized_pnl_total -= amt
            return

        if et == "TDS_CHARGED":
            amt = Decimal(str(payload.get("amount", "0")))
            state.wallet_balance -= amt
            state.realized_pnl_total -= amt
            return

        if et == "PROTECTIVE_ORDERS_PLACED":
            symbol = payload.get("symbol")
            if symbol in state.open_positions:
                orders = state.open_positions[symbol].setdefault("protective_orders", {})
                for key in ("sl", "tp"):
                    oid = payload.get(key)
                    if oid:
                        orders[key] = oid
            return

        if et == "PROTECTIVE_ORDERS_CANCELLED":
            symbol = payload.get("symbol")
            if symbol in state.open_positions:
                state.open_positions[symbol]["protective_orders"] = {}
            return

        if et == "INVARIANT_VIOLATION":
            state.violations.append(payload)
            return

@dataclass
class EnhancedPosition:
    symbol: str
    side: PositionSide
    playbook: Playbook
    entry_price: Decimal
    original_quantity: Decimal
    remaining_quantity: Decimal
    notional: Decimal
    margin_used: Decimal
    leverage: int
    open_time: int  # candle close_time ms, NOT wall clock

    sl_price: Decimal
    tp_levels: List[dict] = field(default_factory=list)
    trailing_active: bool = False
    trailing_stop_price: Optional[float] = None
    time_stop_hours: int = 18

    unrealized_pnl: Decimal = Decimal("0")
    partial_realized_pnl: Decimal = Decimal("0")
    status: Literal["OPEN", "CLOSED"] = "OPEN"
    close_time: Optional[int] = None
    close_price: Optional[Decimal] = None
    close_reason: Optional[str] = None
    highest_price: Optional[Decimal] = None
    peak_pnl_pct: Decimal = Decimal("0")
    # TDS deducted on the sell leg of this trade (F2).
    tds_paid: Decimal = Decimal("0")
    # Resting protective orders placed on the venue (F1): {"sl": id, "tp": id}.
    protective_orders: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.highest_price is None:
            self.highest_price = self.entry_price

    def update_pnl(self, mark_price: Decimal):
        if self.status != "OPEN" or mark_price <= Decimal("0"):
            return
        
        # Calculate dynamic notional and margin_used based on current mark_price
        self.notional = self.remaining_quantity * mark_price
        self.margin_used = self.notional / self.leverage

        if self.side == PositionSide.LONG:
            self.unrealized_pnl = (mark_price - self.entry_price) * self.remaining_quantity
            if self.highest_price is None or mark_price > self.highest_price:
                self.highest_price = mark_price
        else:
            self.unrealized_pnl = (self.entry_price - mark_price) * self.remaining_quantity
            if self.highest_price is None or mark_price < self.highest_price:
                self.highest_price = mark_price

        # Update peak PnL as percentage of entry price (undiluted price change)
        if self.entry_price > Decimal("0") and self.remaining_quantity > Decimal("0"):
            current_pnl_pct = self.unrealized_pnl / (self.entry_price * self.remaining_quantity)
            if current_pnl_pct > self.peak_pnl_pct:
                self.peak_pnl_pct = current_pnl_pct


    @property
    def total_realized_pnl(self) -> float:
        return self.partial_realized_pnl + (self.unrealized_pnl if self.status == "CLOSED" else 0)

    @property
    def current_margin_pnl_pct(self) -> float:
        if self.margin_used == Decimal("0"):
            return Decimal("0")
        return self.unrealized_pnl / self.margin_used

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "playbook": self.playbook.value,
            "entry_price": self.entry_price,
            "original_quantity": self.original_quantity,
            "remaining_quantity": self.remaining_quantity,
            "notional": self.notional,
            "margin_used": self.margin_used,
            "leverage": self.leverage,
            "open_time": self.open_time,
            "sl_price": self.sl_price,
            "tp_levels": self.tp_levels,
            "trailing_active": self.trailing_active,
            "trailing_stop_price": self.trailing_stop_price,
            "time_stop_hours": self.time_stop_hours,
            "unrealized_pnl": self.unrealized_pnl,
            "partial_realized_pnl": self.partial_realized_pnl,
            "status": self.status,
            "close_time": self.close_time,
            "close_price": self.close_price,
            "close_reason": self.close_reason,
            "highest_price": self.highest_price,
            "peak_pnl_pct": self.peak_pnl_pct,
            "tds_paid": self.tds_paid,
            "protective_orders": self.protective_orders,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EnhancedPosition":
        data["side"] = PositionSide(data["side"])
        data["playbook"] = Playbook(data["playbook"])
        for k in ["entry_price", "original_quantity", "remaining_quantity", "notional", "margin_used", "sl_price", "unrealized_pnl", "partial_realized_pnl", "close_price", "highest_price", "peak_pnl_pct", "tds_paid"]:
            if k in data and data[k] is not None:
                data[k] = Decimal(str(data[k]))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class Severity(Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    FATAL = "FATAL"


@dataclass
class InvariantRule:
    name: str
    severity: Severity
    fn: Callable[[], Tuple[bool, dict]]


class EnhancedFuturesWallet:
    """Broker-like futures wallet with position tracking and persistence."""

    def __init__(
        self,
        initial_balance: float = 1_000.0,
        leverage: int = 10,
        equity_utilization: float = 0.50,
        catastrophic_sl_pct: float = -0.50,
        symbol: Optional[str] = None,
        state_namespace: Optional[str] = None,
        maker_fee_rate: float = 0.0002,
        taker_fee_rate: float = 0.0005,
        maintenance_margin_ratio: float = 0.005,
        now_ms_fn: Optional[Callable[[], int]] = None,
        halt_on_invariant_violation: bool = False,
        event_hook: Optional[Callable[[dict], None]] = None,
        halt_on_critical: bool = False,
        halt_on_fatal: bool = True,
        max_snapshots: int = 10,
        tds_enabled: bool = False,
        tds_rate: float = 0.01,
        paper_spread_coeff: Optional[float] = None,
        paper_collar_pct: float = 0.10,
        venue_sl_enabled: bool = True,
        venue_tp_enabled: bool = False,
        require_venue_sl: bool = False,
        software_sl_backup_bps: int = 15,
        database_url: str = "",
        event_bus: Optional[Any] = None,
    ):
        # Symbol is optional; if provided, kept for backward compatibility.
        self.symbol = symbol or "GLOBAL"
        self.leverage = leverage
        self.equity_utilization = equity_utilization
        self.catastrophic_sl_pct = catastrophic_sl_pct
        self.state_namespace = state_namespace or "default"
        self.maker_fee_rate = self._to_decimal(maker_fee_rate)
        self.taker_fee_rate = self._to_decimal(taker_fee_rate)
        self.maintenance_margin_ratio = self._to_decimal(maintenance_margin_ratio)
        # TDS (F2) — 1% on the sell leg for Indian residents.
        self.tds_enabled = bool(tds_enabled)
        self.tds_rate = self._to_decimal(tds_rate)
        # Paper execution-degradation (F4).
        self.paper_spread_coeff = self._to_decimal(paper_spread_coeff) if paper_spread_coeff is not None else None
        self.paper_collar_pct = self._to_decimal(paper_collar_pct)
        # Venue-resident protective stops (F1).
        self.venue_sl_enabled = bool(venue_sl_enabled)
        self.venue_tp_enabled = bool(venue_tp_enabled)
        self.require_venue_sl = bool(require_venue_sl)
        self.software_sl_backup_bps = int(software_sl_backup_bps)
        self._now_ms_fn = now_ms_fn or (lambda: int(time.time() * 1000))
        self.halt_on_invariant_violation = halt_on_invariant_violation
        self.event_hook = event_hook
        self.event_bus = event_bus
        self.halted = False
        self.max_snapshots = max_snapshots

        # Live execution wiring (paper mode leaves these untouched)
        self.execution_engine = None
        self.live_execution = False

        # Fast bootstrap metrics
        self.replay_duration_ms: float = 0.0
        self.delta_size: int = 0
        self.recovery_latency_ms: float = 0.0

        safe_ns = self._sanitize_path_component(self.state_namespace)
        safe_symbol = self._sanitize_path_component(self.symbol)
        self.state_file = DATA_DIR / f"wallet_{safe_ns}_{safe_symbol}.json"
        self.backup_state_file = self.state_file.with_suffix(".bak.json")
        self.events_file = DATA_DIR / f"wallet_events_{safe_ns}_{safe_symbol}.jsonl"
        self.db_file = DATA_DIR / f"wallet_{safe_ns}_{safe_symbol}.db"

        from .storage.wallet_store import WalletStore
        self._wallet_store = WalletStore(
            database_url=database_url,
            sqlite_path=str(self.db_file),
            namespace=safe_ns,
            symbol=safe_symbol,
        )

        self.initial_balance: Decimal = self._to_decimal(initial_balance)
        self.wallet_balance: Decimal = self.initial_balance
        self.unrealized_pnl_total: Decimal = Decimal("0")
        self.realized_pnl_total: Decimal = Decimal("0")
        self.positions: Dict[str, EnhancedPosition] = {}
        self.position_history: List[dict] = []
        self.orders: Dict[str, Order] = {}
        self.fills: List[Fill] = []
        self.reducer = PortfolioReducer()
        self.lock = threading.RLock()
        self._init_db()

        self._load_state()
        if not hasattr(self, "_runtime_reducer_state"):
            self._runtime_reducer_state = PortfolioState(
                wallet_balance=self.wallet_balance,
                realized_pnl_total=self.realized_pnl_total,
            )

        self.halt_on_critical = halt_on_critical or halt_on_invariant_violation
        self.halt_on_fatal = halt_on_fatal
        self.invariant_rules: List[InvariantRule] = []
        self._register_default_invariants()

        events_empty = True
        if self.events_file.exists() and self.events_file.stat().st_size > 0:
            events_empty = False
        else:
            try:
                count = self._wallet_store.count_events()
                if count > 0:
                    events_empty = False
            except Exception:
                pass

        if events_empty:
            self._emit_event(
                "WALLET_INITIALIZED",
                {"initial_balance": str(self.wallet_balance)}
            )

    @property
    def margin_balance(self) -> float:
        with self.lock:
            return self.wallet_balance + self.unrealized_pnl_total

    @property
    def available_balance(self) -> float:
        with self.lock:
            used = sum(p.margin_used for p in self.positions.values() if p.status == "OPEN")
            return max(Decimal("0"), self.margin_balance - used)

    def get_open_position(self, symbol: Optional[str] = None) -> Optional[EnhancedPosition]:
        with self.lock:
            target = symbol or self.symbol
            pos = self.positions.get(target)
            return pos if pos and pos.status == "OPEN" else None

    def can_open(self, symbol: Optional[str] = None) -> Tuple[bool, str]:
        with self.lock:
            if self.halted:
                return False, "Wallet is halted due to invariant violation"
            target = symbol or self.symbol
            if self.get_open_position(target):
                return False, f"Already have open position on {target}"
            if self.available_balance <= 0:
                return False, "No available balance"
            return True, "OK"

    def attach_execution_engine(self, engine, live: bool = True):
        """Wire a live execution venue. When ``live`` is True, the orchestrator
        is expected to pass venue fills into open/close via ``external_fill``."""
        self.execution_engine = engine
        self.live_execution = bool(live)
        logger.info("Execution engine attached (live=%s)", self.live_execution)

    def apply_external_fill(
        self,
        symbol: str,
        side: "PositionSide",
        fill_price,
        fill_quantity,
        order_id: str = "",
        client_order_id: str = "",
        reduce_only: bool = False,
        setup: Optional[dict] = None,
    ):
        """Book a venue-reported fill into the event log (live mode).

        Entries (``reduce_only=False``) require ``setup`` (sl/tp/playbook); exits
        close the existing position at the venue price. This is the bridge the
        order manager / reconciler use to record real fills without invoking the
        synchronous paper simulation.
        """
        external = {"price": self._to_decimal(fill_price), "fee": self._to_decimal(0), "order_id": order_id}
        if reduce_only:
            return self.close_position(symbol, float(fill_price), reason="VENUE_EXIT", external_fill=external)
        if setup is None:
            raise ValueError("apply_external_fill entry requires a setup dict")
        return self.open_position(
            symbol, setup, float(fill_price),
            custom_quantity=float(fill_quantity),
            external_fill=external,
        )

    def _venue_fill(self, symbol: str, order_side: "PositionSide", quantity: Decimal, reduce_only: bool, intent: str) -> dict:
        """Place a market order on the live venue and return the realized fill.

        Used by open/close/partial when ``live_execution`` is on and the caller
        did not supply an ``external_fill`` — turning the wallet into the single
        live order-submission site without changing the orchestrator.
        """
        from .execution.client_order_id import make_client_order_id  # lazy: avoid import cycle
        coid = make_client_order_id(symbol, intent, nonce=str(self._now_ms()))
        order = self.execution_engine.place_order(
            symbol, order_side, quantity, OrderType.MARKET,
            reduce_only=reduce_only, client_order_id=coid,
        )
        price = order.avg_fill_price if order.avg_fill_price and order.avg_fill_price > 0 else Decimal("0")
        if price <= Decimal("0"):
            # Booking at price 0 would corrupt PnL/SL/TP/liquidation. The engine
            # confirms market fills, so a zero here is a hard failure to surface.
            raise ValueError(
                f"venue fill for {symbol} ({intent}) returned no usable price "
                f"(order_id={order.id}); refusing to book a zero-price fill"
            )
        fee = self._calculate_fee(price * quantity, is_taker=True)
        return {"price": price, "fee": fee, "order_id": order.id, "client_order_id": coid}

    def acquire_live_entry_fill(
        self,
        symbol: str,
        side: "PositionSide",
        quantity: Decimal,
        limit_price: Decimal,
        *,
        timeout_s: float = 8.0,
        offset_bps: float = 1.0,
        fallback: str = "market",
        poll_interval_s: float = 0.5,
    ) -> Optional[dict]:
        """Acquire a maker-limit entry fill on the live venue, LOCK-FREE.

        Posts a passive LIMIT just inside the spread (a buy below / sell above
        the signal price) so it rests as a maker order, then polls for the fill
        up to ``timeout_s``. On timeout it cancels and, per ``fallback``, either
        crosses with a MARKET order or skips the entry entirely.

        Returns an ``external_fill`` dict for ``open_position`` (may carry a
        reduced ``filled_quantity`` on a partial maker fill), or ``None`` to skip.

        IMPORTANT: this MUST be called WITHOUT holding ``self.lock`` — it blocks
        for up to ``timeout_s`` and the lock is shared across symbols.
        """
        import time as _t
        from .execution.client_order_id import make_client_order_id

        if not (self.live_execution and self.execution_engine is not None):
            return None

        qty = self._to_decimal(quantity)
        limit_px = self._to_decimal(limit_price)
        off = self._to_decimal(offset_bps) / Decimal("10000")
        if side == PositionSide.LONG:
            post_px = limit_px * (Decimal("1") - off)
        else:
            post_px = limit_px * (Decimal("1") + off)

        coid = make_client_order_id(symbol, "entry", nonce=str(self._now_ms()))
        try:
            order = self.execution_engine.place_order(
                symbol, side, qty, OrderType.LIMIT, limit_price=post_px, client_order_id=coid,
            )
        except Exception as e:
            logger.warning("[MAKER-LIMIT] place failed for %s: %s", symbol, e)
            return self._maker_limit_fallback(symbol, side, qty, fallback)

        oid = str(order.id or "")
        deadline = _t.time() + max(timeout_s, 0.0)
        filled = Decimal("0")
        while _t.time() < deadline:
            _t.sleep(poll_interval_s)
            try:
                st = self.execution_engine.get_order_status(oid, symbol)
            except Exception:
                continue
            filled = self._to_decimal(st.get("filled_quantity", 0) or 0)
            status = str(st.get("status", "")).lower()
            if status in ("filled",) or filled >= qty:
                fee = self._calculate_fee(post_px * qty, is_taker=False)
                return {"price": post_px, "fee": fee, "order_id": oid,
                        "client_order_id": coid, "is_maker": True}
            if status in ("cancelled", "canceled", "rejected"):
                break

        # Timed out (or terminal-without-fill): cancel the resting order, then
        # re-read to settle any race, and decide based on what actually filled.
        try:
            self.execution_engine.cancel_order(oid)
        except Exception:
            pass
        try:
            st = self.execution_engine.get_order_status(oid, symbol)
            filled = self._to_decimal(st.get("filled_quantity", 0) or 0)
        except Exception:
            pass

        if filled >= qty:
            fee = self._calculate_fee(post_px * qty, is_taker=False)
            return {"price": post_px, "fee": fee, "order_id": oid,
                    "client_order_id": coid, "is_maker": True}
        if filled > Decimal("0"):
            # Partial maker fill: book only what filled (engine reads filled_quantity).
            fee = self._calculate_fee(post_px * filled, is_taker=False)
            logger.info("[MAKER-LIMIT] %s partial maker fill %s/%s", symbol, filled, qty)
            return {"price": post_px, "fee": fee, "order_id": oid,
                    "client_order_id": coid, "is_maker": True, "filled_quantity": filled}
        logger.info("[MAKER-LIMIT] %s unfilled within %.1fs — fallback=%s", symbol, timeout_s, fallback)
        return self._maker_limit_fallback(symbol, side, qty, fallback)

    def _maker_limit_fallback(self, symbol, side, qty, fallback) -> Optional[dict]:
        if str(fallback).lower() == "market":
            return self._venue_fill(symbol, side, qty, reduce_only=False, intent="entry")
        return None

    def _place_protective_orders(self, pos: EnhancedPosition) -> dict:
        """Place resting SL (and optionally TP) on the venue (F1).

        Opposite side, full remaining qty. CoinDCX nets the one-way position, so
        an opposite-side stop acts as a reduce-only protective order. A placement
        failure never unwinds the (already open) entry; if ``require_venue_sl`` is
        set, an SL failure trips HALT — a naked position is the risk we eliminate.
        """
        if not (self.live_execution and self.execution_engine is not None and self.venue_sl_enabled):
            return {}
        exit_side = PositionSide.SHORT if pos.side == PositionSide.LONG else PositionSide.LONG
        placed: dict = {}
        try:
            sl_order = self.execution_engine.place_order(
                pos.symbol, exit_side, pos.remaining_quantity, OrderType.STOP_MARKET,
                trigger_price=pos.sl_price, reduce_only=True,
                leverage=pos.leverage,
            )
            if sl_order and sl_order.id:
                placed["sl"] = sl_order.id
        except Exception as e:
            logger.critical("[PROTECTIVE SL] venue stop placement failed for %s: %s", pos.symbol, e)
            if self.require_venue_sl:
                from . import safe_mode
                safe_mode.trip_halt(f"venue SL placement failed for {pos.symbol}: {e}")
                try:
                    logger.critical("[PROTECTIVE SL] require_venue_sl=True: emergency flattening position for %s to protect capital", pos.symbol)
                    self.close_position(pos.symbol, float(pos.entry_price), reason="REQUIRED_SL_FAILED")
                except Exception as close_err:
                    logger.error("[PROTECTIVE SL] emergency close failed for %s: %s", pos.symbol, close_err)
                raise RuntimeError(f"Required venue Stop Loss placement failed: {e}") from e
        if self.venue_tp_enabled and pos.tp_levels:
            try:
                tp_price = self._to_decimal(pos.tp_levels[-1]["price"])
                tp_order = self.execution_engine.place_order(
                    pos.symbol, exit_side, pos.remaining_quantity, OrderType.TAKE_PROFIT,
                    trigger_price=tp_price, reduce_only=True,
                    leverage=pos.leverage,
                )
                if tp_order and tp_order.id:
                    placed["tp"] = tp_order.id
            except Exception as e:
                logger.warning("[PROTECTIVE TP] venue TP placement failed for %s: %s", pos.symbol, e)
        if placed:
            pos.protective_orders.update(placed)
            self._emit_event("PROTECTIVE_ORDERS_PLACED", {"symbol": pos.symbol, **placed})
        return placed

    def _cancel_protective_orders(self, pos: EnhancedPosition) -> None:
        """Cancel any resting venue protective orders for a position (F1)."""
        if not (self.live_execution and self.execution_engine is not None):
            return
        ids = dict(pos.protective_orders)
        if not ids:
            return
        for oid in ids.values():
            if not oid:
                continue
            try:
                self.execution_engine.cancel_order(oid)
            except Exception as e:
                logger.warning("[PROTECTIVE CANCEL] failed to cancel %s: %s", oid, e)
        pos.protective_orders = {}
        self._emit_event("PROTECTIVE_ORDERS_CANCELLED", {"symbol": pos.symbol})

    def _software_sl_level(self, pos: EnhancedPosition) -> Decimal:
        """SL price the software monitor should act on (F1).

        When a venue SL rests, the software net is a backup: it only fires
        ``software_sl_backup_bps`` beyond the real SL so the venue stop is primary
        and the two paths don't double-exit on a normal SL touch.
        """
        sl = self._to_decimal(pos.sl_price)
        if self.live_execution and pos.protective_orders.get("sl"):
            buf = self._to_decimal(self.software_sl_backup_bps) / Decimal("10000")
            if pos.side == PositionSide.LONG:
                return sl * (Decimal("1") - buf)
            return sl * (Decimal("1") + buf)
        return sl

    def open_position(
        self,
        symbol: str,
        setup: dict,
        mark_price: float,
        custom_margin: Optional[float] = None,
        custom_quantity: Optional[float] = None,
        external_fill: Optional[dict] = None,
    ) -> Optional[EnhancedPosition]:
        """Open a position. custom_margin overrides equity_utilization for LLM-adjusted sizing.

        ``external_fill`` (live mode) injects a venue-reported fill
        ``{"price", "fee", "order_id"}`` so the position is booked at the real
        CoinDCX fill instead of the simulated price. When ``None`` (paper mode)
        the original synchronous simulation path runs unchanged.
        """
        with self.lock:
            can, reason = self.can_open(symbol)
            if not can:
                logger.info(f"[OPEN BLOCKED] {symbol}: {reason}")
                return None

            margin = (self._to_decimal(custom_margin) if custom_margin is not None else self.available_balance * self._to_decimal(self.equity_utilization))
            if margin <= Decimal("0"):
                return None

            entry = self._to_decimal(setup["entry_price"])
            if custom_quantity is not None and self._to_decimal(custom_quantity) > Decimal("0"):
                qty = self._to_decimal(custom_quantity)
                notional = qty * entry
                margin = notional / self.leverage
                if margin > self.available_balance:
                    logger.info(f"[OPEN BLOCKED] {symbol}: risk-sized margin exceeds available balance")
                    return None
            else:
                notional = margin * self.leverage
                qty = notional / entry

            side = setup["side"]
            playbook = setup["playbook"]
            mark_price = self._to_decimal(mark_price)

            sl_price = self._to_decimal(setup["sl_price"])
            tp_levels = setup.get("tp_levels", [])
            if not tp_levels and "tp_price" in setup:
                tp_levels = [{"price": self._to_decimal(setup["tp_price"]), "pct": 1.0, "hit": False, "label": "TP"}]

            entry_style = str(setup.get("entry_order_style", "market")).lower()

            if external_fill is None and self.live_execution and self.execution_engine is not None:
                external_fill = self._venue_fill(symbol, side, qty, reduce_only=False, intent="entry")

            order = self._create_order(symbol=symbol, side=side, quantity=qty)
            if external_fill is not None:
                execution_price = self._to_decimal(external_fill["price"])
                fee_open = self._to_decimal(external_fill.get("fee", 0))
                self._record_fill(order, qty, execution_price, fee_open, sequence=1)
            elif entry_style == "maker_limit":
                # Paper passive maker fill: filled at the intended (signal) price
                # with the maker fee and no spread/collar penalty — models a
                # resting LIMIT that gets hit rather than crossing the spread.
                execution_price = entry
                fee_open = self._calculate_fee(execution_price * qty, is_taker=False)
                self._record_fill(order, qty, execution_price, fee_open, sequence=1)
            else:
                execution_price = self._execution_price(mark_price, side, is_entry=True, setup=setup)
                # F4: CoinDCX 10% market-order collar — reject paper fills that
                # deviate too far from the intended price (replicates the venue).
                if self.paper_collar_pct > 0 and entry > Decimal("0"):
                    deviation = abs(execution_price - entry) / entry
                    if deviation > self.paper_collar_pct:
                        logger.warning(
                            f"[PAPER COLLAR] {symbol} fill {execution_price:.4f} deviates "
                            f"{deviation:.1%} from intended {entry:.4f} (> {self.paper_collar_pct:.0%}); REJECTED"
                        )
                        self._emit_event("ORDER_REJECTED", {"order_id": order.id, "symbol": symbol, "reason": "PAPER_COLLAR"})
                        return None
                fill_chunks = int(setup.get("fill_chunks", 1))
                fee_open = self._execute_order_in_chunks(
                    order=order,
                    total_quantity=qty,
                    fill_price=execution_price,
                    fee_rate=self.taker_fee_rate,
                    chunk_count=fill_chunks,
                )

            pos = EnhancedPosition(
                symbol=symbol,
                side=side,
                playbook=playbook,
                entry_price=execution_price,
                original_quantity=qty,
                remaining_quantity=qty,
                notional=notional,
                margin_used=margin,
                leverage=self.leverage,
                open_time=setup.get("candle_close_time", self._now_ms()),
                sl_price=sl_price,
                tp_levels=tp_levels,
                time_stop_hours=setup.get("time_stop_hours", 18),
            )
            pos.update_pnl(mark_price)
            self._emit_event("FEE_CHARGED", {"amount": fee_open, "reason": "OPEN_FEE", "symbol": symbol})
            # F2: TDS on the SELL leg — for a SHORT, the sell happens at open.
            tds_open = self._calculate_tds(side == PositionSide.SHORT, execution_price * qty)
            if tds_open > Decimal("0"):
                pos.tds_paid += tds_open
                self._emit_event("TDS_CHARGED", {"amount": tds_open, "reason": "OPEN_TDS", "symbol": symbol})
            self.positions[symbol] = pos
            self._sync_unrealized_total()

            logger.info(
                f"[POSITION OPENED] {symbol} {side.value} | Playbook={playbook.value} | "
                f"Qty={qty:.4f} @ {entry:.2f} | Margin={margin:.2f} | "
                f"SL={sl_price:.2f} | TP={tp_levels}"
            )
            self._emit_event(
                "POSITION_OPENED",
                {
                    "symbol": symbol,
                    "side": side.value,
                    "entry_price": entry,
                    "quantity": qty,
                    "margin": margin,
                    "execution_price": execution_price,
                    "fee": fee_open,
                    "playbook": playbook.value,
                    "sl_price": sl_price,
                    "leverage": self.leverage,
                    "open_time": pos.open_time,
                    "tp_levels": tp_levels,
                },
            )
            # F1: rest a protective SL (+optional TP) on the venue (live only).
            self._place_protective_orders(pos)
            self._save_state()
            return pos

    def adopt_venue_position(
        self,
        symbol: str,
        side: "PositionSide",
        quantity: float,
        entry_price: float,
        *,
        leverage: Optional[int] = None,
        sl_pct: float = 0.02,
        mark_price: Optional[float] = None,
    ) -> Optional["EnhancedPosition"]:
        """Adopt a live venue position the internal wallet doesn't know about.

        Synthesizes an internal OPEN position from the venue's qty/side/entry,
        emits a POSITION_ADOPTED event (so it persists + projects), and rests a
        protective stop. Used by reconciliation recovery when the venue is the
        source of truth (e.g. a position opened in a prior run / different store).

        Returns the adopted position, or None if one already exists for *symbol*.
        """
        with self.lock:
            if self.get_open_position(symbol) is not None:
                logger.warning("[ADOPT] %s already has an internal position; skipping", symbol)
                return None

            qty = self._to_decimal(quantity)
            entry = self._to_decimal(entry_price)
            if qty <= Decimal("0") or entry <= Decimal("0"):
                logger.error("[ADOPT] %s invalid qty/entry (%s/%s); refusing", symbol, qty, entry)
                return None

            lev = int(leverage or self.leverage)
            notional = qty * entry
            margin = notional / lev
            # Protective stop on the adverse side of entry.
            if side == PositionSide.LONG:
                sl_price = entry * (Decimal("1") - self._to_decimal(sl_pct))
            else:
                sl_price = entry * (Decimal("1") + self._to_decimal(sl_pct))

            pos = EnhancedPosition(
                symbol=symbol,
                side=side,
                playbook=Playbook.INTRADAY,
                entry_price=entry,
                original_quantity=qty,
                remaining_quantity=qty,
                notional=notional,
                margin_used=margin,
                leverage=lev,
                open_time=self._now_ms(),
                sl_price=sl_price,
                tp_levels=[],
            )
            if mark_price is not None and self._to_decimal(mark_price) > Decimal("0"):
                pos.update_pnl(self._to_decimal(mark_price))
            self.positions[symbol] = pos
            self._sync_unrealized_total()

            logger.critical(
                "[ADOPT] %s %s qty=%.6f @ %.6f lev=%dx SL=%.6f — adopted from venue",
                symbol, side.value, float(qty), float(entry), lev, float(sl_price),
            )
            self._emit_event(
                "POSITION_ADOPTED",
                {
                    "symbol": symbol,
                    "side": side.value,
                    "entry_price": entry,
                    "quantity": qty,
                    "margin": margin,
                    "execution_price": entry,
                    "fee": Decimal("0"),  # adoption books no entry fee (already paid on venue)
                    "playbook": Playbook.INTRADAY.value,
                    "sl_price": sl_price,
                    "leverage": lev,
                    "open_time": pos.open_time,
                    "tp_levels": [],
                    "adopted": True,
                },
            )
            # Rest a protective SL on the venue (live only) so the adopted
            # position is never left naked.
            self._place_protective_orders(pos)
            self._save_state()
            return pos

    def partial_close(self, symbol: str, mark_price: float, pct: float, reason: str, tp_label: Optional[str] = None, external_fill: Optional[dict] = None) -> Decimal:
        """Close a percentage of the position. Returns realized PnL from this slice.

        ``external_fill`` (live mode) supplies the venue exit price via ``{"price": ...}``.
        """
        with self.lock:
            mark_price = self._to_decimal(mark_price)
            pos = self.get_open_position(symbol)
            if not pos:
                return Decimal("0")

            # F1: the resting venue stop is sized to the full position; drop it
            # before partially netting so it can't over-sell, then re-place below.
            had_protective = bool(pos.protective_orders)
            self._cancel_protective_orders(pos)

            qty_to_close = pos.remaining_quantity * self._to_decimal(pct)
            if external_fill is None and self.live_execution and self.execution_engine is not None and qty_to_close > Decimal("0"):
                exit_side = PositionSide.SHORT if pos.side == PositionSide.LONG else PositionSide.LONG
                external_fill = self._venue_fill(symbol, exit_side, qty_to_close, reduce_only=True, intent="exit-partial")

            if external_fill is not None:
                execution_price = self._to_decimal(external_fill["price"])
            else:
                execution_price = self._execution_price(mark_price, pos.side, is_entry=False)
            if qty_to_close <= Decimal("0"):
                return Decimal("0")

            if pos.side == PositionSide.LONG:
                pnl_slice = (execution_price - pos.entry_price) * qty_to_close
            else:
                pnl_slice = (pos.entry_price - execution_price) * qty_to_close

            fee_close = self._calculate_fee(execution_price * qty_to_close, is_taker=True)

            # Credit the slice immediately (net fees)
            self._emit_event(
                "POSITION_PARTIALLY_CLOSED",
                {
                    "symbol": symbol,
                    "reason": reason,
                    "pct": pct,
                    "price": execution_price,
                    "pnl": pnl_slice,
                    "fee": fee_close,
                    "closed_qty": qty_to_close,
                    "tp_label": tp_label,
                },
            )
            pos.partial_realized_pnl += (pnl_slice - fee_close)

            # F2: TDS on the SELL leg — for a LONG, each partial close is a sell.
            tds_slice = self._calculate_tds(pos.side == PositionSide.LONG, execution_price * qty_to_close)
            if tds_slice > Decimal("0"):
                pos.tds_paid += tds_slice
                self._emit_event("TDS_CHARGED", {"amount": tds_slice, "reason": "PARTIAL_TDS", "symbol": symbol})

            logger.info(
                f"[PARTIAL CLOSE] {symbol} {pos.side.value} | Closed {pct*100:.0f}% | "
                f"Qty={qty_to_close:.4f} | PnL={pnl_slice:.2f} | Reason={reason}"
            )

            if pos.remaining_quantity <= Decimal("0.0001"):
                return self.close_position(symbol, mark_price, reason="FULL_VIA_PARTIALS")

            # F1: restore a resting stop sized to the reduced position. The
            # software SL backup (_software_sl_level, checked every 1s) covers the
            # brief cancel→replace window. If a venue SL is mandatory but the
            # re-place yields no stop (even without an exception), HALT — a live
            # position must never be left without its required protective stop.
            if had_protective:
                self._place_protective_orders(pos)
                if (self.live_execution and self.execution_engine is not None
                        and self.venue_sl_enabled and self.require_venue_sl
                        and not pos.protective_orders.get("sl")):
                    from . import safe_mode
                    safe_mode.trip_halt(
                        f"venue SL re-placement after partial close yielded no stop for {symbol}")

            self._save_state()
            return pnl_slice

    def close_position(self, symbol: str, mark_price: float, reason: str, external_fill: Optional[dict] = None) -> Optional[EnhancedPosition]:
        """Close full position. Only credits remaining unrealized (partials already credited).

        ``external_fill`` (live mode) supplies the venue exit price via
        ``{"price": ...}``; when ``None`` the simulated exit price is used.
        """
        with self.lock:
            mark_price = self._to_decimal(mark_price)
            pos = self.get_open_position(symbol)
            if not pos:
                return None

            # F1: drop any resting venue protective orders before the exit fill so
            # they can't fire against a flat position after we net out.
            self._cancel_protective_orders(pos)

            if external_fill is None and self.live_execution and self.execution_engine is not None:
                exit_side = PositionSide.SHORT if pos.side == PositionSide.LONG else PositionSide.LONG
                external_fill = self._venue_fill(symbol, exit_side, pos.remaining_quantity, reduce_only=True, intent="exit")

            if external_fill is not None:
                execution_price = self._to_decimal(external_fill["price"])
            else:
                execution_price = self._execution_price(mark_price, pos.side, is_entry=False)
            pos.update_pnl(execution_price)
            remaining_pnl = pos.unrealized_pnl  # Only the still-open portion
            fee_close = self._calculate_fee(execution_price * pos.remaining_quantity, is_taker=True)

            # Credit remaining PnL
            self._emit_event(
                "POSITION_CLOSED",
                {
                    "symbol": symbol,
                    "side": pos.side.value,
                    "close_price": execution_price,
                    "fee": fee_close,
                    "reason": reason,
                    "remaining_pnl": remaining_pnl,
                },
            )

            # F2: TDS on the SELL leg — for a LONG, the sell happens at close.
            tds_close = self._calculate_tds(pos.side == PositionSide.LONG, execution_price * pos.remaining_quantity)
            if tds_close > Decimal("0"):
                pos.tds_paid += tds_close
                self._emit_event("TDS_CHARGED", {"amount": tds_close, "reason": "CLOSE_TDS", "symbol": symbol})

            # Credit the still-open leg into realized PnL before flattening so
            # total_realized_pnl reflects the full trade once unrealized is zeroed.
            pos.partial_realized_pnl += remaining_pnl
            pos.unrealized_pnl = Decimal("0")
            pos.status = "CLOSED"
            pos.close_time = self._now_ms()
            pos.close_price = execution_price
            pos.close_reason = reason
            self.position_history.append(pos.to_dict())

            logger.info(
                f"[POSITION CLOSED] {symbol} {pos.side.value} | "
                f"Close={execution_price:.2f} | Remaining PnL={remaining_pnl:.2f} | "
                f"Total Trade PnL={pos.partial_realized_pnl:.2f} | Reason={reason}"
            )
            self._save_state()
            if hasattr(self, "event_bus") and self.event_bus is not None:
                try:
                    from .events import TradeClosedEvent
                    pnl_pct = (float(pos.partial_realized_pnl) / float(pos.margin_used) * 100) if float(pos.margin_used) > 0 else 0
                    duration = (time.time() - pos.open_time / 1000) / 60 if getattr(pos, 'open_time', 0) > 0 else 0
                    self.event_bus.publish(TradeClosedEvent(
                        symbol=symbol,
                        side=pos.side.value,
                        realized_pnl=float(pos.partial_realized_pnl),
                        pnl_percent=pnl_pct,
                        exit_price=float(execution_price),
                        reason=reason,
                        duration_minutes=duration,
                        fees=float(pos.fees_paid),
                        tds=float(pos.tds_paid),
                        wallet_balance=float(self.wallet_balance),
                    ))
                except Exception as ex:
                    logger.error("Failed to publish TradeClosedEvent from wallet: %s", ex)
            return pos

    def _liquidate_position(self, symbol: str, mark_price: Decimal) -> None:
        """Force-close at maintenance boundary with no additional fee charge."""
        pos = self.get_open_position(symbol)
        if not pos:
            return
        self._cancel_protective_orders(pos)
        pos.update_pnl(mark_price)
        self._emit_event(
            "LIQUIDATION",
            {
                "symbol": symbol,
                "side": pos.side.value,
                "close_price": mark_price,
                "fee": Decimal("0"),
                "reason": "LIQUIDATION",
                "remaining_pnl": pos.unrealized_pnl,
            },
        )
        pos.unrealized_pnl = Decimal("0")
        pos.status = "CLOSED"
        pos.close_time = self._now_ms()
        pos.close_price = mark_price
        pos.close_reason = "LIQUIDATION"
        self.position_history.append(pos.to_dict())
        logger.warning(
            f"[LIQUIDATION] {symbol} {pos.side.value} | Price={mark_price:.2f} | "
            f"Margin Lost={pos.margin_used:.2f}"
        )
        self._save_state()
        if hasattr(self, "event_bus") and self.event_bus is not None:
            try:
                from .events import TradeClosedEvent
                pnl_pct = (float(pos.partial_realized_pnl) / float(pos.margin_used) * 100) if float(pos.margin_used) > 0 else 0
                duration = (time.time() - pos.open_time / 1000) / 60 if getattr(pos, 'open_time', 0) > 0 else 0
                self.event_bus.publish(TradeClosedEvent(
                    symbol=symbol,
                    side=pos.side.value,
                    realized_pnl=float(pos.partial_realized_pnl),
                    pnl_percent=pnl_pct,
                    exit_price=float(mark_price),
                    reason="LIQUIDATION",
                    duration_minutes=duration,
                    fees=float(pos.fees_paid),
                    tds=float(pos.tds_paid),
                    wallet_balance=float(self.wallet_balance),
                ))
            except Exception as ex:
                logger.error("Failed to publish TradeClosedEvent from wallet (liquidation): %s", ex)

    def adjust_sl_price(self, symbol: str, new_sl_price: Decimal):
        """Adjust Stop Loss price for symbol position via events."""
        with self.lock:
            self._emit_event("SL_ADJUSTED", {"symbol": symbol, "sl_price": self._to_decimal(new_sl_price)})
            self._save_state()

    def activate_trailing_stop(self, symbol: str):
        """Activate Trailing Stop for symbol position via events."""
        with self.lock:
            self._emit_event("TRAILING_STOP_ACTIVATED", {"symbol": symbol})
            self._save_state()

    def update_positions(
        self,
        symbol: str,
        mark_price: float,
        candle_close_time: int,
        ema9_1h: Optional[float] = None,
        current_llm_advice: Optional[dict] = None,
        current_regime: Optional[str] = None,
    ):
        """Update PnL and check all exit conditions."""
        with self.lock:
            mark_price = self._to_decimal(mark_price)
            pos = self.get_open_position(symbol)
            if not pos:
                self._sync_unrealized_total()
                return

            pos.update_pnl(mark_price)
            pnl = pos.unrealized_pnl

            # 1. Catastrophic SL (-50% margin)
            cat_sl = pos.margin_used * self._to_decimal(self.catastrophic_sl_pct)
            if self._is_liquidation_required(pos, mark_price):
                # mark_price is already Decimal here; _liquidate_position is
                # Decimal-typed — don't round-trip through float in the hot path.
                self._liquidate_position(symbol, self._to_decimal(mark_price))
                return
            if pnl <= cat_sl:
                self.close_position(symbol, float(mark_price), reason=f"CATASTROPHIC_SL ({pnl:.2f})")
                return

            # 2. Windfall Return on Margin Exit (ROM >= 40%)
            if pos.current_margin_pnl_pct >= Decimal("0.40"):
                self.close_position(symbol, float(mark_price), reason="WINDFALL_RO_MARGIN_EXIT")
                return

            # 3. Peak PnL Trailing Stop (Triggered at >= 2% undiluted price change, exit if drops by 25% of peak)
            if pos.peak_pnl_pct >= Decimal("0.02"):
                if pos.side == PositionSide.LONG:
                    peak_pnl = (pos.highest_price - pos.entry_price) * pos.remaining_quantity
                else:
                    peak_pnl = (pos.entry_price - pos.highest_price) * pos.remaining_quantity
                
                if pos.unrealized_pnl <= peak_pnl * Decimal("0.75"):
                    self.close_position(symbol, float(mark_price), reason="PEAK_TRADING_STOP")
                    return

            # 4. AI/Regime Reversal Exit
            if current_llm_advice:
                bias = current_llm_advice.get("bias") if isinstance(current_llm_advice, dict) else (current_llm_advice.bias.value if hasattr(current_llm_advice.bias, "value") else current_llm_advice.bias)
                confidence = current_llm_advice.get("confidence", 0.0) if isinstance(current_llm_advice, dict) else getattr(current_llm_advice, "confidence", 0.0)
                
                is_reversal = False
                if pos.side == PositionSide.LONG and bias == "bearish":
                    is_reversal = True
                elif pos.side == PositionSide.SHORT and bias == "bullish":
                    is_reversal = True
                    
                if is_reversal and confidence >= 0.70:
                    if pos.side == PositionSide.LONG and mark_price >= pos.entry_price * Decimal("0.995"):
                        self.close_position(symbol, float(mark_price), reason="AI_REVERSAL_EXIT")
                        return
                    elif pos.side == PositionSide.SHORT and mark_price <= pos.entry_price * Decimal("1.005"):
                        self.close_position(symbol, float(mark_price), reason="AI_REVERSAL_EXIT")
                        return

            if current_regime:
                is_regime_reversal = False
                if pos.side == PositionSide.LONG and current_regime == "TRENDING_DOWN":
                    is_regime_reversal = True
                elif pos.side == PositionSide.SHORT and current_regime == "TRENDING_UP":
                    is_regime_reversal = True
                    
                if is_regime_reversal:
                    if pos.side == PositionSide.LONG and mark_price >= pos.entry_price * Decimal("0.995"):
                        self.close_position(symbol, float(mark_price), reason="REGIME_REVERSAL_EXIT")
                        return
                    elif pos.side == PositionSide.SHORT and mark_price <= pos.entry_price * Decimal("1.005"):
                        self.close_position(symbol, float(mark_price), reason="REGIME_REVERSAL_EXIT")
                        return

            # 5. Playbook-specific exits
            if pos.playbook == Playbook.INTRADAY:
                self._check_intraday_exits(symbol, pos, mark_price, candle_close_time)
            elif pos.playbook == Playbook.SWING:
                self._check_swing_exits(symbol, pos, mark_price, candle_close_time, ema9_1h)

            self._sync_unrealized_total()

    def _check_intraday_exits(self, symbol: str, pos: EnhancedPosition, mark_price: float, candle_close_time: int):
        # TP check (guarded: positions may have empty tp_levels after reducer sync)
        if pos.tp_levels:
            if pos.side == PositionSide.LONG and mark_price >= pos.tp_levels[0]["price"]:
                self.close_position(symbol, mark_price, reason=f"TP_HIT ({pos.unrealized_pnl:.2f})")
                return
            elif pos.side == PositionSide.SHORT and mark_price <= pos.tp_levels[0]["price"]:
                self.close_position(symbol, mark_price, reason=f"TP_HIT ({pos.unrealized_pnl:.2f})")
                return

        # SL check (backup-only level when a venue stop rests; F1)
        sl_level = self._software_sl_level(pos)
        if pos.side == PositionSide.LONG and mark_price <= sl_level:
            self.close_position(symbol, mark_price, reason=f"SL_HIT ({pos.unrealized_pnl:.2f})")
            return
        elif pos.side == PositionSide.SHORT and mark_price >= sl_level:
            self.close_position(symbol, mark_price, reason=f"SL_HIT ({pos.unrealized_pnl:.2f})")
            return

        # Time stop (using candle time, not wall clock)
        hours_open = (candle_close_time - pos.open_time) / 3_600_000
        if hours_open >= pos.time_stop_hours:
            self.close_position(symbol, mark_price, reason=f"TIME_STOP ({hours_open:.1f}h)")

    def _check_swing_exits(self, symbol: str, pos: EnhancedPosition, mark_price: float, candle_close_time: int, ema9_1h: Optional[float]):
        # Scaled exits
        for tp in pos.tp_levels:
            if tp["hit"]:
                continue
            hit = False
            if pos.side == PositionSide.LONG and mark_price >= tp["price"]:
                hit = True
            elif pos.side == PositionSide.SHORT and mark_price <= tp["price"]:
                hit = True

            if hit:
                self.partial_close(symbol, mark_price, tp["pct"], reason=f"{tp['label']} HIT", tp_label=tp["label"])
                if tp["label"] == "TP1":
                    self.adjust_sl_price(symbol, pos.entry_price)
                elif tp["label"] == "TP2":
                    self.activate_trailing_stop(symbol)
                return  # Only one TP per tick

        # SL check (backup-only level when a venue stop rests; F1)
        sl_level = self._software_sl_level(pos)
        if pos.side == PositionSide.LONG and mark_price <= sl_level:
            self.close_position(symbol, mark_price, reason=f"SL_HIT ({pos.unrealized_pnl:.2f})")
            return
        elif pos.side == PositionSide.SHORT and mark_price >= sl_level:
            self.close_position(symbol, mark_price, reason=f"SL_HIT ({pos.unrealized_pnl:.2f})")
            return

        # Trailing stop
        if pos.trailing_active and ema9_1h is not None:
            if pos.side == PositionSide.LONG and mark_price < ema9_1h:
                self.close_position(symbol, mark_price, reason=f"TRAIL_STOP (EMA9 {ema9_1h:.2f})")
                return
            elif pos.side == PositionSide.SHORT and mark_price > ema9_1h:
                self.close_position(symbol, mark_price, reason=f"TRAIL_STOP (EMA9 {ema9_1h:.2f})")
                return

        # Time stop
        hours_open = (candle_close_time - pos.open_time) / 3_600_000
        if hours_open >= pos.time_stop_hours:
            self.close_position(symbol, mark_price, reason=f"TIME_STOP ({hours_open:.1f}h)")

    def get_summary(self) -> dict:
        with self.lock:
            open_pos = [p.to_dict() for p in self.positions.values() if p.status == "OPEN"]
            utilized = sum(p.margin_used for p in self.positions.values() if p.status == "OPEN")
            return {
                "wallet_balance": float(round(self.wallet_balance, 4)),
                "unrealized_pnl": float(round(self.unrealized_pnl_total, 4)),
                "realized_pnl": float(round(self.realized_pnl_total, 4)),
                "margin_balance": float(round(self.margin_balance, 4)),
                "available": float(round(self.available_balance, 4)),
                "utilized": float(round(utilized, 4)),
                "open_count": len(open_pos),
                "open_positions": open_pos,
                "history_count": len(self.position_history),
            }

    def print_summary(self):
        s = self.get_summary()
        print("\n" + "=" * 65)
        print(f"  CRYPTO TRADER v4 — WALLET SUMMARY ({self.symbol})")
        print("=" * 65)
        print(f"  Wallet Balance    : {s['wallet_balance']:.4f} USDT")
        print(f"  Unrealized PnL    : {s['unrealized_pnl']:.4f} USDT")
        print(f"  Realized PnL      : {s['realized_pnl']:.4f} USDT")
        print(f"  Margin Balance    : {s['margin_balance']:.4f} USDT")
        print(f"  Utilized          : {s.get('utilized', 0.0):.4f} USDT")
        print(f"  Available         : {s['available']:.4f} USDT")
        print(f"  Open Positions    : {s['open_count']}")
        for p in s["open_positions"]:
            print(f"    → {p['symbol']} {p['side']} | Playbook={p['playbook']} | "
                  f"Entry={p['entry_price']:.2f} | RemQty={p['remaining_quantity']:.4f} | "
                  f"U-PnL={p['unrealized_pnl']:.4f} ({p['unrealized_pnl']/p['margin_used']*100:.2f}%)")
        print("=" * 65 + "\n")


    @staticmethod
    def _to_decimal(value) -> Decimal:
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    @staticmethod
    def _serialize_decimals(obj):
        if isinstance(obj, Decimal):
            return str(obj)
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    def _to_json_compatible(self, val):
        if isinstance(val, Decimal):
            return str(val)
        if isinstance(val, Enum):
            return val.value
        if isinstance(val, dict):
            return {k: self._to_json_compatible(v) for k, v in val.items()}
        if isinstance(val, list):
            return [self._to_json_compatible(x) for x in val]
        return val

    def _deserialize_reducer_state(self, snap: dict) -> PortfolioState:
        state = PortfolioState(
            wallet_balance=self._to_decimal(snap.get("wallet_balance", "0")),
            realized_pnl_total=self._to_decimal(snap.get("realized_pnl_total", "0")),
            funding_total=self._to_decimal(snap.get("funding_total", "0") or "0"),
            last_ts=int(snap.get("reducer_last_ts") or snap.get("last_ts") or 0),
            events_applied=int(snap.get("reducer_events_applied", 0))
        )
        
        # open_positions
        raw_pos = snap.get("reducer_open_positions")
        if raw_pos is not None:
            for symbol, pdata in raw_pos.items():
                tp_levels = []
                for tp in pdata.get("tp_levels", []):
                    tp_levels.append({
                        "price": self._to_decimal(tp.get("price", "0")),
                        "pct": float(tp.get("pct", 0.0)),
                        "hit": bool(tp.get("hit", False)),
                        "label": tp.get("label", ""),
                    })
                state.open_positions[symbol] = {
                    "entry_price": self._to_decimal(pdata.get("entry_price", "0")),
                    "quantity": self._to_decimal(pdata.get("quantity", "0")),
                    "margin": self._to_decimal(pdata.get("margin", "0")),
                    "side": pdata.get("side"),
                    "playbook": pdata.get("playbook", "INTRADAY"),
                    "sl_price": self._to_decimal(pdata.get("sl_price", "0")),
                    "leverage": int(pdata.get("leverage", 1)),
                    "open_time": int(pdata.get("open_time", 0)),
                    "tp_levels": tp_levels,
                    "trailing_active": bool(pdata.get("trailing_active", False)),
                    "highest_price": self._to_decimal(pdata.get("highest_price", pdata.get("entry_price", "0"))),
                    "peak_pnl_pct": self._to_decimal(pdata.get("peak_pnl_pct", "0")),
                }
        elif "positions" in snap:
            # Fallback for legacy snapshots
            for symbol, pdata in snap["positions"].items():
                if pdata.get("status") == "OPEN":
                    tp_levels = []
                    for tp in pdata.get("tp_levels", []):
                        tp_levels.append({
                            "price": self._to_decimal(tp.get("price", "0")),
                            "pct": float(tp.get("pct", 0.0)),
                            "hit": bool(tp.get("hit", False)),
                            "label": tp.get("label", ""),
                        })
                    state.open_positions[symbol] = {
                        "entry_price": self._to_decimal(pdata.get("entry_price", "0")),
                        "quantity": self._to_decimal(pdata.get("remaining_quantity", "0")),
                        "margin": self._to_decimal(pdata.get("margin_used", "0")),
                        "side": pdata.get("side"),
                        "playbook": pdata.get("playbook", "INTRADAY"),
                        "sl_price": self._to_decimal(pdata.get("sl_price", "0")),
                        "leverage": int(pdata.get("leverage", 1)),
                        "open_time": int(pdata.get("open_time", 0)),
                        "tp_levels": tp_levels,
                        "trailing_active": bool(pdata.get("trailing_active", False)),
                        "highest_price": self._to_decimal(pdata.get("highest_price", pdata.get("entry_price", "0"))),
                        "peak_pnl_pct": self._to_decimal(pdata.get("peak_pnl_pct", "0")),
                    }

        # orders
        raw_orders = snap.get("reducer_orders", {})
        for oid, odata in raw_orders.items():
            state.orders[oid] = {
                "symbol": odata.get("symbol"),
                "side": odata.get("side"),
                "quantity": self._to_decimal(odata.get("quantity", "0")),
                "filled_quantity": self._to_decimal(odata.get("filled_quantity", "0")),
                "remaining_quantity": self._to_decimal(odata.get("remaining_quantity", "0")),
                "avg_fill_price": self._to_decimal(odata.get("avg_fill_price", "0")),
                "status": odata.get("status"),
                "order_type": odata.get("order_type"),
                "reduce_only": bool(odata.get("reduce_only", False)),
                "trigger_price": self._to_decimal(odata.get("trigger_price")) if odata.get("trigger_price") is not None else None,
                "limit_price": self._to_decimal(odata.get("limit_price")) if odata.get("limit_price") is not None else None,
                "expires_at": int(odata.get("expires_at")) if odata.get("expires_at") is not None else None,
            }

        # fills
        raw_fills = snap.get("reducer_fills", [])
        for f in raw_fills:
            state.fills.append({
                "order_id": f.get("order_id"),
                "symbol": f.get("symbol"),
                "side": f.get("side"),
                "quantity": self._to_decimal(f.get("quantity", "0")),
                "fill_price": self._to_decimal(f.get("fill_price", "0")),
                "fee": self._to_decimal(f.get("fee", "0")),
                "ts": int(f.get("ts", 0)),
                "sequence": int(f.get("sequence", 1)),
                **{k: v for k, v in f.items() if k not in ("quantity", "fill_price", "fee", "ts", "sequence")}
            })

        # violations
        state.violations = list(snap.get("reducer_violations", []))

        return state

    def _encode_snapshot(self, state: dict) -> str:
        json_str = json.dumps(state, default=self._serialize_decimals)
        sha256_hash = hashlib.sha256(json_str.encode('utf-8')).hexdigest()
        compressed = gzip.compress(json_str.encode('utf-8'))
        b64_str = base64.b64encode(compressed).decode('utf-8')
        return f"v1_gzip_b64:{sha256_hash}:{b64_str}"

    def _decode_snapshot(self, encoded: str) -> dict:
        if encoded.startswith("{"):
            return json.loads(encoded)
        if encoded.startswith("v1_gzip_b64:"):
            parts = encoded.split(":", 2)
            if len(parts) != 3:
                raise ValueError("Invalid encoded snapshot format")
            _, expected_hash, b64_str = parts
            compressed = base64.b64decode(b64_str.encode('utf-8'))
            json_bytes = gzip.decompress(compressed)
            json_str = json_bytes.decode('utf-8')
            actual_hash = hashlib.sha256(json_bytes).hexdigest()
            if actual_hash != expected_hash:
                raise ValueError("Snapshot integrity check failed: SHA256 hash mismatch")
            return json.loads(json_str)
        raise ValueError("Unknown snapshot format")

    def _save_state(self):
        # Sync dynamic peak fields back to reducer open positions before saving
        for symbol, pos in self.positions.items():
            if symbol in self._runtime_reducer_state.open_positions:
                self._runtime_reducer_state.open_positions[symbol]["highest_price"] = pos.highest_price
                self._runtime_reducer_state.open_positions[symbol]["peak_pnl_pct"] = pos.peak_pnl_pct

        state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "saved_at": self._now_ms(),
            "wallet_balance": self.wallet_balance,
            "realized_pnl_total": self.realized_pnl_total,
            "symbol": self.symbol,
            "state_namespace": self.state_namespace,
            "maker_fee_rate": self.maker_fee_rate,
            "taker_fee_rate": self.taker_fee_rate,
            "maintenance_margin_ratio": self.maintenance_margin_ratio,
            "positions": {s: p.to_dict() for s, p in self.positions.items()},
            "position_history": self.position_history,
            "reducer_open_positions": self._runtime_reducer_state.open_positions,
            "reducer_orders": self._runtime_reducer_state.orders,
            "reducer_fills": self._runtime_reducer_state.fills,
            "reducer_violations": self._runtime_reducer_state.violations,
            "reducer_last_ts": self._runtime_reducer_state.last_ts,
            "reducer_events_applied": self._runtime_reducer_state.events_applied,
        }
        json_compatible_state = self._to_json_compatible(state)
        self._atomic_write_json(self.state_file, json_compatible_state)
        encoded_state = self._encode_snapshot(json_compatible_state)
        self._wallet_store.save_snapshot(
            self._now_ms(),
            str(self.wallet_balance),
            str(self.realized_pnl_total),
            encoded_state,
            max_snapshots=self.max_snapshots,
        )

    def _atomic_write_json(self, path: Path, state: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        payload = self._encode_snapshot(state)
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

        if path.exists():
            try:
                path.replace(self.backup_state_file)
            except Exception as e:
                logger.warning(f"Failed to rotate wallet backup {path} -> {self.backup_state_file}: {e}")

        temp_path.replace(path)


    def _init_db(self):
        # Schema is now managed by WalletStore (initialized before _init_db is called).
        pass

    def _append_event_db(self, event: dict):
        payload_json = json.dumps(event.get("payload", {}), default=self._serialize_decimals)
        self._wallet_store.append_event(
            int(event.get("ts", 0)),
            event.get("event_type"),
            event.get("symbol"),
            event.get("namespace"),
            payload_json,
        )

    def _iter_events(self):
        rows = self._wallet_store.iter_events()
        for ts, et, sym, ns, payload_json in rows:
            yield {"ts": ts, "event_type": et, "symbol": sym, "namespace": ns, "payload": json.loads(payload_json)}
        # JSONL fallback for legacy migration (only when store is empty)
        if not rows and self.events_file.exists():
            with open(self.events_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        yield json.loads(line)

    def _append_event(self, event_type: str, payload: dict) -> dict:
        event = {
            "ts": self._now_ms(),
            "event_type": event_type,
            "symbol": self.symbol,
            "namespace": self.state_namespace,
            "payload": payload,
        }
        self.events_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=self._serialize_decimals) + "\n")
        self._append_event_db(event)
        return event

    def _emit_event(self, event_type: str, payload: dict):
        """Authoritative runtime transition: append event, then apply reducer-derived runtime updates."""
        event = self._append_event(event_type, payload)
        self._persist_domain_event_to_db(event)
        self.reducer.apply(self._runtime_reducer_state, event)
        if self.event_hook:
            try:
                self.event_hook(event)
            except Exception:
                pass
        self.wallet_balance = self._runtime_reducer_state.wallet_balance
        self.realized_pnl_total = self._runtime_reducer_state.realized_pnl_total
        self._sync_positions_from_reducer()
        self._sync_orders_from_reducer()
        self._sync_fills_from_reducer()
        if event_type == "INVARIANT_VIOLATION" and self.halt_on_invariant_violation:
            self.halted = True

    def _sync_positions_from_reducer(self):
        """Align runtime open positions with reducer-authoritative open_positions."""
        current = {}
        for symbol, pdata in self._runtime_reducer_state.open_positions.items():
            existing = self.positions.get(symbol)
            if existing and existing.status == "OPEN":
                existing.remaining_quantity = pdata["quantity"]
                existing.original_quantity = max(existing.original_quantity, pdata["quantity"])
                existing.entry_price = pdata["entry_price"]
                existing.sl_price = pdata.get("sl_price", existing.sl_price)
                existing.trailing_active = bool(pdata.get("trailing_active", existing.trailing_active))
                existing.tp_levels = pdata.get("tp_levels", existing.tp_levels)
                existing.protective_orders = pdata.get("protective_orders", existing.protective_orders)
                existing.notional = existing.remaining_quantity * existing.entry_price
                existing.margin_used = existing.notional / existing.leverage
                current[symbol] = existing
            else:
                side = PositionSide(pdata.get("side", "LONG"))
                playbook = Playbook(pdata.get("playbook", "INTRADAY"))
                qty = pdata["quantity"]
                entry = pdata["entry_price"]
                lev = int(pdata.get("leverage", self.leverage))
                prior = self.positions.get(symbol)
                prior_tp = prior.tp_levels if prior and prior.tp_levels else []
                tp_levels = pdata.get("tp_levels", prior_tp)
                if not tp_levels:
                    # Retroactive fallback for legacy database positions
                    if playbook == Playbook.INTRADAY:
                        p1 = entry * Decimal("1.01" if side == PositionSide.LONG else "0.99")
                        p2 = entry * Decimal("1.02" if side == PositionSide.LONG else "0.98")
                        tp_levels = [
                            {"label": "TP1", "pct": 0.5, "price": p1, "hit": False},
                            {"label": "TP2", "pct": 0.5, "price": p2, "hit": False}
                        ]

                current[symbol] = EnhancedPosition(
                    symbol=symbol,
                    side=side,
                    playbook=playbook,
                    entry_price=entry,
                    original_quantity=qty,
                    remaining_quantity=qty,
                    notional=qty * entry,
                    margin_used=(qty * entry) / lev,
                    leverage=lev,
                    open_time=int(pdata.get("open_time", self._now_ms())),
                    sl_price=pdata.get("sl_price", Decimal("0")),
                    tp_levels=tp_levels,
                    trailing_active=bool(pdata.get("trailing_active", False)),
                    time_stop_hours=int(pdata.get("time_stop_hours", 18)),
                    highest_price=self._to_decimal(pdata.get("highest_price", entry)),
                    peak_pnl_pct=self._to_decimal(pdata.get("peak_pnl_pct", Decimal("0"))),
                    protective_orders=dict(pdata.get("protective_orders", {})),
                )
        self.positions = current

    def _sync_orders_from_reducer(self):
        """Align runtime orders with reducer-authoritative order state."""
        synced = {}
        for oid, odata in self._runtime_reducer_state.orders.items():
            side = PositionSide(odata.get("side", "LONG"))
            existing = self.orders.get(oid)
            if existing:
                existing.side = side
                existing.quantity = odata.get("quantity", existing.quantity)
                existing.filled_quantity = odata.get("filled_quantity", existing.filled_quantity)
                existing.avg_fill_price = odata.get("avg_fill_price", existing.avg_fill_price)
                existing.status = OrderStatus(odata.get("status", existing.status.value))
                existing.order_type = OrderType(odata.get("order_type", existing.order_type.value))
                existing.reduce_only = bool(odata.get("reduce_only", existing.reduce_only))
                existing.trigger_price = odata.get("trigger_price", existing.trigger_price)
                existing.limit_price = odata.get("limit_price", existing.limit_price)
                existing.expires_at = odata.get("expires_at", existing.expires_at)
                synced[oid] = existing
            else:
                synced[oid] = Order(
                    id=oid,
                    symbol=odata.get("symbol", self.symbol),
                    side=side,
                    order_type=OrderType(odata.get("order_type", OrderType.MARKET.value)),
                    quantity=odata.get("quantity", Decimal("0")),
                    status=OrderStatus(odata.get("status", OrderStatus.NEW.value)),
                    created_at=self._now_ms(),
                    reduce_only=bool(odata.get("reduce_only", False)),
                    trigger_price=odata.get("trigger_price"),
                    limit_price=odata.get("limit_price"),
                    expires_at=odata.get("expires_at"),
                    filled_quantity=odata.get("filled_quantity", Decimal("0")),
                    avg_fill_price=odata.get("avg_fill_price", Decimal("0")),
                )
        self.orders = synced

    def _sync_fills_from_reducer(self):
        """Align runtime fills from reducer-authoritative fill event stream."""
        converted: List[Fill] = []
        for f in self._runtime_reducer_state.fills:
            try:
                converted.append(
                    Fill(
                        order_id=f.get("order_id"),
                        symbol=f.get("symbol", self.symbol),
                        side=PositionSide(f.get("side", "LONG")),
                        quantity=self._to_decimal(f.get("quantity", "0")),
                        fill_price=self._to_decimal(f.get("fill_price", "0")),
                        fee=self._to_decimal(f.get("fee", "0")),
                        timestamp=int(f.get("ts", self._now_ms())),
                        sequence=int(f.get("sequence", 1)),
                    )
                )
            except Exception:
                continue
        self.fills = converted

    def _create_order(self, symbol: str, side: PositionSide, quantity: Decimal) -> Order:
        return self._create_order_with_type(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=OrderType.MARKET,
        )

    def _create_order_with_type(
        self,
        symbol: str,
        side: PositionSide,
        quantity: Decimal,
        order_type: OrderType,
        *,
        reduce_only: bool = False,
        trigger_price: Optional[Decimal] = None,
        limit_price: Optional[Decimal] = None,
        expires_at: Optional[int] = None,
    ) -> Order:
        order_id = f"{symbol}-{self._now_ms()}-{len(self.orders)+1}"
        self._emit_event(
            "ORDER_CREATED",
            {
                "order_id": order_id,
                "symbol": symbol,
                "side": side.value,
                "quantity": quantity,
                "status": OrderStatus.PENDING.value,
                "order_type": order_type.value,
                "reduce_only": reduce_only,
                "trigger_price": trigger_price,
                "limit_price": limit_price,
                "expires_at": expires_at,
            },
        )
        return self.orders[order_id]

    def cancel_order(self, order_id: str, reason: str = "USER_CANCEL") -> bool:
        with self.lock:
            order = self.orders.get(order_id)
            if not order:
                return False
            if order.status in {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED}:
                return False
            self._emit_event("ORDER_CANCELLED", {"order_id": order_id, "reason": reason})
            return True

    def place_pending_order(
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
    ) -> Order:
        with self.lock:
            if self.halted:
                raise ValueError("Cannot place order: Wallet is halted due to invariant violation")
            return self._create_order_with_type(
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type=order_type,
                reduce_only=reduce_only,
                trigger_price=trigger_price,
                limit_price=limit_price,
                expires_at=expires_at,
            )

    def evaluate_pending_orders(self, symbol: str, mark_price: float):
        with self.lock:
            if self.halted:
                return
            price = self._to_decimal(mark_price)
            for order in list(self.orders.values()):
                if order.symbol != symbol or order.status != OrderStatus.PENDING:
                    continue
                if order.expires_at and self._now_ms() >= order.expires_at:
                    self._emit_event("ORDER_EXPIRED", {"order_id": order.id})
                    continue
                if self._should_trigger_order(order, price):
                    if order.reduce_only and not self.get_open_position(symbol):
                        self._emit_event(
                            "ORDER_REJECTED",
                            {"order_id": order.id, "reason": "REDUCE_ONLY_WITHOUT_POSITION"},
                        )
                        continue
                    if order.reduce_only:
                        pos = self.get_open_position(symbol)
                        if pos and pos.side == order.side:
                            self._emit_event(
                                "ORDER_REJECTED",
                                {"order_id": order.id, "reason": "REDUCE_ONLY_EXPOSURE_INCREASE"},
                            )
                            continue
                    fill_price = self._execution_price(price, order.side, is_entry=True)
                    remaining = max(Decimal("0"), order.quantity - order.filled_quantity)
                    if remaining > Decimal("0"):
                        self._execute_order_in_chunks(
                            order=order,
                            total_quantity=remaining,
                            fill_price=fill_price,
                            fee_rate=self.taker_fee_rate,
                            chunk_count=1,
                        )
            self._run_invariant_checks()

    def _should_trigger_order(self, order: Order, mark_price: Decimal) -> bool:
        if order.order_type == OrderType.MARKET:
            return True
        if order.order_type == OrderType.LIMIT and order.limit_price is not None:
            return mark_price <= order.limit_price if order.side == PositionSide.LONG else mark_price >= order.limit_price
        if order.order_type == OrderType.STOP_MARKET and order.trigger_price is not None:
            return mark_price >= order.trigger_price if order.side == PositionSide.LONG else mark_price <= order.trigger_price
        if order.order_type == OrderType.TAKE_PROFIT and order.trigger_price is not None:
            return mark_price >= order.trigger_price if order.side == PositionSide.LONG else mark_price <= order.trigger_price
        return False

    def apply_funding(self, symbol: str, funding_rate: Decimal):
        with self.lock:
            pos = self.get_open_position(symbol)
            if not pos:
                return Decimal("0")
            funding_rate = self._to_decimal(funding_rate)
            notional = pos.remaining_quantity * pos.entry_price
            # Longs usually pay when positive, shorts receive; simplified sign by side.
            direction = Decimal("-1") if pos.side == PositionSide.LONG else Decimal("1")
            amount = (notional * funding_rate) * direction
            self._emit_event(
                "FUNDING_APPLIED",
                {"symbol": symbol, "rate": funding_rate, "notional": notional, "amount": amount},
            )
            self.assert_runtime_matches_replay()
            return amount

    def _register_default_invariants(self):
        self.invariant_rules = [
            InvariantRule(
                name="equity_consistency",
                severity=Severity.FATAL,
                fn=self._check_equity_consistency
            ),
            InvariantRule(
                name="order_fill_limits",
                severity=Severity.CRITICAL,
                fn=self._check_order_fill_limits
            ),
            InvariantRule(
                name="remaining_quantity_non_negative",
                severity=Severity.CRITICAL,
                fn=self._check_remaining_quantity_non_negative
            ),
            InvariantRule(
                name="available_margin_non_negative",
                severity=Severity.CRITICAL,
                fn=self._check_available_margin_non_negative
            ),
            InvariantRule(
                name="reduce_only_never_increases_exposure",
                severity=Severity.CRITICAL,
                fn=self._check_reduce_only_never_increases_exposure
            ),
        ]

    def _check_equity_consistency(self) -> Tuple[bool, dict]:
        expected = self.wallet_balance + self.unrealized_pnl_total
        actual = Decimal(str(self.margin_balance))
        diff = abs(actual - expected)
        if diff > Decimal("0.0001"):
            return True, {"expected": str(expected), "actual": str(actual), "diff": str(diff)}
        return False, {}

    def _check_order_fill_limits(self) -> Tuple[bool, dict]:
        fill_sums = {}
        for f in self.fills:
            fill_sums[f.order_id] = fill_sums.get(f.order_id, Decimal("0")) + f.quantity
        
        for oid, o in self.orders.items():
            filled_sum = fill_sums.get(oid, Decimal("0"))
            if filled_sum > o.quantity:
                return True, {"order_id": oid, "sum_fills": str(filled_sum), "order_qty": str(o.quantity)}
            if o.filled_quantity > o.quantity:
                return True, {"order_id": oid, "filled_quantity": str(o.filled_quantity), "order_qty": str(o.quantity)}
        return False, {}

    def _check_remaining_quantity_non_negative(self) -> Tuple[bool, dict]:
        for oid, o in self.orders.items():
            remaining = o.quantity - o.filled_quantity
            if remaining < Decimal("0"):
                return True, {"order_id": oid, "remaining": str(remaining)}
        for symbol, pos in self.positions.items():
            if pos.status == "OPEN" and pos.remaining_quantity < Decimal("0"):
                return True, {"symbol": symbol, "position_remaining": str(pos.remaining_quantity)}
        return False, {}

    def _check_available_margin_non_negative(self) -> Tuple[bool, dict]:
        avail = Decimal(str(self.available_balance))
        if avail < Decimal("0"):
            return True, {"available_margin": str(avail)}
        return False, {}

    def _check_reduce_only_never_increases_exposure(self) -> Tuple[bool, dict]:
        from collections import defaultdict
        active_reduce_only = defaultdict(list)
        for o in self.orders.values():
            if o.status in (OrderStatus.NEW, OrderStatus.PENDING) and o.reduce_only:
                active_reduce_only[o.symbol].append(o)
                
        for symbol, orders in active_reduce_only.items():
            pos = self.positions.get(symbol)
            if not pos or pos.status != "OPEN":
                return True, {"symbol": symbol, "reason": "No open position for reduce_only order", "orders": [o.id for o in orders]}
                
            expected_side = PositionSide.SHORT if pos.side == PositionSide.LONG else PositionSide.LONG
            total_qty = Decimal("0")
            for o in orders:
                if o.side != expected_side:
                    return True, {"symbol": symbol, "order_id": o.id, "reason": f"reduce_only side {o.side} does not reduce position {pos.side}"}
                total_qty += o.quantity
                
            if total_qty > pos.remaining_quantity:
                return True, {
                    "symbol": symbol,
                    "reason": "Sum of reduce_only orders exceeds position remaining quantity",
                    "total_reduce_qty": str(total_qty),
                    "position_qty": str(pos.remaining_quantity),
                    "orders": [o.id for o in orders]
                }
        return False, {}

    def _run_invariant_checks(self):
        self.run_invariant_sweep()

    def run_invariant_sweep(self) -> List[dict]:
        """Run all registered invariant rules and apply halt policies on violation."""
        violations = []
        with self.lock:
            for rule in self.invariant_rules:
                try:
                    violated, context = rule.fn()
                    if violated:
                        violations.append({
                            "rule_name": rule.name,
                            "severity": rule.severity.value,
                            "context": context,
                        })
                        self._emit_event(
                            "INVARIANT_VIOLATION",
                            {
                                "rule_name": rule.name,
                                "severity": rule.severity.value,
                                "context": context,
                            }
                        )
                        # Check halt policies
                        if rule.severity == Severity.FATAL and self.halt_on_fatal:
                            self.halted = True
                        elif rule.severity == Severity.CRITICAL and self.halt_on_critical:
                            self.halted = True
                except Exception as e:
                    logger.error(f"Error checking invariant rule {rule.name}: {e}")
        return violations

    def run_funding_scheduler(self, symbol: str, funding_rate: Decimal, interval_ms: int, now_ms: Optional[int] = None) -> Decimal:
        """Apply funding if interval has elapsed since last application for symbol."""
        now = int(now_ms if now_ms is not None else self._now_ms())
        last_ts = self._wallet_store.get_funding_last_ts(symbol)
        if now - last_ts < int(interval_ms):
            return Decimal("0")
        return self.apply_funding(symbol, self._to_decimal(funding_rate))

    def run_housekeeping(
        self,
        symbol: str,
        mark_price: float,
        funding_rate: Optional[Decimal] = None,
        funding_interval_ms: Optional[int] = None,
    ):
        """Run operational safety and periodic tasks in one call."""
        with self.lock:
            if self.halted:
                return
        self.evaluate_pending_orders(symbol, mark_price)
        if funding_rate is not None and funding_interval_ms is not None:
            self.run_funding_scheduler(symbol, funding_rate, funding_interval_ms)
        self.assert_runtime_matches_replay()

    def _record_fill(self, order: Order, quantity: Decimal, fill_price: Decimal, fee: Decimal, sequence: int = 1):
        prospective_filled = order.filled_quantity + quantity
        status = OrderStatus.FILLED if prospective_filled >= order.quantity else OrderStatus.PARTIALLY_FILLED
        event_type = "ORDER_FILLED" if status == OrderStatus.FILLED else "ORDER_PARTIALLY_FILLED"
        self._emit_event(
            event_type,
            {
                "order_id": order.id,
                "symbol": order.symbol,
                "side": order.side.value,
                "quantity": quantity,
                "fill_price": fill_price,
                "fee": fee,
                "status": status.value,
                "sequence": sequence,
            },
        )

    def _execute_order_in_chunks(
        self,
        order: Order,
        total_quantity: Decimal,
        fill_price: Decimal,
        fee_rate: Decimal,
        chunk_count: int = 1,
    ) -> Decimal:
        chunk_count = max(1, int(chunk_count))
        remaining = total_quantity
        total_fee = Decimal("0")
        for idx in range(1, chunk_count + 1):
            if idx == chunk_count:
                chunk_qty = remaining
            else:
                chunk_qty = (total_quantity / Decimal(str(chunk_count))).quantize(Decimal("0.00000001"))
                remaining -= chunk_qty
            chunk_fee = abs(chunk_qty * fill_price) * fee_rate
            total_fee += chunk_fee
            self._record_fill(order, chunk_qty, fill_price, chunk_fee, sequence=idx)
        return total_fee

    def replay_event_log(self) -> dict:
        if not self.events_file.exists():
            return {"event_count": 0, "order_count": 0, "fill_count": 0}
        event_count = 0
        order_count = 0
        fill_count = 0
        for event in self._iter_events():
            event_count += 1
            et = event.get("event_type")
            if et == "ORDER_CREATED":
                order_count += 1
            elif et in ("ORDER_FILLED", "ORDER_PARTIALLY_FILLED"):
                fill_count += 1
        return {"event_count": event_count, "order_count": order_count, "fill_count": fill_count}

    def get_order_timeline(self, order_id: str) -> List[dict]:
        """Return chronological event timeline for one order id."""
        timeline: List[dict] = []
        for event in self._iter_events():
            payload = event.get("payload", {})
            if payload.get("order_id") == order_id:
                timeline.append(event)
        timeline.sort(key=lambda e: (int(e.get("ts", 0)), e.get("event_type", "")))
        return timeline

    def get_recent_events(self, limit: int = 100, event_type: Optional[str] = None) -> List[dict]:
        """Simple event browser helper for observability tooling."""
        rows: List[dict] = []
        for event in self._iter_events():
            if event_type and event.get("event_type") != event_type:
                continue
            rows.append(event)
        rows.sort(key=lambda e: int(e.get("ts", 0)), reverse=True)
        return rows[: max(1, limit)]

    def get_position_timeline(self, symbol: str) -> List[dict]:
        """Returns a chronological list of events for the specified symbol."""
        timeline = []
        try:
            rows = self._wallet_store.get_events_by_symbol(symbol)
            for ts, et, sym, ns, payload_json in rows:
                timeline.append({
                    "ts": ts,
                    "event_type": et,
                    "symbol": sym,
                    "namespace": ns,
                    "payload": json.loads(payload_json)
                })
        except Exception as e:
            logger.warning(f"Failed to get position timeline from DB: {e}")
        return timeline

    def get_invariant_events(self, limit: int = 100) -> List[dict]:
        """Returns the list of recent INVARIANT_VIOLATION events."""
        violations = []
        try:
            rows = self._wallet_store.get_events_by_type("INVARIANT_VIOLATION", limit)
            for ts, et, sym, ns, payload_json in rows:
                violations.append({
                    "ts": ts,
                    "event_type": et,
                    "symbol": sym,
                    "namespace": ns,
                    "payload": json.loads(payload_json)
                })
            violations.reverse()
        except Exception as e:
            logger.warning(f"Failed to get invariant events from DB: {e}")
        return violations

    def get_funding_history(self, symbol: str) -> List[dict]:
        """Returns chronological list of funding payments/events for a symbol."""
        history = []
        try:
            rows = self._wallet_store.get_funding_history(symbol)
            for ts, rate, notional, amount in rows:
                history.append({
                    "ts": ts,
                    "symbol": symbol,
                    "rate": Decimal(rate),
                    "notional": Decimal(notional),
                    "amount": Decimal(amount)
                })
        except Exception as e:
            logger.warning(f"Failed to get funding history from DB: {e}")
        return history

    def replay_until(self, ts: int) -> PortfolioState:
        """Replay all events in order up to timestamp `ts` (inclusive) and returns the state."""
        state = PortfolioState(wallet_balance=self.initial_balance)
        if not self.events_file.exists() and self._wallet_store.count_events() == 0:
            return state

        last_ts = -1
        seen = set()
        for event in self._iter_events():
            event_ts = int(event.get("ts", 0))
            if event_ts > ts:
                break
            fingerprint = json.dumps(event, sort_keys=True, default=str)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            if event_ts < last_ts:
                raise ValueError("Out-of-order event stream detected")
            last_ts = event_ts
            self.reducer.apply(state, event)
        return state

    def compare_runtime_vs_replay(self, ts: Optional[int] = None) -> dict:
        """Compares runtime state against replayed event state at timestamp `ts` (or current if None).
        
        Returns a dictionary summarizing any discrepancies.
        """
        with self.lock:
            if ts is None:
                replayed = self.replay_portfolio_state()
            else:
                replayed = self.replay_until(ts)

            diffs = {}
            
            # 1. Balances
            balance_diff = abs(self.wallet_balance - replayed.wallet_balance)
            if balance_diff >= Decimal("0.0001"):
                diffs["wallet_balance"] = {
                    "runtime": float(self.wallet_balance),
                    "replay": float(replayed.wallet_balance),
                    "diff": float(balance_diff)
                }

            pnl_diff = abs(self.realized_pnl_total - replayed.realized_pnl_total)
            if pnl_diff >= Decimal("0.0001"):
                diffs["realized_pnl"] = {
                    "runtime": float(self.realized_pnl_total),
                    "replay": float(replayed.realized_pnl_total),
                    "diff": float(pnl_diff)
                }

            # 2. Open Positions
            runtime_open = {s: p for s, p in self.positions.items() if p.status == "OPEN"}
            if len(runtime_open) != len(replayed.open_positions):
                diffs["position_count"] = {
                    "runtime": len(runtime_open),
                    "replay": len(replayed.open_positions)
                }
            
            position_details = {}
            for symbol in set(runtime_open.keys()) | set(replayed.open_positions.keys()):
                run_pos = runtime_open.get(symbol)
                replay_pos = replayed.open_positions.get(symbol)
                
                if not run_pos or not replay_pos:
                    position_details[symbol] = {
                        "runtime_exists": run_pos is not None,
                        "replay_exists": replay_pos is not None
                    }
                else:
                    pos_diff = {}
                    if abs(run_pos.entry_price - replay_pos["entry_price"]) >= Decimal("0.0001"):
                        pos_diff["entry_price"] = {"runtime": float(run_pos.entry_price), "replay": float(replay_pos["entry_price"])}
                    if abs(run_pos.remaining_quantity - replay_pos["quantity"]) >= Decimal("0.0001"):
                        pos_diff["quantity"] = {"runtime": float(run_pos.remaining_quantity), "replay": float(replay_pos["quantity"])}
                    if abs(run_pos.sl_price - replay_pos["sl_price"]) >= Decimal("0.0001"):
                        pos_diff["sl_price"] = {"runtime": float(run_pos.sl_price), "replay": float(replay_pos["sl_price"])}
                    if run_pos.trailing_active != replay_pos.get("trailing_active", False):
                        pos_diff["trailing_active"] = {"runtime": run_pos.trailing_active, "replay": replay_pos.get("trailing_active")}
                    
                    if pos_diff:
                        position_details[symbol] = pos_diff
            
            if position_details:
                diffs["positions"] = position_details

            # 3. Orders
            if len(self.orders) != len(replayed.orders):
                diffs["order_count"] = {
                    "runtime": len(self.orders),
                    "replay": len(replayed.orders)
                }

            order_details = {}
            for oid in set(self.orders.keys()) | set(replayed.orders.keys()):
                run_order = self.orders.get(oid)
                replay_order = replayed.orders.get(oid)
                
                if not run_order or not replay_order:
                    order_details[oid] = {
                        "runtime_exists": run_order is not None,
                        "replay_exists": replay_order is not None
                    }
                else:
                    ord_diff = {}
                    if run_order.status.value != replay_order["status"]:
                        ord_diff["status"] = {"runtime": run_order.status.value, "replay": replay_order["status"]}
                    if abs(run_order.quantity - replay_order["quantity"]) >= Decimal("0.0001"):
                        ord_diff["quantity"] = {"runtime": float(run_order.quantity), "replay": float(replay_order["quantity"])}
                    if abs(run_order.filled_quantity - replay_order["filled_quantity"]) >= Decimal("0.0001"):
                        ord_diff["filled_quantity"] = {"runtime": float(run_order.filled_quantity), "replay": float(replay_order["filled_quantity"])}
                    
                    if ord_diff:
                        order_details[oid] = ord_diff
            
            if order_details:
                diffs["orders"] = order_details

            # 4. Fills
            if len(self.fills) != len(replayed.fills):
                diffs["fill_count"] = {
                    "runtime": len(self.fills),
                    "replay": len(replayed.fills)
                }
            
            fill_details = []
            for idx, (run_fill, replay_fill) in enumerate(zip(self.fills, replayed.fills)):
                fill_diff = {}
                if run_fill.order_id != replay_fill.get("order_id"):
                    fill_diff["order_id"] = {"runtime": run_fill.order_id, "replay": replay_fill.get("order_id")}
                if abs(run_fill.quantity - Decimal(str(replay_fill.get("quantity")))) >= Decimal("0.0001"):
                    fill_diff["quantity"] = {"runtime": float(run_fill.quantity), "replay": float(replay_fill.get("quantity"))}
                if abs(run_fill.fee - Decimal(str(replay_fill.get("fee")))) >= Decimal("0.0001"):
                    fill_diff["fee"] = {"runtime": float(run_fill.fee), "replay": float(replay_fill.get("fee"))}
                
                if fill_diff:
                    fill_details.append({"index": idx, "diffs": fill_diff})
            
            if fill_details:
                diffs["fills"] = fill_details

            return {
                "in_sync": len(diffs) == 0,
                "timestamp": self._now_ms() if ts is None else ts,
                "discrepancies": diffs
            }

    def replay_portfolio_state(self) -> PortfolioState:
        """Rebuild a minimal portfolio view from event log only."""
        state = PortfolioState(wallet_balance=self.initial_balance)
        if not self.events_file.exists() and self._wallet_store.count_events() == 0:
            return state

        last_ts = -1
        seen = set()
        for event in self._iter_events():
            fingerprint = json.dumps(event, sort_keys=True, default=str)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            ts = int(event.get("ts", 0))
            if ts < last_ts:
                raise ValueError("Out-of-order event stream detected")
            last_ts = ts
            self.reducer.apply(state, event)
        return state

    def assert_runtime_matches_replay(self):
        """Assert that all runtime state perfectly matches a full replay from the event log."""
        with self.lock:
            replayed = self.replay_portfolio_state()
            
            # 1. Compare balances
            assert abs(self.wallet_balance - replayed.wallet_balance) < Decimal("0.0001"), \
                f"Balance drift: runtime={self.wallet_balance}, replay={replayed.wallet_balance}"
            assert abs(self.realized_pnl_total - replayed.realized_pnl_total) < Decimal("0.0001"), \
                f"Realized PnL drift: runtime={self.realized_pnl_total}, replay={replayed.realized_pnl_total}"
            
            # 2. Compare open positions
            runtime_open = {s: p for s, p in self.positions.items() if p.status == "OPEN"}
            assert len(runtime_open) == len(replayed.open_positions), \
                f"Position count mismatch: runtime={len(runtime_open)}, replay={len(replayed.open_positions)}"
            
            for symbol, replay_pos in replayed.open_positions.items():
                run_pos = runtime_open.get(symbol)
                assert run_pos is not None, f"Position for {symbol} missing in runtime"
                assert abs(run_pos.entry_price - replay_pos["entry_price"]) < Decimal("0.0001"), \
                    f"Entry price mismatch for {symbol}: runtime={run_pos.entry_price}, replay={replay_pos['entry_price']}"
                assert abs(run_pos.remaining_quantity - replay_pos["quantity"]) < Decimal("0.0001"), \
                    f"Quantity mismatch for {symbol}: runtime={run_pos.remaining_quantity}, replay={replay_pos['quantity']}"
                assert abs(run_pos.sl_price - replay_pos["sl_price"]) < Decimal("0.0001"), \
                    f"SL price mismatch for {symbol}: runtime={run_pos.sl_price}, replay={replay_pos['sl_price']}"
                assert run_pos.trailing_active == replay_pos.get("trailing_active", False), \
                    f"Trailing active mismatch for {symbol}: runtime={run_pos.trailing_active}, replay={replay_pos.get('trailing_active')}"
            
            # 3. Compare orders
            assert len(self.orders) == len(replayed.orders), \
                f"Order count mismatch: runtime={len(self.orders)}, replay={len(replayed.orders)}"
            for oid, replay_order in replayed.orders.items():
                run_order = self.orders.get(oid)
                assert run_order is not None, f"Order {oid} missing in runtime"
                assert run_order.status.value == replay_order["status"], \
                    f"Order {oid} status mismatch: runtime={run_order.status.value}, replay={replay_order['status']}"
                assert abs(run_order.quantity - replay_order["quantity"]) < Decimal("0.0001"), \
                    f"Order {oid} quantity mismatch: runtime={run_order.quantity}, replay={replay_order['quantity']}"
                assert abs(run_order.filled_quantity - replay_order["filled_quantity"]) < Decimal("0.0001"), \
                    f"Order {oid} filled quantity mismatch: runtime={run_order.filled_quantity}, replay={replay_order['filled_quantity']}"
            
            # 4. Compare fills
            assert len(self.fills) == len(replayed.fills), \
                f"Fill count mismatch: runtime={len(self.fills)}, replay={len(replayed.fills)}"
            for idx, replay_fill in enumerate(replayed.fills):
                run_fill = self.fills[idx]
                assert run_fill.order_id == replay_fill.get("order_id"), \
                    f"Fill {idx} order ID mismatch: runtime={run_fill.order_id}, replay={replay_fill.get('order_id')}"
                assert abs(run_fill.quantity - Decimal(str(replay_fill.get("quantity")))) < Decimal("0.0001"), \
                    f"Fill {idx} quantity mismatch"
                assert abs(run_fill.fee - Decimal(str(replay_fill.get("fee")))) < Decimal("0.0001"), \
                    f"Fill {idx} fee mismatch"


    def _persist_domain_event_to_db(self, event: dict):
        et = event.get("event_type")
        payload = event.get("payload", {})
        ts = int(event.get("ts", self._now_ms()))
        self._wallet_store.persist_domain_event(et, payload, ts)

    @staticmethod
    def _sanitize_path_component(value: str) -> str:
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        sanitized = "".join(ch if ch in allowed else "_" for ch in value)
        return sanitized or "default"

    def _sync_unrealized_total(self):
        with self.lock:
            self.unrealized_pnl_total = sum(
                p.unrealized_pnl for p in self.positions.values() if p.status == "OPEN"
            )

    def _load_state(self):
        start_time = time.time()
        if self._load_from_db_snapshot_and_replay():
            try:
                self.assert_runtime_matches_replay()
            except Exception as e:
                logger.warning(f"Replay parity check failed after DB restore: {e}")
            return
        if not self.state_file.exists():
            return
        try:
            state = self._load_state_with_recovery()
            self.wallet_balance = self._to_decimal(state.get("wallet_balance", self.wallet_balance))
            self.realized_pnl_total = self._to_decimal(state.get("realized_pnl_total", Decimal("0")))
            self.maker_fee_rate = self._to_decimal(state.get("maker_fee_rate", self.maker_fee_rate))
            self.taker_fee_rate = self._to_decimal(state.get("taker_fee_rate", self.taker_fee_rate))
            self.maintenance_margin_ratio = self._to_decimal(state.get("maintenance_margin_ratio", self.maintenance_margin_ratio))
            self.positions = {
                s: EnhancedPosition.from_dict(d)
                for s, d in state.get("positions", {}).items()
                if d.get("status") == "OPEN"
            }
            self.position_history = state.get("position_history", [])
            
            self._runtime_reducer_state = self._deserialize_reducer_state(state)
            self._sync_positions_from_reducer()
            self._sync_orders_from_reducer()
            self._sync_fills_from_reducer()
            self._sync_unrealized_total()
            logger.info(f"Loaded wallet state from {self.state_file}")

            self.replay_duration_ms = 0.0
            self.delta_size = 0
            self.recovery_latency_ms = (time.time() - start_time) * 1000.0

            try:
                self.assert_runtime_matches_replay()
            except Exception as e:
                logger.warning(f"Replay parity check failed after file restore: {e}")
        except Exception as e:
            logger.warning(f"Failed to load state: {e}")

    def _load_from_db_snapshot_and_replay(self) -> bool:
        start_time = time.time()
        try:
            row = self._wallet_store.get_latest_snapshot()
            if not row:
                return False
            snap_ts, wallet_balance, realized_pnl_total, state_json = row
            snap = self._decode_snapshot(state_json)
            self.wallet_balance = self._to_decimal(wallet_balance)
            self.realized_pnl_total = self._to_decimal(realized_pnl_total)
            self.positions = {
                s: EnhancedPosition.from_dict(d)
                for s, d in snap.get("positions", {}).items()
                if d.get("status") == "OPEN"
            }
            self.position_history = snap.get("position_history", [])
            
            replay_state = self._deserialize_reducer_state(snap)
            
            replay_start = time.time()
            delta_count = 0
            skip_count = replay_state.events_applied
            for idx, event in enumerate(self._iter_events()):
                if skip_count > 0:
                    if idx >= skip_count:
                        self.reducer.apply(replay_state, event)
                        delta_count += 1
                else:
                    # Legacy fallback for snapshots without events_applied
                    if int(event.get("ts", 0)) > int(snap_ts):
                        self.reducer.apply(replay_state, event)
                        delta_count += 1
            replay_end = time.time()

            self._runtime_reducer_state = replay_state
            self.wallet_balance = replay_state.wallet_balance
            self.realized_pnl_total = replay_state.realized_pnl_total
            self._sync_positions_from_reducer()
            self._sync_orders_from_reducer()
            self._sync_fills_from_reducer()
            self._sync_unrealized_total()
            
            end_time = time.time()
            self.replay_duration_ms = (replay_end - replay_start) * 1000.0
            self.delta_size = delta_count
            self.recovery_latency_ms = (end_time - start_time) * 1000.0

            return True
        except Exception as e:
            logger.warning(f"DB snapshot/replay load failed: {e}")
            return False

    def _load_state_with_recovery(self) -> dict:
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                content = f.read()
            return self._decode_snapshot(content)
        except Exception as primary_err:
            logger.warning(f"Primary wallet state load failed ({self.state_file}): {primary_err}")
            if self.backup_state_file.exists():
                with open(self.backup_state_file, "r", encoding="utf-8") as f:
                    content = f.read()
                logger.warning(f"Recovered wallet state from backup file {self.backup_state_file}")
                return self._decode_snapshot(content)
            raise



    def _now_ms(self) -> int:
        return int(self._now_ms_fn())

    def _calculate_fee(self, notional: Decimal, is_taker: bool = True) -> float:
        rate = self.taker_fee_rate if is_taker else self.maker_fee_rate
        return abs(notional) * rate

    def _calculate_tds(self, is_sell_leg: bool, gross_value: Decimal) -> Decimal:
        """TDS is deducted only on the SELL leg of a trade (F2).

        LONG: the sell happens at close. SHORT: the sell happens at open.
        Returns the deductible amount (0 when disabled or not a sell leg).
        """
        if not self.tds_enabled or not is_sell_leg:
            return Decimal("0")
        return abs(self._to_decimal(gross_value)) * self.tds_rate

    def _execution_price(self, mark_price: Decimal, side: PositionSide, is_entry: bool, setup: Optional[dict] = None) -> Decimal:
        setup = setup or {}
        if self.paper_spread_coeff is not None:
            # F4: model CoinDCX's wider spread per side instead of the small fixed bump.
            bump = self.paper_spread_coeff
        else:
            spread_bps = self._to_decimal(setup.get("spread_bps", 2.0))
            slippage_bps = self._to_decimal(setup.get("slippage_bps", 3.0))
            bump = (spread_bps + slippage_bps) / Decimal("10000")
        if is_entry:
            return mark_price * (1 + bump) if side == PositionSide.LONG else mark_price * (1 - bump)
        return mark_price * (1 - bump) if side == PositionSide.LONG else mark_price * (1 + bump)

    def _is_liquidation_required(self, pos: EnhancedPosition, mark_price: Decimal) -> bool:
        pos.update_pnl(mark_price)
        equity = (pos.margin_used + pos.unrealized_pnl)
        maintenance = pos.notional * self.maintenance_margin_ratio
        return equity < maintenance

    def liquidation_price(self, pos: EnhancedPosition) -> Decimal:
        """Estimated liquidation price, consistent with ``_is_liquidation_required``
        (dynamic margin = notional/leverage at mark, plus PnL, vs notional*mmr).

        LONG  liquidates as price falls to  entry / (1 + 1/lev - mmr)
        SHORT liquidates as price rises to  entry / (1 - 1/lev + mmr)
        Returns 0 when undefined (e.g. zero leverage/entry).
        """
        entry = self._to_decimal(pos.entry_price)
        lev = self._to_decimal(pos.leverage)
        mmr = self.maintenance_margin_ratio
        if entry <= 0 or lev <= 0:
            return Decimal("0")
        inv_lev = Decimal("1") / lev
        denom = (Decimal("1") + inv_lev - mmr) if pos.side == PositionSide.LONG \
            else (Decimal("1") - inv_lev + mmr)
        if denom <= 0:
            return Decimal("0")
        return entry / denom
    def reset(self):
        with self.lock:
            self.positions.clear()
            self.position_history.clear()
            self.wallet_balance = self._to_decimal(1_000.0)
            self.unrealized_pnl_total = Decimal("0")
            self.realized_pnl_total = Decimal("0")
            if self.state_file.exists():
                self.state_file.unlink()
            logger.info("Wallet state reset")


class ExecutionEngine(Protocol):
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
    ) -> Order:
        ...

    def cancel_order(self, order_id: str) -> bool:
        ...

    def sync_positions(self) -> Dict[str, dict]:
        ...


class PaperExecutionEngine:
    def __init__(self, wallet: EnhancedFuturesWallet):
        self.wallet = wallet
        from .exchanges.instrument_mapper import InstrumentSpec
        class MockMapper:
            def get_spec(self, symbol: str) -> InstrumentSpec:
                return InstrumentSpec(
                    internal_symbol=symbol,
                    pair=symbol,
                    price_increment=Decimal("0.0001"),
                    quantity_increment=Decimal("0.001"),
                    min_quantity=Decimal("0.001"),
                    min_notional=Decimal("6.0"),
                    max_leverage=20,
                    maker_fee_rate=0.0002,
                    taker_fee_rate=0.0005,
                    status="active",
                )
        self.mapper = MockMapper()

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
        leverage: Optional[float] = None,  # accepted for API parity; paper has no venue leverage
    ) -> Order:
        return self.wallet.place_pending_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            trigger_price=trigger_price,
            limit_price=limit_price,
            reduce_only=reduce_only,
            expires_at=expires_at,
        )

    def cancel_order(self, order_id: str) -> bool:
        return self.wallet.cancel_order(order_id)

    def sync_positions(self) -> Dict[str, dict]:
        with self.wallet.lock:
            return {
                s: {
                    "symbol": p.symbol,
                    "side": p.side.value,
                    "quantity": p.remaining_quantity,
                    "entry_price": p.entry_price,
                }
                for s, p in self.wallet.positions.items()
                if p.status == "OPEN"
            }

