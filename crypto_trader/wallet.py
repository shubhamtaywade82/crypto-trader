"""
crypto_trader.wallet — Futures Position & Account Manager
==========================================================
Tracks positions, PnL, margin, and persists state to disk.
Supports partial closes, trailing stops, and time stops.
"""

import json
import time
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Literal, Tuple
from pathlib import Path
from enum import Enum

logger = logging.getLogger("crypto_trader.wallet")

DATA_DIR = Path.home() / ".crypto_trader"
DATA_DIR.mkdir(exist_ok=True)


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
    entry_price: float
    original_quantity: float
    remaining_quantity: float
    notional: float
    margin_used: float
    leverage: int
    open_time: int  # candle close_time ms, NOT wall clock

    sl_price: float
    tp_levels: List[dict] = field(default_factory=list)
    trailing_active: bool = False
    trailing_stop_price: Optional[float] = None
    time_stop_hours: int = 18

    unrealized_pnl: float = 0.0
    partial_realized_pnl: float = 0.0
    status: Literal["OPEN", "CLOSED"] = "OPEN"
    close_time: Optional[int] = None
    close_price: Optional[float] = None
    close_reason: Optional[str] = None

    def update_pnl(self, mark_price: float):
        if self.status != "OPEN" or mark_price <= 0:
            return
        if self.side == PositionSide.LONG:
            self.unrealized_pnl = (mark_price - self.entry_price) * self.remaining_quantity
        else:
            self.unrealized_pnl = (self.entry_price - mark_price) * self.remaining_quantity

    @property
    def total_realized_pnl(self) -> float:
        return self.partial_realized_pnl + (self.unrealized_pnl if self.status == "CLOSED" else 0)

    @property
    def current_margin_pnl_pct(self) -> float:
        if self.margin_used == 0:
            return 0.0
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
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class EnhancedFuturesWallet:
    """Broker-like futures wallet with position tracking and persistence."""

    def __init__(
        self,
        symbol: str,
        initial_balance: float = 1_000.0,
        leverage: int = 10,
        equity_utilization: float = 0.50,
        catastrophic_sl_pct: float = -0.50,
    ):
        self.symbol = symbol
        self.leverage = leverage
        self.equity_utilization = equity_utilization
        self.catastrophic_sl_pct = catastrophic_sl_pct
        self.state_file = DATA_DIR / f"wallet_{symbol}.json"

        self.wallet_balance: float = initial_balance
        self.unrealized_pnl_total: float = 0.0
        self.realized_pnl_total: float = 0.0
        self.positions: Dict[str, EnhancedPosition] = {}
        self.position_history: List[dict] = []

        self._load_state()

    @property
    def margin_balance(self) -> float:
        return self.wallet_balance + self.unrealized_pnl_total

    @property
    def available_balance(self) -> float:
        used = sum(p.margin_used for p in self.positions.values() if p.status == "OPEN")
        return max(0.0, self.margin_balance - used)

    def get_open_position(self) -> Optional[EnhancedPosition]:
        pos = self.positions.get(self.symbol)
        return pos if pos and pos.status == "OPEN" else None

    def can_open(self) -> Tuple[bool, str]:
        if self.get_open_position():
            return False, f"Already have open position on {self.symbol}"
        if self.available_balance <= 0:
            return False, "No available balance"
        return True, "OK"

    def open_position(
        self,
        setup: dict,
        mark_price: float,
        custom_margin: Optional[float] = None,
    ) -> Optional[EnhancedPosition]:
        """Open a position. custom_margin overrides equity_utilization for LLM-adjusted sizing."""
        can, reason = self.can_open()
        if not can:
            logger.info(f"[OPEN BLOCKED] {self.symbol}: {reason}")
            return None

        margin = custom_margin if custom_margin is not None else self.available_balance * self.equity_utilization
        if margin <= 0:
            return None

        notional = margin * self.leverage
        entry = setup["entry_price"]
        qty = notional / entry

        side = setup["side"]
        playbook = setup["playbook"]

        tp_levels = setup.get("tp_levels", [])
        if not tp_levels and "tp_price" in setup:
            tp_levels = [{"price": setup["tp_price"], "pct": 1.0, "hit": False, "label": "TP"}]

        pos = EnhancedPosition(
            symbol=self.symbol,
            side=side,
            playbook=playbook,
            entry_price=entry,
            original_quantity=qty,
            remaining_quantity=qty,
            notional=notional,
            margin_used=margin,
            leverage=self.leverage,
            open_time=setup.get("candle_close_time", int(time.time() * 1000)),
            sl_price=setup["sl_price"],
            tp_levels=tp_levels,
            time_stop_hours=setup.get("time_stop_hours", 18),
        )
        pos.update_pnl(mark_price)
        self.positions[self.symbol] = pos
        self._sync_unrealized_total()

        logger.info(
            f"[POSITION OPENED] {self.symbol} {side.value} | Playbook={playbook.value} | "
            f"Qty={qty:.4f} @ {entry:.2f} | Margin={margin:.2f} | "
            f"SL={setup['sl_price']:.2f} | TP={tp_levels}"
        )
        self._save_state()
        return pos

    def partial_close(self, mark_price: float, pct: float, reason: str) -> float:
        """Close a percentage of the position. Returns realized PnL from this slice."""
        pos = self.get_open_position()
        if not pos:
            return 0.0

        qty_to_close = pos.remaining_quantity * pct
        if qty_to_close <= 0:
            return 0.0

        if pos.side == PositionSide.LONG:
            pnl_slice = (mark_price - pos.entry_price) * qty_to_close
        else:
            pnl_slice = (pos.entry_price - mark_price) * qty_to_close

        pos.partial_realized_pnl += pnl_slice
        pos.remaining_quantity -= qty_to_close
        pos.notional = pos.remaining_quantity * pos.entry_price
        pos.margin_used = pos.notional / pos.leverage

        # Credit the slice immediately
        self.wallet_balance += pnl_slice
        self.realized_pnl_total += pnl_slice

        logger.info(
            f"[PARTIAL CLOSE] {self.symbol} {pos.side.value} | Closed {pct*100:.0f}% | "
            f"Qty={qty_to_close:.4f} | PnL={pnl_slice:.2f} | Reason={reason}"
        )

        if pos.remaining_quantity <= 0.0001:
            return self.close_position(mark_price, reason="FULL_VIA_PARTIALS")

        self._save_state()
        return pnl_slice

    def close_position(self, mark_price: float, reason: str) -> Optional[EnhancedPosition]:
        """Close full position. Only credits remaining unrealized (partials already credited)."""
        pos = self.get_open_position()
        if not pos:
            return None

        pos.update_pnl(mark_price)
        remaining_pnl = pos.unrealized_pnl  # Only the still-open portion

        # Credit remaining PnL
        self.wallet_balance += remaining_pnl
        self.realized_pnl_total += remaining_pnl

        pos.unrealized_pnl = 0.0
        pos.status = "CLOSED"
        pos.close_time = int(time.time() * 1000)
        pos.close_price = mark_price
        pos.close_reason = reason

        self.position_history.append(pos.to_dict())
        del self.positions[self.symbol]

        logger.info(
            f"[POSITION CLOSED] {self.symbol} {pos.side.value} | "
            f"Close={mark_price:.2f} | Remaining PnL={remaining_pnl:.2f} | Total Trade PnL={pos.partial_realized_pnl + remaining_pnl:.2f} | Reason={reason}"
        )
        self._save_state()
        return pos

    def update_positions(
        self,
        mark_price: float,
        candle_close_time: int,
        ema9_1h: Optional[float] = None,
    ):
        """Update PnL and check all exit conditions."""
        pos = self.get_open_position()
        if not pos:
            self.unrealized_pnl_total = 0.0
            return

        pos.update_pnl(mark_price)
        pnl = pos.unrealized_pnl

        # 1. Catastrophic SL (-50% margin)
        cat_sl = pos.margin_used * self.catastrophic_sl_pct
        if pnl <= cat_sl:
            self.close_position(mark_price, reason=f"CATASTROPHIC_SL ({pnl:.2f})")
            return

        # 2. Playbook-specific exits
        if pos.playbook == Playbook.INTRADAY:
            self._check_intraday_exits(pos, mark_price, candle_close_time)
        elif pos.playbook == Playbook.SWING:
            self._check_swing_exits(pos, mark_price, candle_close_time, ema9_1h)

        self._sync_unrealized_total()

    def _check_intraday_exits(self, pos: EnhancedPosition, mark_price: float, candle_close_time: int):
        # Simple TP/SL
        if pos.side == PositionSide.LONG:
            if mark_price >= pos.tp_levels[0]["price"]:
                self.close_position(mark_price, reason=f"TP_HIT ({pos.unrealized_pnl:.2f})")
                return
            if mark_price <= pos.sl_price:
                self.close_position(mark_price, reason=f"SL_HIT ({pos.unrealized_pnl:.2f})")
                return
        else:
            if mark_price <= pos.tp_levels[0]["price"]:
                self.close_position(mark_price, reason=f"TP_HIT ({pos.unrealized_pnl:.2f})")
                return
            if mark_price >= pos.sl_price:
                self.close_position(mark_price, reason=f"SL_HIT ({pos.unrealized_pnl:.2f})")
                return

        # Time stop (using candle time, not wall clock)
        hours_open = (candle_close_time - pos.open_time) / 3_600_000
        if hours_open >= pos.time_stop_hours:
            self.close_position(mark_price, reason=f"TIME_STOP ({hours_open:.1f}h)")

    def _check_swing_exits(self, pos: EnhancedPosition, mark_price: float, candle_close_time: int, ema9_1h: Optional[float]):
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
                self.partial_close(mark_price, tp["pct"], reason=f"{tp['label']} HIT")
                if tp["label"] == "TP1":
                    pos.sl_price = pos.entry_price
                    logger.info(f"[SL ADJUSTED] {self.symbol} SL → BREAKEVEN")
                elif tp["label"] == "TP2":
                    pos.trailing_active = True
                    logger.info(f"[TRAIL ACTIVE] {self.symbol} trailing on 1H EMA9")
                return  # Only one TP per tick

        # SL check
        if pos.side == PositionSide.LONG and mark_price <= pos.sl_price:
            self.close_position(mark_price, reason=f"SL_HIT ({pos.unrealized_pnl:.2f})")
            return
        elif pos.side == PositionSide.SHORT and mark_price >= pos.sl_price:
            self.close_position(mark_price, reason=f"SL_HIT ({pos.unrealized_pnl:.2f})")
            return

        # Trailing stop
        if pos.trailing_active and ema9_1h is not None:
            if pos.side == PositionSide.LONG and mark_price < ema9_1h:
                self.close_position(mark_price, reason=f"TRAIL_STOP (EMA9 {ema9_1h:.2f})")
                return
            elif pos.side == PositionSide.SHORT and mark_price > ema9_1h:
                self.close_position(mark_price, reason=f"TRAIL_STOP (EMA9 {ema9_1h:.2f})")
                return

        # Time stop
        hours_open = (candle_close_time - pos.open_time) / 3_600_000
        if hours_open >= pos.time_stop_hours:
            self.close_position(mark_price, reason=f"TIME_STOP ({hours_open:.1f}h)")

    def get_summary(self) -> dict:
        open_pos = [p.to_dict() for p in self.positions.values() if p.status == "OPEN"]
        return {
            "wallet_balance": round(self.wallet_balance, 4),
            "unrealized_pnl": round(self.unrealized_pnl_total, 4),
            "realized_pnl": round(self.realized_pnl_total, 4),
            "margin_balance": round(self.margin_balance, 4),
            "available": round(self.available_balance, 4),
            "open_count": len(open_pos),
            "open_positions": open_pos,
            "history_count": len(self.position_history),
        }

    def print_summary(self):
        s = self.get_summary()
        print("\n" + "=" * 65)
        print("  CRYPTO TRADER v4 — WALLET SUMMARY")
        print("=" * 65)
        print(f"  Wallet Balance    : {s['wallet_balance']:.4f} USDT")
        print(f"  Unrealized PnL    : {s['unrealized_pnl']:.4f} USDT")
        print(f"  Realized PnL      : {s['realized_pnl']:.4f} USDT")
        print(f"  Margin Balance    : {s['margin_balance']:.4f} USDT")
        print(f"  Available         : {s['available']:.4f} USDT")
        print(f"  Open Positions    : {s['open_count']}")
        for p in s["open_positions"]:
            print(f"    → {p['symbol']} {p['side']} | Playbook={p['playbook']} | "
                  f"Entry={p['entry_price']:.2f} | RemQty={p['remaining_quantity']:.4f} | "
                  f"U-PnL={p['unrealized_pnl']:.4f} ({p['unrealized_pnl']/p['margin_used']*100:.2f}%)")
        print("=" * 65 + "\n")

    def _save_state(self):
        state = {
            "wallet_balance": self.wallet_balance,
            "realized_pnl_total": self.realized_pnl_total,
            "positions": {s: p.to_dict() for s, p in self.positions.items()},
            "position_history": self.position_history,
        }
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2, default=str)

    def _sync_unrealized_total(self):
        self.unrealized_pnl_total = sum(
            p.unrealized_pnl for p in self.positions.values() if p.status == "OPEN"
        )

    def _load_state(self):
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file, "r") as f:
                state = json.load(f)
            self.wallet_balance = state.get("wallet_balance", self.wallet_balance)
            self.realized_pnl_total = state.get("realized_pnl_total", 0.0)
            self.positions = {
                s: EnhancedPosition.from_dict(d)
                for s, d in state.get("positions", {}).items()
                if d.get("status") == "OPEN"  # BUG FIX: skip closed positions
            }
            self.position_history = state.get("position_history", [])
            self._sync_unrealized_total()
            logger.info(f"Loaded wallet state from {self.state_file}")
        except Exception as e:
            logger.warning(f"Failed to load state: {e}")

    def reset(self):
        self.positions.clear()
        self.position_history.clear()
        self.wallet_balance = 1_000.0
        self.unrealized_pnl_total = 0.0
        self.realized_pnl_total = 0.0
        if self.state_file.exists():
            self.state_file.unlink()
        logger.info("Wallet state reset")
