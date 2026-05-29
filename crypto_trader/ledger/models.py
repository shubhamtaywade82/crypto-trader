"""
Typed dataclass models for the ledger layer.
These are transport/domain objects — not ORM models.
All monetary fields are Decimal for precision.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Any


_D = Decimal
_ZERO = _D("0")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _uid() -> str:
    return str(uuid.uuid4())


# ─────────────────────────────────────────────────────────────
# Currency & Ledger
# ─────────────────────────────────────────────────────────────

@dataclass
class CurrencyAccount:
    currency: str
    ledger_balance: Decimal = _ZERO
    available_balance: Decimal = _ZERO
    locked_balance: Decimal = _ZERO
    user_id: str = "default"
    id: Optional[int] = None
    updated_at: int = field(default_factory=_now_ms)


@dataclass
class LedgerEntry:
    account_id: int
    currency: str
    direction: str          # 'debit' | 'credit'
    amount: Decimal
    running_balance: Decimal
    event_type: str
    reference_type: Optional[str] = None
    reference_id: Optional[str] = None
    rate_snapshot_id: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    id: Optional[int] = None
    created_at: int = field(default_factory=_now_ms)


@dataclass
class FxRate:
    from_currency: str
    to_currency: str
    bid: Decimal
    ask: Decimal
    mid: Decimal
    source: str
    id: Optional[int] = None
    captured_at: int = field(default_factory=_now_ms)

    def rate_for_buy(self) -> Decimal:
        """Conservative rate when spending from_currency to receive to_currency."""
        return self.ask

    def rate_for_sell(self) -> Decimal:
        """Conservative rate when selling to_currency to receive from_currency."""
        return self.bid

    def is_stale(self, max_age_ms: int = 60_000) -> bool:
        return (_now_ms() - self.captured_at) > max_age_ms


# ─────────────────────────────────────────────────────────────
# Wallet snapshot
# ─────────────────────────────────────────────────────────────

@dataclass
class WalletSnapshot:
    venue: str
    settlement_currency: str = "USDT"
    wallet_balance: Decimal = _ZERO
    margin_balance: Decimal = _ZERO
    available_balance: Decimal = _ZERO
    locked_balance: Decimal = _ZERO
    unrealized_pnl: Decimal = _ZERO
    realized_pnl: Decimal = _ZERO
    funding_accrued: Decimal = _ZERO
    fees_accrued: Decimal = _ZERO
    account_id: str = "default"
    id: Optional[int] = None
    snapshot_at: int = field(default_factory=_now_ms)

    # ── Derived ──────────────────────────────────────────────
    @property
    def equity(self) -> Decimal:
        return self.wallet_balance + self.unrealized_pnl

    @property
    def margin_utilization(self) -> Decimal:
        if self.equity <= _ZERO:
            return _D("1")
        return self.locked_balance / self.equity

    @property
    def free_collateral(self) -> Decimal:
        return max(_ZERO, self.available_balance)

    @property
    def tradeable_balance(self) -> Decimal:
        safety = self.wallet_balance * _D("0.02")
        return max(_ZERO, self.available_balance - safety)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "venue": self.venue,
            "settlement_currency": self.settlement_currency,
            "wallet_balance": str(self.wallet_balance),
            "margin_balance": str(self.margin_balance),
            "available_balance": str(self.available_balance),
            "locked_balance": str(self.locked_balance),
            "unrealized_pnl": str(self.unrealized_pnl),
            "realized_pnl": str(self.realized_pnl),
            "funding_accrued": str(self.funding_accrued),
            "fees_accrued": str(self.fees_accrued),
            "equity": str(self.equity),
            "margin_utilization": str(self.margin_utilization),
            "tradeable_balance": str(self.tradeable_balance),
            "snapshot_at": self.snapshot_at,
        }


# ─────────────────────────────────────────────────────────────
# Position
# ─────────────────────────────────────────────────────────────

@dataclass
class Position:
    venue: str
    symbol: str
    side: str               # 'LONG' | 'SHORT'
    qty: Decimal
    entry_price_avg: Decimal
    leverage: int = 1
    mark_price: Optional[Decimal] = None
    notional: Optional[Decimal] = None
    initial_margin: Optional[Decimal] = None
    maintenance_margin: Optional[Decimal] = None
    margin_used: Optional[Decimal] = None
    liquidation_price: Optional[Decimal] = None
    unrealized_pnl: Decimal = _ZERO
    realized_pnl: Decimal = _ZERO
    unrealized_pnl_pct: Decimal = _ZERO
    funding_accrued: Decimal = _ZERO
    fees_accrued: Decimal = _ZERO
    tp_price: Optional[Decimal] = None
    sl_price: Optional[Decimal] = None
    status: str = "open"    # 'open' | 'closing' | 'closed'
    playbook: Optional[str] = None
    strategy: Optional[str] = None
    position_uid: str = field(default_factory=_uid)
    user_id: str = "default"
    id: Optional[int] = None
    opened_at: int = field(default_factory=_now_ms)
    closed_at: Optional[int] = None
    updated_at: int = field(default_factory=_now_ms)

    # ── Live formulas ────────────────────────────────────────

    def refresh(self, mark: Decimal) -> None:
        """Recompute all mark-price-dependent fields in place.

        initial_margin is entry-price-based (fixed at open) and is NOT
        overwritten here.  Only notional uses the current mark price.
        PnL% = unrealized_pnl / entry_initial_margin × 100.
        """
        self.mark_price = mark
        self.notional = self.qty * mark      # mark-based for risk sizing
        # Preserve entry-based initial_margin; compute it once if missing
        if self.initial_margin is None or self.initial_margin <= _ZERO:
            self.initial_margin = (self.qty * self.entry_price_avg) / _D(self.leverage)
        if self.side == "LONG":
            self.unrealized_pnl = (mark - self.entry_price_avg) * self.qty
        else:
            self.unrealized_pnl = (self.entry_price_avg - mark) * self.qty
        if self.initial_margin and self.initial_margin > _ZERO:
            self.unrealized_pnl_pct = (self.unrealized_pnl / self.initial_margin) * _D(100)
        self.updated_at = _now_ms()

    def effective_leverage(self, wallet_balance: Decimal) -> Decimal:
        equity = wallet_balance + self.unrealized_pnl
        if equity <= _ZERO or self.notional is None:
            return _ZERO
        return self.notional / equity

    def safety_check(self) -> List[str]:
        """Return list of safety violations; empty = safe."""
        issues: List[str] = []
        if self.sl_price and self.liquidation_price:
            sl_dist = abs(self.entry_price_avg - self.sl_price)
            liq_dist = abs(self.entry_price_avg - self.liquidation_price)
            if liq_dist < sl_dist * _D("3"):
                issues.append(
                    f"Liquidation {self.liquidation_price} is less than 3× SL distance "
                    f"from entry {self.entry_price_avg}"
                )
        if self.qty <= _ZERO:
            issues.append("Position qty must be > 0")
        return issues

    def to_dict(self) -> Dict[str, Any]:
        return {
            "position_uid": self.position_uid,
            "venue": self.venue,
            "symbol": self.symbol,
            "side": self.side,
            "qty": str(self.qty),
            "entry_price_avg": str(self.entry_price_avg),
            "mark_price": str(self.mark_price) if self.mark_price else None,
            "notional": str(self.notional) if self.notional else None,
            "leverage": self.leverage,
            "initial_margin": str(self.initial_margin) if self.initial_margin else None,
            "liquidation_price": str(self.liquidation_price) if self.liquidation_price else None,
            "unrealized_pnl": str(self.unrealized_pnl),
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl_pct": str(self.unrealized_pnl_pct),
            "funding_accrued": str(self.funding_accrued),
            "fees_accrued": str(self.fees_accrued),
            "tp_price": str(self.tp_price) if self.tp_price else None,
            "sl_price": str(self.sl_price) if self.sl_price else None,
            "status": self.status,
            "opened_at": self.opened_at,
            "updated_at": self.updated_at,
        }


# ─────────────────────────────────────────────────────────────
# Order & Fill
# ─────────────────────────────────────────────────────────────

@dataclass
class Order:
    client_order_id: str
    venue: str
    symbol: str
    side: str               # 'BUY' | 'SELL'
    order_type: str         # 'MARKET' | 'LIMIT' | 'STOP_MARKET' | 'TAKE_PROFIT'
    qty: Decimal
    intent: str = "entry"   # 'entry' | 'sl' | 'tp' | 'reduce' | 'exit'
    price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    reduce_only: bool = False
    post_only: bool = False
    time_in_force: str = "GTC"
    status: str = "pending"
    filled_qty: Decimal = _ZERO
    avg_fill_price: Optional[Decimal] = None
    remaining_qty: Optional[Decimal] = None
    fee: Decimal = _ZERO
    fee_currency: str = "USDT"
    exchange_order_id: Optional[str] = None
    position_id: Optional[int] = None
    id: Optional[int] = None
    created_at: int = field(default_factory=_now_ms)
    updated_at: int = field(default_factory=_now_ms)

    def is_terminal(self) -> bool:
        return self.status in ("filled", "cancelled", "rejected", "expired")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "exchange_order_id": self.exchange_order_id,
            "venue": self.venue,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "intent": self.intent,
            "qty": str(self.qty),
            "price": str(self.price) if self.price else None,
            "stop_price": str(self.stop_price) if self.stop_price else None,
            "reduce_only": self.reduce_only,
            "status": self.status,
            "filled_qty": str(self.filled_qty),
            "avg_fill_price": str(self.avg_fill_price) if self.avg_fill_price else None,
            "fee": str(self.fee),
        }


@dataclass
class Fill:
    order_id: int
    venue: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    fee: Decimal = _ZERO
    fee_currency: str = "USDT"
    is_maker: bool = False
    exchange_trade_id: Optional[str] = None
    position_id: Optional[int] = None
    id: Optional[int] = None
    created_at: int = field(default_factory=_now_ms)


# ─────────────────────────────────────────────────────────────
# Reconciliation
# ─────────────────────────────────────────────────────────────

@dataclass
class ReconciliationResult:
    venue: str
    check_type: str         # 'balance' | 'position' | 'order'
    status: str             # 'ok' | 'drift' | 'fixed' | 'error'
    local_value: Optional[Dict] = None
    exchange_value: Optional[Dict] = None
    delta: Optional[Dict] = None
    action_taken: Optional[str] = None
    created_at: int = field(default_factory=_now_ms)
