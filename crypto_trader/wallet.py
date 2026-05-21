from __future__ import annotations
import json, time
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path

DATA_DIR = Path.home() / ".crypto_trader"
DATA_DIR.mkdir(parents=True, exist_ok=True)

class PositionSide(Enum):
    LONG = "LONG"
    SHORT = "SHORT"

@dataclass
class Position:
    symbol: str
    side: PositionSide
    entry_price: float
    quantity: float
    leverage: int
    open_time: int
    remaining_quantity: float
    partial_realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0

    def update_pnl(self, mark: float):
        sign = 1 if self.side == PositionSide.LONG else -1
        self.unrealized_pnl = (mark - self.entry_price) * self.remaining_quantity * sign

class Wallet:
    def __init__(self, symbol: str, initial_balance: float = 1000.0, leverage: int = 10):
        self.symbol = symbol
        self.balance = initial_balance
        self.leverage = leverage
        self.state_file = DATA_DIR / f"wallet_{symbol}.json"
        self.position: Position | None = None
        self._load()

    def open_position(self, side: PositionSide, entry: float, margin: float, custom_margin: float | None = None):
        use_margin = custom_margin if custom_margin is not None else margin
        qty = (use_margin * self.leverage) / entry
        self.position = Position(self.symbol, side, entry, qty, self.leverage, int(time.time()*1000), qty)
        self._save()

    def partial_close(self, price: float, pct: float):
        if not self.position: return 0.0
        qty = self.position.remaining_quantity * pct
        sign = 1 if self.position.side == PositionSide.LONG else -1
        pnl = (price - self.position.entry_price) * qty * sign
        self.position.partial_realized_pnl += pnl
        self.position.remaining_quantity -= qty
        self.balance += pnl
        self._save()
        return pnl

    def close_position(self, price: float):
        if not self.position: return 0.0
        self.position.update_pnl(price)
        realized = self.position.unrealized_pnl
        self.balance += realized
        self.position = None
        self._save()
        return realized

    def _save(self):
        pos_data = None
        if self.position:
            pos_data = {
                "symbol": self.position.symbol,
                "side": self.position.side.value,
                "entry_price": self.position.entry_price,
                "quantity": self.position.quantity,
                "leverage": self.position.leverage,
                "open_time": self.position.open_time,
                "remaining_quantity": self.position.remaining_quantity,
                "partial_realized_pnl": self.position.partial_realized_pnl,
                "unrealized_pnl": self.position.unrealized_pnl,
            }
        self.state_file.write_text(json.dumps({"balance": self.balance, "position": pos_data}))

    def _load(self):
        if not self.state_file.exists():
            return
        d = json.loads(self.state_file.read_text())
        self.balance = d.get("balance", self.balance)
        pos_data = d.get("position")
        if pos_data:
            self.position = Position(
                symbol=pos_data["symbol"],
                side=PositionSide(pos_data["side"]),
                entry_price=pos_data["entry_price"],
                quantity=pos_data["quantity"],
                leverage=pos_data["leverage"],
                open_time=pos_data["open_time"],
                remaining_quantity=pos_data["remaining_quantity"],
                partial_realized_pnl=pos_data.get("partial_realized_pnl", 0.0),
                unrealized_pnl=pos_data.get("unrealized_pnl", 0.0),
            )
