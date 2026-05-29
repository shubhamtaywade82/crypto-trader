"""
crypto_trader.ledger.unit_of_work — Concrete ledger Unit of Work.

Wraps a single LedgerDB transaction so that multiple repository writes
(ledger entry + position update + wallet snapshot) either all commit or all
roll back together, and domain events are only dispatched after a confirmed
commit.

Usage::

    from crypto_trader.ledger.unit_of_work import LedgerUnitOfWork

    with LedgerUnitOfWork(db, redis_layer=redis, event_bus=bus) as uow:
        # All writes use the same DB cursor / transaction
        ledger_svc.post_trade(uow.cursor, ...)
        position_engine.update(uow.cursor, position)
        uow.collect_event(TradeSettledEvent(position_uid=uid))
    # → commit on clean exit; Redis cache invalidated; events dispatched
    # → rollback + events discarded on any exception
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .db import LedgerDB
from ..patterns.unit_of_work import UnitOfWork

logger = logging.getLogger("crypto_trader.ledger.unit_of_work")


class LedgerUnitOfWork(UnitOfWork):
    """
    Atomic boundary for ledger writes.

    Wraps ``LedgerDB.transaction()`` so callers get a single cursor for the
    duration of their writes, with automatic commit/rollback and post-commit
    domain-event dispatch.

    Parameters
    ----------
    db:
        Shared LedgerDB instance.
    redis_layer:
        Optional RedisLayer for cache invalidation after commit.
        If None, the hot-cache layer is skipped (safe for tests/paper mode).
    event_bus:
        Optional event bus whose ``publish(event)`` is called for every
        domain event collected via ``collect_event()`` after a successful
        commit.
    """

    def __init__(
        self,
        db: LedgerDB,
        *,
        redis_layer=None,
        event_bus=None,
    ) -> None:
        super().__init__()
        self._db = db
        self._redis = redis_layer
        self._event_bus = event_bus
        self._cm = None    # the active contextmanager object
        self._cursor = None

    # ── Subclass contract ─────────────────────────────────────────────────────

    def _begin(self) -> None:
        self._cm = self._db.transaction()
        self._cursor = self._cm.__enter__()
        logger.debug("[LedgerUoW] transaction begun")

    def _commit(self) -> None:
        if self._cm is None:
            return
        self._cm.__exit__(None, None, None)
        self._cm = None
        logger.debug("[LedgerUoW] committed")
        self._post_commit()

    def _rollback(self) -> None:
        if self._cm is None:
            return
        # Passing a dummy exception to __exit__ triggers the except branch in
        # the contextmanager, which calls conn.rollback() then re-raises.
        try:
            self._cm.__exit__(Exception, Exception("uow rollback"), None)
        except Exception:
            pass  # swallow the re-raise from the CM
        self._cm = None
        logger.debug("[LedgerUoW] rolled back")

    # ── Cursor access ─────────────────────────────────────────────────────────

    @property
    def cursor(self) -> Any:
        """DB cursor for the active transaction. Available between _begin and _commit."""
        if self._cursor is None:
            raise RuntimeError("LedgerUnitOfWork: transaction not started (call __enter__ first)")
        return self._cursor

    # ── Post-commit side effects ──────────────────────────────────────────────

    def _post_commit(self) -> None:
        """Invalidate Redis cache and dispatch domain events after a successful commit."""
        events = self.pop_events()
        if self._redis is not None and events:
            self._invalidate_cache(events)
        if self._event_bus is not None:
            for evt in events:
                try:
                    self._event_bus.publish(evt)
                except Exception as exc:
                    logger.warning("[LedgerUoW] event dispatch failed: %s", exc)

    def _invalidate_cache(self, events: list) -> None:
        """Best-effort Redis key invalidation based on event metadata."""
        for evt in events:
            uid = getattr(evt, "position_uid", None)
            user_id = getattr(evt, "user_id", "default")
            venue = getattr(evt, "venue", None)
            try:
                if uid and hasattr(self._redis, "delete_position"):
                    self._redis.delete_position(uid)
                if venue and hasattr(self._redis, "delete_positions_index"):
                    self._redis.delete_positions_index(user_id, venue)
            except Exception as exc:
                logger.debug("[LedgerUoW] Redis invalidation skipped: %s", exc)
