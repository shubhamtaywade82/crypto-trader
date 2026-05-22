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
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Literal, Tuple, Callable
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

class PortfolioReducer:
    """Canonical event reducer for replay-derived portfolio state."""

    def apply(self, state: PortfolioState, event: dict):
        et = event.get("event_type")
        payload = event.get("payload", {})
        state.last_ts = int(event.get("ts", state.last_ts))

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
                order_ref["status"] = payload.get("status", OrderStatus.FILLED.value)
            state.fills.append(payload)
            state.wallet_balance -= fee
            state.realized_pnl_total -= fee
            return

        if et == "ORDER_CANCELLED":
            oid = payload.get("order_id")
            if oid and oid in state.orders:
                state.orders[oid]["status"] = OrderStatus.CANCELLED.value
            return

        if et == "ORDER_REJECTED":
            oid = payload.get("order_id")
            if oid and oid in state.orders:
                state.orders[oid]["status"] = OrderStatus.REJECTED.value
            return
        
        if et == "ORDER_EXPIRED":
            oid = payload.get("order_id")
            if oid and oid in state.orders:
                state.orders[oid]["status"] = OrderStatus.EXPIRED.value
            return

        if et == "POSITION_OPENED":
            symbol = payload.get("symbol")
            if symbol:
                state.open_positions[symbol] = {
                    "entry_price": Decimal(str(payload.get("execution_price", payload.get("entry_price", "0")))),
                    "quantity": Decimal(str(payload.get("quantity", "0"))),
                    "margin": Decimal(str(payload.get("margin", "0"))),
                    "side": payload.get("side"),
                    "playbook": payload.get("playbook", "INTRADAY"),
                    "sl_price": Decimal(str(payload.get("sl_price", "0"))),
                    "leverage": int(payload.get("leverage", 1)),
                    "open_time": int(payload.get("open_time", 0)),
                }
            return

        if et == "POSITION_PARTIALLY_CLOSED":
            symbol = payload.get("symbol")
            pnl = Decimal(str(payload.get("pnl", "0")))
            fee = Decimal(str(payload.get("fee", "0")))
            state.wallet_balance += (pnl - fee)
            state.realized_pnl_total += (pnl - fee)
            if symbol in state.open_positions:
                qty_closed = Decimal(str(payload.get("closed_qty", "0")))
                state.open_positions[symbol]["quantity"] = max(
                    Decimal("0"),
                    state.open_positions[symbol]["quantity"] - qty_closed,
                )
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

    def update_pnl(self, mark_price: Decimal):
        if self.status != "OPEN" or mark_price <= Decimal("0"):
            return
        
        # Calculate dynamic notional and margin_used based on current mark_price
        self.notional = self.remaining_quantity * mark_price
        self.margin_used = self.notional / self.leverage

        if self.side == PositionSide.LONG:
            self.unrealized_pnl = (mark_price - self.entry_price) * self.remaining_quantity
        else:
            self.unrealized_pnl = (self.entry_price - mark_price) * self.remaining_quantity

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
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EnhancedPosition":
        data["side"] = PositionSide(data["side"])
        data["playbook"] = Playbook(data["playbook"])
        for k in ["entry_price", "original_quantity", "remaining_quantity", "notional", "margin_used", "sl_price", "unrealized_pnl", "partial_realized_pnl", "close_price"]:
            if k in data and data[k] is not None:
                data[k] = Decimal(str(data[k]))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


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
        self._now_ms_fn = now_ms_fn or (lambda: int(time.time() * 1000))
        safe_ns = self._sanitize_path_component(self.state_namespace)
        safe_symbol = self._sanitize_path_component(self.symbol)
        self.state_file = DATA_DIR / f"wallet_{safe_ns}_{safe_symbol}.json"
        self.backup_state_file = self.state_file.with_suffix(".bak.json")
        self.events_file = DATA_DIR / f"wallet_events_{safe_ns}_{safe_symbol}.jsonl"
        self.db_file = DATA_DIR / f"wallet_{safe_ns}_{safe_symbol}.db"

        self.wallet_balance: Decimal = self._to_decimal(initial_balance)
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
        self._runtime_reducer_state = PortfolioState(
            wallet_balance=self.wallet_balance,
            realized_pnl_total=self.realized_pnl_total,
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
            target = symbol or self.symbol
            if self.get_open_position(target):
                return False, f"Already have open position on {target}"
            if self.available_balance <= 0:
                return False, "No available balance"
            return True, "OK"

    def open_position(
        self,
        symbol: str,
        setup: dict,
        mark_price: float,
        custom_margin: Optional[float] = None,
        custom_quantity: Optional[float] = None,
    ) -> Optional[EnhancedPosition]:
        """Open a position. custom_margin overrides equity_utilization for LLM-adjusted sizing."""
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

            order = self._create_order(symbol=symbol, side=side, quantity=qty)
            execution_price = self._execution_price(mark_price, side, is_entry=True, setup=setup)
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
            self.positions[symbol] = pos
            self._sync_unrealized_total()

            logger.info(
                f"[POSITION OPENED] {symbol} {side.value} | Playbook={playbook.value} | "
                f"Qty={qty:.4f} @ {entry:.2f} | Margin={margin:.2f} | "
                f"SL={sl_price:.2f} | TP={tp_levels}"
            )
            self._save_state()
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
                },
            )
            self._sync_positions_from_reducer()
            return pos

    def partial_close(self, symbol: str, mark_price: float, pct: float, reason: str) -> Decimal:
        """Close a percentage of the position. Returns realized PnL from this slice."""
        with self.lock:
            mark_price = self._to_decimal(mark_price)
            pos = self.get_open_position(symbol)
            if not pos:
                return Decimal("0")

            qty_to_close = pos.remaining_quantity * self._to_decimal(pct)
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
                },
            )
            self._sync_positions_from_reducer()

            logger.info(
                f"[PARTIAL CLOSE] {symbol} {pos.side.value} | Closed {pct*100:.0f}% | "
                f"Qty={qty_to_close:.4f} | PnL={pnl_slice:.2f} | Reason={reason}"
            )

            if pos.remaining_quantity <= Decimal("0.0001"):
                return self.close_position(symbol, mark_price, reason="FULL_VIA_PARTIALS")

            self._save_state()
            return pnl_slice

    def close_position(self, symbol: str, mark_price: float, reason: str) -> Optional[EnhancedPosition]:
        """Close full position. Only credits remaining unrealized (partials already credited)."""
        with self.lock:
            mark_price = self._to_decimal(mark_price)
            pos = self.get_open_position(symbol)
            if not pos:
                return None

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

            pos.unrealized_pnl = Decimal("0")
            pos.status = "CLOSED"
            pos.close_time = self._now_ms()
            pos.close_price = execution_price
            pos.close_reason = reason
            self.position_history.append(pos.to_dict())

            logger.info(
                f"[POSITION CLOSED] {symbol} {pos.side.value} | "
                f"Close={execution_price:.2f} | Remaining PnL={remaining_pnl:.2f} | "
                f"Total Trade PnL={pos.partial_realized_pnl + remaining_pnl:.2f} | Reason={reason}"
            )
            self._save_state()
            self._sync_positions_from_reducer()
            return pos

    def update_positions(
        self,
        symbol: str,
        mark_price: float,
        candle_close_time: int,
        ema9_1h: Optional[float] = None,
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
                self.close_position(symbol, mark_price, reason="LIQUIDATION")
                return
            if pnl <= cat_sl:
                self.close_position(symbol, mark_price, reason=f"CATASTROPHIC_SL ({pnl:.2f})")
                return

            # 2. Playbook-specific exits
            if pos.playbook == Playbook.INTRADAY:
                self._check_intraday_exits(symbol, pos, mark_price, candle_close_time)
            elif pos.playbook == Playbook.SWING:
                self._check_swing_exits(symbol, pos, mark_price, candle_close_time, ema9_1h)

            self._sync_unrealized_total()

    def _check_intraday_exits(self, symbol: str, pos: EnhancedPosition, mark_price: float, candle_close_time: int):
        # Simple TP/SL
        if pos.side == PositionSide.LONG:
            if mark_price >= pos.tp_levels[0]["price"]:
                self.close_position(symbol, mark_price, reason=f"TP_HIT ({pos.unrealized_pnl:.2f})")
                return
            if mark_price <= pos.sl_price:
                self.close_position(symbol, mark_price, reason=f"SL_HIT ({pos.unrealized_pnl:.2f})")
                return
        else:
            if mark_price <= pos.tp_levels[0]["price"]:
                self.close_position(symbol, mark_price, reason=f"TP_HIT ({pos.unrealized_pnl:.2f})")
                return
            if mark_price >= pos.sl_price:
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
                tp["hit"] = True
                self.partial_close(symbol, mark_price, tp["pct"], reason=f"{tp['label']} HIT")
                if tp["label"] == "TP1":
                    pos.sl_price = pos.entry_price
                    logger.info(f"[SL ADJUSTED] {symbol} SL → BREAKEVEN")
                elif tp["label"] == "TP2":
                    pos.trailing_active = True
                    logger.info(f"[TRAIL ACTIVE] {symbol} trailing on 1H EMA9")
                return  # Only one TP per tick

        # SL check
        if pos.side == PositionSide.LONG and mark_price <= pos.sl_price:
            self.close_position(symbol, mark_price, reason=f"SL_HIT ({pos.unrealized_pnl:.2f})")
            return
        elif pos.side == PositionSide.SHORT and mark_price >= pos.sl_price:
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

    def _save_state(self):
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
        }
        self._atomic_write_json(self.state_file, state)
        with sqlite3.connect(self.db_file) as conn:
            conn.execute("INSERT INTO snapshots (ts, wallet_balance, realized_pnl_total, state_json) VALUES (?, ?, ?, ?)", (self._now_ms(), str(self.wallet_balance), str(self.realized_pnl_total), json.dumps(state, default=self._serialize_decimals)))
            conn.commit()

    def _atomic_write_json(self, path: Path, state: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        payload = json.dumps(state, indent=2, default=self._serialize_decimals)
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
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_file) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                symbol TEXT,
                namespace TEXT,
                payload_json TEXT NOT NULL
            )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                quantity TEXT NOT NULL,
                filled_quantity TEXT NOT NULL,
                avg_fill_price TEXT NOT NULL,
                status TEXT NOT NULL,
                reduce_only INTEGER NOT NULL DEFAULT 0,
                trigger_price TEXT,
                limit_price TEXT,
                expires_at INTEGER,
                updated_ts INTEGER NOT NULL
            )
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity TEXT NOT NULL,
                fill_price TEXT NOT NULL,
                fee TEXT NOT NULL,
                ts INTEGER NOT NULL,
                sequence INTEGER NOT NULL
            )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_fills_order_id ON fills(order_id)")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS funding (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                rate TEXT NOT NULL,
                notional TEXT NOT NULL,
                amount TEXT NOT NULL,
                ts INTEGER NOT NULL
            )
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS fees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                reason TEXT,
                amount TEXT NOT NULL,
                ts INTEGER NOT NULL
            )
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                wallet_balance TEXT NOT NULL,
                realized_pnl_total TEXT NOT NULL,
                state_json TEXT NOT NULL
            )
            """)
            conn.commit()

    def _append_event_db(self, event: dict):
        payload_json = json.dumps(event.get("payload", {}), default=self._serialize_decimals)
        with sqlite3.connect(self.db_file) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                "INSERT INTO events (ts, event_type, symbol, namespace, payload_json) VALUES (?, ?, ?, ?, ?)",
                (
                    int(event.get("ts", 0)),
                    event.get("event_type"),
                    event.get("symbol"),
                    event.get("namespace"),
                    payload_json,
                ),
            )
            conn.commit()

    def _iter_events(self):
        if self.db_file.exists():
            with sqlite3.connect(self.db_file) as conn:
                rows = conn.execute(
                    "SELECT ts, event_type, symbol, namespace, payload_json FROM events ORDER BY ts ASC, id ASC"
                ).fetchall()
            for ts, et, sym, ns, payload_json in rows:
                yield {"ts": ts, "event_type": et, "symbol": sym, "namespace": ns, "payload": json.loads(payload_json)}
            return

        if self.events_file.exists():
            with open(self.events_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        yield json.loads(line)

    def _append_event(self, event_type: str, payload: dict):
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

    def _emit_event(self, event_type: str, payload: dict):
        """Authoritative runtime transition: append event, then apply reducer-derived runtime updates."""
        self._append_event(event_type, payload)
        event = {
            "ts": self._now_ms(),
            "event_type": event_type,
            "symbol": self.symbol,
            "namespace": self.state_namespace,
            "payload": payload,
        }
        self._persist_domain_event_to_db(event)
        self.reducer.apply(self._runtime_reducer_state, event)
        self.wallet_balance = self._runtime_reducer_state.wallet_balance
        self.realized_pnl_total = self._runtime_reducer_state.realized_pnl_total
        self._sync_orders_from_reducer()
        self._sync_fills_from_reducer()

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
                existing.notional = existing.remaining_quantity * existing.entry_price
                existing.margin_used = existing.notional / existing.leverage
                current[symbol] = existing
            else:
                side = PositionSide(pdata.get("side", "LONG"))
                playbook = Playbook(pdata.get("playbook", "INTRADAY"))
                qty = pdata["quantity"]
                entry = pdata["entry_price"]
                lev = int(pdata.get("leverage", self.leverage))
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
                    tp_levels=[],
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
            return amount

    def _run_invariant_checks(self):
        # 1) order filled qty <= order qty
        for o in self.orders.values():
            if o.filled_quantity > o.quantity:
                self._emit_event(
                    "INVARIANT_VIOLATION",
                    {"type": "ORDER_OVERFILLED", "severity": "CRITICAL", "order_id": o.id, "filled": o.filled_quantity, "qty": o.quantity},
                )
        # 2) remaining qty >= 0
        for o in self.orders.values():
            remaining = o.quantity - o.filled_quantity
            if remaining < Decimal("0"):
                self._emit_event(
                    "INVARIANT_VIOLATION",
                    {"type": "NEGATIVE_REMAINING_QTY", "severity": "CRITICAL", "order_id": o.id, "remaining": remaining},
                )
        # 3) available margin >= 0
        if self.available_balance < Decimal("0"):
            self._emit_event(
                "INVARIANT_VIOLATION",
                {"type": "NEGATIVE_AVAILABLE_MARGIN", "severity": "CRITICAL", "available": self.available_balance},
            )

    def run_funding_scheduler(self, symbol: str, funding_rate: Decimal, interval_ms: int, now_ms: Optional[int] = None) -> Decimal:
        """Apply funding if interval has elapsed since last application for symbol."""
        now = int(now_ms if now_ms is not None else self._now_ms())
        with sqlite3.connect(self.db_file) as conn:
            row = conn.execute("SELECT MAX(ts) FROM funding WHERE symbol = ?", (symbol,)).fetchone()
            last_ts = int(row[0]) if row and row[0] is not None else 0
        if now - last_ts < int(interval_ms):
            return Decimal("0")
        return self.apply_funding(symbol, self._to_decimal(funding_rate))

    def _record_fill(self, order: Order, quantity: Decimal, fill_price: Decimal, fee: Decimal, sequence: int = 1):
        prospective_filled = order.filled_quantity + quantity
        status = OrderStatus.FILLED if prospective_filled >= order.quantity else OrderStatus.PARTIALLY_FILLED
        self.fills.append(
            Fill(
                order_id=order.id,
                symbol=order.symbol,
                side=order.side,
                quantity=quantity,
                fill_price=fill_price,
                fee=fee,
                timestamp=self._now_ms(),
                sequence=sequence,
            )
        )
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

    def replay_portfolio_state(self) -> PortfolioState:
        """Rebuild a minimal portfolio view from event log only."""
        state = PortfolioState()
        if not self.events_file.exists():
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


    def _persist_domain_event_to_db(self, event: dict):
        et = event.get("event_type")
        payload = event.get("payload", {})
        ts = int(event.get("ts", self._now_ms()))
        with sqlite3.connect(self.db_file) as conn:
            if et == "ORDER_CREATED":
                conn.execute(
                    """
                    INSERT OR REPLACE INTO orders
                    (order_id, symbol, side, order_type, quantity, filled_quantity, avg_fill_price, status, reduce_only, trigger_price, limit_price, expires_at, updated_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload.get("order_id"), payload.get("symbol"), payload.get("side"), payload.get("order_type", "MARKET"),
                        str(payload.get("quantity", "0")), "0", "0", payload.get("status", "NEW"),
                        1 if payload.get("reduce_only", False) else 0,
                        str(payload.get("trigger_price")) if payload.get("trigger_price") is not None else None,
                        str(payload.get("limit_price")) if payload.get("limit_price") is not None else None,
                        payload.get("expires_at"), ts,
                    ),
                )
            elif et in ("ORDER_FILLED", "ORDER_PARTIALLY_FILLED", "ORDER_CANCELLED", "ORDER_REJECTED", "ORDER_EXPIRED"):
                oid = payload.get("order_id")
                if et in ("ORDER_FILLED", "ORDER_PARTIALLY_FILLED"):
                    conn.execute(
                        "INSERT INTO fills (order_id, symbol, side, quantity, fill_price, fee, ts, sequence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (oid, payload.get("symbol"), payload.get("side"), str(payload.get("quantity", "0")), str(payload.get("fill_price", "0")), str(payload.get("fee", "0")), ts, int(payload.get("sequence", 1))),
                    )
                row = conn.execute("SELECT quantity, filled_quantity FROM orders WHERE order_id = ?", (oid,)).fetchone()
                if row:
                    qty = Decimal(str(row[0])); filled = Decimal(str(row[1]))
                    if et in ("ORDER_FILLED", "ORDER_PARTIALLY_FILLED"):
                        filled += Decimal(str(payload.get("quantity", "0")))
                    status = payload.get("status") or ("CANCELLED" if et=="ORDER_CANCELLED" else "REJECTED" if et=="ORDER_REJECTED" else "EXPIRED" if et=="ORDER_EXPIRED" else "FILLED")
                    conn.execute("UPDATE orders SET filled_quantity=?, avg_fill_price=?, status=?, updated_ts=? WHERE order_id=?", (str(filled), str(payload.get("fill_price", "0")), status, ts, oid))
            elif et == "FUNDING_APPLIED":
                conn.execute("INSERT INTO funding (symbol, rate, notional, amount, ts) VALUES (?, ?, ?, ?, ?)", (payload.get("symbol"), str(payload.get("rate", "0")), str(payload.get("notional", "0")), str(payload.get("amount", "0")), ts))
            elif et == "FEE_CHARGED":
                conn.execute("INSERT INTO fees (symbol, reason, amount, ts) VALUES (?, ?, ?, ?)", (payload.get("symbol"), payload.get("reason"), str(payload.get("amount", "0")), ts))
            conn.commit()

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
        if self._load_from_db_snapshot_and_replay():
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
            self._sync_unrealized_total()
            logger.info(f"Loaded wallet state from {self.state_file}")
        except Exception as e:
            logger.warning(f"Failed to load state: {e}")

    def _load_from_db_snapshot_and_replay(self) -> bool:
        if not self.db_file.exists():
            return False
        try:
            with sqlite3.connect(self.db_file) as conn:
                row = conn.execute(
                    "SELECT ts, wallet_balance, realized_pnl_total, state_json FROM snapshots ORDER BY ts DESC LIMIT 1"
                ).fetchone()
            if not row:
                return False
            snap_ts, wallet_balance, realized_pnl_total, state_json = row
            snap = json.loads(state_json)
            self.wallet_balance = self._to_decimal(wallet_balance)
            self.realized_pnl_total = self._to_decimal(realized_pnl_total)
            self.positions = {
                s: EnhancedPosition.from_dict(d)
                for s, d in snap.get("positions", {}).items()
                if d.get("status") == "OPEN"
            }
            self.position_history = snap.get("position_history", [])
            replay_state = PortfolioState(wallet_balance=self.wallet_balance, realized_pnl_total=self.realized_pnl_total)
            for event in self._iter_events():
                if int(event.get("ts", 0)) > int(snap_ts):
                    self.reducer.apply(replay_state, event)
            self._runtime_reducer_state = replay_state
            self.wallet_balance = replay_state.wallet_balance
            self.realized_pnl_total = replay_state.realized_pnl_total
            self._sync_positions_from_reducer()
            self._sync_orders_from_reducer()
            self._sync_fills_from_reducer()
            self._sync_unrealized_total()
            return True
        except Exception as e:
            logger.warning(f"DB snapshot/replay load failed: {e}")
            return False

    def _load_state_with_recovery(self) -> dict:
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as primary_err:
            logger.warning(f"Primary wallet state load failed ({self.state_file}): {primary_err}")
            if self.backup_state_file.exists():
                with open(self.backup_state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                logger.warning(f"Recovered wallet state from backup file {self.backup_state_file}")
                return state
            raise



    def _now_ms(self) -> int:
        return int(self._now_ms_fn())

    def _calculate_fee(self, notional: Decimal, is_taker: bool = True) -> float:
        rate = self.taker_fee_rate if is_taker else self.maker_fee_rate
        return abs(notional) * rate

    def _execution_price(self, mark_price: Decimal, side: PositionSide, is_entry: bool, setup: Optional[dict] = None) -> Decimal:
        setup = setup or {}
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
        return equity <= maintenance
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
