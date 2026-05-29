"""
Normalised exchange adapter protocol.

Every venue adapter (Binance, CoinDCX, Delta) must satisfy this interface
so the rest of the system can swap venues without touching business logic.

All monetary values returned by adapters are plain Python ``float`` or
``str`` — the ledger layer converts them to ``Decimal`` before any
arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


# ─────────────────────────────────────────────────────────────
# Normalised data shapes returned by adapters
# ─────────────────────────────────────────────────────────────

@dataclass
class NormalisedBalance:
    asset: str                          # 'USDT', 'BTC', …
    wallet_balance: Decimal
    available_balance: Decimal
    locked_balance: Decimal
    unrealized_pnl: Decimal = Decimal("0")
    cross_wallet_balance: Optional[Decimal] = None


@dataclass
class NormalisedPosition:
    symbol: str
    side: str                           # 'LONG' | 'SHORT'
    qty: Decimal                        # always positive
    entry_price: Decimal
    mark_price: Decimal
    notional: Decimal
    leverage: int
    unrealized_pnl: Decimal
    liquidation_price: Optional[Decimal] = None
    initial_margin: Optional[Decimal] = None
    maintenance_margin: Optional[Decimal] = None
    position_uid: Optional[str] = None  # exchange-provided id if available


@dataclass
class NormalisedOrder:
    exchange_order_id: str
    client_order_id: Optional[str]
    symbol: str
    side: str                           # 'BUY' | 'SELL'
    order_type: str                     # 'MARKET' | 'LIMIT' | 'STOP_MARKET' | …
    qty: Decimal
    price: Optional[Decimal]
    stop_price: Optional[Decimal]
    status: str                         # 'pending' | 'open' | 'filled' | 'cancelled'
    filled_qty: Decimal = Decimal("0")
    avg_fill_price: Optional[Decimal] = None
    fee: Decimal = Decimal("0")
    fee_currency: str = "USDT"
    reduce_only: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)  # original exchange response


@dataclass
class NormalisedFill:
    exchange_trade_id: str
    exchange_order_id: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    fee: Decimal = Decimal("0")
    fee_currency: str = "USDT"
    is_maker: bool = False


@dataclass
class OrderRequest:
    symbol: str
    side: str                           # 'BUY' | 'SELL'
    order_type: str                     # 'MARKET' | 'LIMIT' | 'STOP_MARKET' | …
    qty: Decimal
    price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    reduce_only: bool = False
    post_only: bool = False
    time_in_force: str = "GTC"
    leverage: Optional[int] = None
    client_order_id: Optional[str] = None


# ─────────────────────────────────────────────────────────────
# Protocol
# ─────────────────────────────────────────────────────────────

@runtime_checkable
class ExchangeAdapter(Protocol):
    """
    Normalised interface every venue adapter must satisfy.

    Implementations must convert all venue-specific field names and units
    into the normalised shapes above.  Monetary values must be Decimal.
    """

    @property
    def venue_name(self) -> str:
        """e.g. 'binance_futures', 'coindcx_futures', 'delta'"""
        ...

    # ── Account state ─────────────────────────────────────────

    def get_balances(self) -> List[NormalisedBalance]:
        """Return all non-zero asset balances."""
        ...

    def get_positions(self) -> List[NormalisedPosition]:
        """Return all currently open positions."""
        ...

    def get_open_orders(
        self, symbol: Optional[str] = None
    ) -> List[NormalisedOrder]:
        """Return all open (unfilled) orders, optionally filtered by symbol."""
        ...

    def get_fills(
        self,
        symbol: Optional[str] = None,
        since_ms: Optional[int] = None,
        limit: int = 100,
    ) -> List[NormalisedFill]:
        """Return recent fills, optionally filtered by symbol / time window."""
        ...

    # ── Trading ───────────────────────────────────────────────

    def place_order(self, req: OrderRequest) -> NormalisedOrder:
        """
        Submit an order to the exchange.

        Raises ``ValueError`` for invalid parameters and ``RuntimeError``
        for exchange rejections.
        """
        ...

    def cancel_order(self, exchange_order_id: str, symbol: str) -> bool:
        """
        Cancel an open order.  Returns True on success.
        Returns False (instead of raising) if the order was already
        filled or cancelled.
        """
        ...

    # ── Configuration ─────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set account leverage for a symbol.  Returns True on success."""
        ...

    def set_margin_mode(
        self, symbol: str, mode: str
    ) -> bool:
        """
        Set margin mode for a symbol.

        ``mode`` must be one of ``'ISOLATED'`` or ``'CROSSED'``.
        Returns True on success.
        """
        ...


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def normalise_side(raw: str) -> str:
    """Map exchange-specific side strings to 'BUY' or 'SELL'."""
    s = raw.upper()
    if s in ("BUY", "LONG", "buy", "long"):
        return "BUY"
    if s in ("SELL", "SHORT", "sell", "short"):
        return "SELL"
    raise ValueError(f"Unrecognised order side: {raw!r}")


def normalise_position_side(raw: str, qty: Decimal) -> str:
    """Map exchange-specific position side to 'LONG' or 'SHORT'."""
    s = raw.upper()
    if s in ("LONG", "BUY"):
        return "LONG"
    if s in ("SHORT", "SELL"):
        return "SHORT"
    # Binance one-way mode: derive from signed qty
    if s in ("BOTH", ""):
        return "LONG" if qty >= Decimal("0") else "SHORT"
    raise ValueError(f"Unrecognised position side: {raw!r}")


def normalise_order_status(raw: str) -> str:
    """Map exchange order status to internal status string."""
    s = raw.upper()
    _map = {
        "NEW": "open",
        "OPEN": "open",
        "INIT": "open",
        "PARTIALLY_FILLED": "open",
        "PARTIALLY FILLED": "open",
        "FILLED": "filled",
        "CLOSED": "filled",
        "CANCELLED": "cancelled",
        "CANCELED": "cancelled",
        "EXPIRED": "cancelled",
        "REJECTED": "rejected",
        "PENDING": "pending",
    }
    return _map.get(s, s.lower())
