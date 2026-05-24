"""
crypto_trader.exchanges.base — Exchange interfaces
===================================================
Protocols that decouple strategy/orchestration from any specific venue.

* ``MarketDataSource`` — read-only price/feed access with a freshness signal.
* ``ExecutionVenue``   — order placement + account-state reads.

The existing ``wallet.ExecutionEngine`` Protocol remains the canonical
execution contract used by the wallet/order-manager; ``ExecutionVenue`` extends
it conceptually with the account-state reads needed for reconciliation.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class MarketDataSource(Protocol):
    name: str

    def get_mark_price(self, symbol: str) -> float: ...

    def last_update_ms(self, symbol: str) -> int:
        """Local epoch-ms of the most recent update; 0 if never."""
        ...

    def is_fresh(self, symbol: str, max_age_ms: int) -> bool: ...


@runtime_checkable
class ExecutionVenue(Protocol):
    def get_balances(self) -> Dict[str, float]: ...
    def get_positions(self) -> List[dict]: ...
    def get_open_orders(self, symbol: Optional[str] = None) -> List[dict]: ...
    def get_fills(self, symbol: Optional[str] = None) -> List[dict]: ...
