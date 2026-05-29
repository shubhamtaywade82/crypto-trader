"""
Unit of Work — groups multiple repository writes into a single atomic boundary.

Guarantees that either all writes commit together or all roll back, and that
domain events are only dispatched after a successful commit.

Usage (as context manager)::

    class LedgerUoW(UnitOfWork):
        def _begin(self):    self._conn = db.get_connection(); self._conn.begin()
        def _commit(self):   self._conn.commit()
        def _rollback(self): self._conn.rollback()

    with LedgerUoW(db) as uow:
        uow.positions.save(pos)
        uow.ledger.post(entry)
        uow.collect_event(PositionOpenedEvent(pos))
    # on clean exit: commits, then pops and dispatches events
    # on exception:  rolls back; events are discarded

Usage (explicit)::

    uow = LedgerUoW(db)
    uow._begin()
    try:
        ...
        uow._commit()
    except Exception:
        uow._rollback()
        raise
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, List


class UnitOfWork(ABC):
    """Abstract Unit of Work. Subclass and implement _begin / _commit / _rollback."""

    def __init__(self) -> None:
        self._events: List[Any] = []

    # ── Subclass contract ─────────────────────────────────────────────────────

    @abstractmethod
    def _begin(self) -> None:
        """Open the transaction / acquire resources."""

    @abstractmethod
    def _commit(self) -> None:
        """Persist all writes atomically."""

    @abstractmethod
    def _rollback(self) -> None:
        """Discard all writes since _begin."""

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "UnitOfWork":
        self._begin()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is None:
            self._commit()
        else:
            self._rollback()
            self._events.clear()
        return False  # never suppress exceptions

    # ── Domain event collection ───────────────────────────────────────────────

    def collect_event(self, event: Any) -> None:
        """Accumulate a domain event to dispatch *after* a successful commit."""
        self._events.append(event)

    def pop_events(self) -> List[Any]:
        """Drain and return all pending domain events (call after commit)."""
        events, self._events = list(self._events), []
        return events
