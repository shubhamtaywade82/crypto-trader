"""
crypto_trader.execution.adapters.base — ExecutorProtocol

The single interface every executor must satisfy.  Everything above the
executor in the stack (Risk, Wallet, Portfolio, Signal engines) codes
against this protocol — not against a concrete class.

Paper, Sandbox, and Live executors all implement this protocol, which is
how 95% of the codebase stays identical across modes.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional, Protocol, runtime_checkable

from ...wallet import Order, OrderType, PositionSide
from ...exchanges.adapter_protocol import NormalisedBalance, NormalisedPosition


@runtime_checkable
class ExecutorProtocol(Protocol):
    """
    Minimal execution interface shared by Paper, Sandbox, and Live modes.

    Implementations must NOT leak mode-specific details upward — the caller
    should never need to know which executor is active.
    """

    # ── Order operations ──────────────────────────────────────────────────

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
        """Submit an order.  Returns a filled/pending Order."""
        ...

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by ID.  Returns True on success."""
        ...

    # ── Account state ─────────────────────────────────────────────────────

    def sync_positions(self) -> Dict[str, dict]:
        """
        Return current open positions keyed by symbol.

        Paper: derived from local wallet.
        Sandbox/Live: fetched from exchange.
        """
        ...

    def get_balances(self) -> List[NormalisedBalance]:
        """
        Return current balances.

        Paper: derived from local ledger.
        Sandbox/Live: fetched from exchange.
        """
        ...

    # ── Identity ──────────────────────────────────────────────────────────

    @property
    def mode_label(self) -> str:
        """Human-readable mode identifier for logs ('paper', 'sandbox', 'live')."""
        ...
