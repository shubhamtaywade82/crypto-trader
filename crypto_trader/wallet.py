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

        self.wallet_balance: Decimal = self._to_decimal(initial_balance)
        self.unrealized_pnl_total: Decimal = Decimal("0")
        self.realized_pnl_total: Decimal = Decimal("0")
        self.positions: Dict[str, EnhancedPosition] = {}
        self.position_history: List[dict] = []
        self.lock = threading.RLock()

        self._load_state()

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

            tp_levels = setup.get("tp_levels", [])
            if not tp_levels and "tp_price" in setup:
                tp_levels = [{"price": setup["tp_price"], "pct": 1.0, "hit": False, "label": "TP"}]

            pos = EnhancedPosition(
                symbol=symbol,
                side=side,
                playbook=playbook,
                entry_price=entry,
                original_quantity=qty,
                remaining_quantity=qty,
                notional=notional,
                margin_used=margin,
                leverage=self.leverage,
                open_time=setup.get("candle_close_time", self._now_ms()),
                sl_price=setup["sl_price"],
                tp_levels=tp_levels,
                time_stop_hours=setup.get("time_stop_hours", 18),
            )
            execution_price = self._execution_price(mark_price, side, is_entry=True, setup=setup)
            fee_open = self._calculate_fee(execution_price * qty, is_taker=True)

            pos.entry_price = execution_price
            pos.update_pnl(mark_price)
            self.wallet_balance -= fee_open
            self.realized_pnl_total -= fee_open
            self.positions[symbol] = pos
            self._sync_unrealized_total()

            logger.info(
                f"[POSITION OPENED] {symbol} {side.value} | Playbook={playbook.value} | "
                f"Qty={qty:.4f} @ {entry:.2f} | Margin={margin:.2f} | "
                f"SL={setup['sl_price']:.2f} | TP={tp_levels}"
            )
            self._save_state()
            self._append_event(
                "POSITION_OPENED",
                {
                    "symbol": symbol,
                    "side": side.value,
                    "entry_price": entry,
                    "quantity": qty,
                    "margin": margin,
                    "execution_price": execution_price,
                    "fee": fee_open,
                },
            )
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

            pos.partial_realized_pnl += pnl_slice
            pos.remaining_quantity -= qty_to_close
            pos.notional = pos.remaining_quantity * pos.entry_price
            pos.margin_used = pos.notional / pos.leverage

            fee_close = self._calculate_fee(execution_price * qty_to_close, is_taker=True)

            # Credit the slice immediately (net fees)
            self.wallet_balance += (pnl_slice - fee_close)
            self.realized_pnl_total += (pnl_slice - fee_close)

            logger.info(
                f"[PARTIAL CLOSE] {symbol} {pos.side.value} | Closed {pct*100:.0f}% | "
                f"Qty={qty_to_close:.4f} | PnL={pnl_slice:.2f} | Reason={reason}"
            )

            if pos.remaining_quantity <= Decimal("0.0001"):
                return self.close_position(symbol, mark_price, reason="FULL_VIA_PARTIALS")

            self._save_state()
            self._append_event(
                "POSITION_PARTIALLY_CLOSED",
                {
                    "symbol": symbol,
                    "reason": reason,
                    "pct": pct,
                    "price": execution_price,
                    "pnl": pnl_slice,
                    "fee": fee_close,
                },
            )
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
            self.wallet_balance += (remaining_pnl - fee_close)
            self.realized_pnl_total += (remaining_pnl - fee_close)

            pos.unrealized_pnl = Decimal("0")
            pos.status = "CLOSED"
            pos.close_time = self._now_ms()
            pos.close_price = execution_price
            pos.close_reason = reason

            self.position_history.append(pos.to_dict())
            del self.positions[symbol]

            logger.info(
                f"[POSITION CLOSED] {symbol} {pos.side.value} | "
                f"Close={execution_price:.2f} | Remaining PnL={remaining_pnl:.2f} | "
                f"Total Trade PnL={pos.partial_realized_pnl + remaining_pnl:.2f} | Reason={reason}"
            )
            self._save_state()
            self._append_event(
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
        if not self.state_file.exists():
            return
        try:
            state = self._load_state_with_recovery()
            self.wallet_balance = self._to_decimal(state.get("wallet_balance", self.wallet_balance))
            self.realized_pnl_total = self._to_decimal(state.get("realized_pnl_total", Decimal("0")))
            self.maker_fee_rate = state.get("maker_fee_rate", self.maker_fee_rate)
            self.taker_fee_rate = state.get("taker_fee_rate", self.taker_fee_rate)
            self.maintenance_margin_ratio = state.get("maintenance_margin_ratio", self.maintenance_margin_ratio)
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
