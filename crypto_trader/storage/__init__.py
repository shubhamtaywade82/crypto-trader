"""Event-store factory: always Postgres (requires DATABASE_URL)."""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("crypto_trader.storage")


def get_event_store(database_url: str = "", *, sqlite_path: Optional[str] = None):
    if database_url:
        from .postgres_store import PostgresEventStore
        return PostgresEventStore(database_url)
    if sqlite_path:
        # Allow explicit sqlite for tests
        from .sqlite_store import SQLiteEventStore
        logger.warning("Using SQLite event store — set DATABASE_URL for production")
        return SQLiteEventStore(sqlite_path)
    raise ValueError(
        "DATABASE_URL is required. Set it in your environment: "
        "DATABASE_URL=postgresql://user:pass@host:5432/dbname"
    )
