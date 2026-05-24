"""
crypto_trader.storage.base — Event store interface
====================================================
Production journaling abstraction. The wallet's internal SQLite state-recovery
stays as-is (local dev durability); an ``EventStore`` is attached as an
additional sink (via the wallet's ``event_hook``) so the canonical domain-event
journal can live in Postgres for production.
"""
from __future__ import annotations

from typing import List, Optional, Protocol


class EventStore(Protocol):
    def append(self, event: dict) -> None: ...
    def load_events(self, limit: Optional[int] = None) -> List[dict]: ...
    def snapshot(self, state: dict) -> None: ...
    def latest_snapshot(self) -> Optional[dict]: ...
    def close(self) -> None: ...
