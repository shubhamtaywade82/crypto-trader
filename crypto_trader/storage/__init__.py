"""Event-store factory: Postgres when DATABASE_URL is set, else SQLite dev fallback."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("crypto_trader.storage")


def get_event_store(database_url: str = "", *, sqlite_path: Optional[str] = None):
    if database_url:
        from .postgres_store import PostgresEventStore
        return PostgresEventStore(database_url)
    from .sqlite_store import SQLiteEventStore
    path = sqlite_path or str(Path.home() / ".crypto_trader" / "event_journal.db")
    logger.info("Using SQLite event store at %s (no DATABASE_URL set)", path)
    return SQLiteEventStore(path)
