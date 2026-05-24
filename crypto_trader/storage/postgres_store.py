"""
crypto_trader.storage.postgres_store — Postgres-backed event journal
=====================================================================
Production journal for domain events + state snapshots. psycopg2 is imported
lazily so the package still imports in environments without it.

Schema::

    events(id BIGSERIAL PK, ts BIGINT, type TEXT, symbol TEXT, payload JSONB)
    snapshots(id BIGSERIAL PK, ts BIGINT, state JSONB)
"""
from __future__ import annotations

import json
import logging
import time
from typing import List, Optional

logger = logging.getLogger("crypto_trader.storage.postgres")

_DDL = """
CREATE TABLE IF NOT EXISTS events (
    id      BIGSERIAL PRIMARY KEY,
    ts      BIGINT NOT NULL,
    type    TEXT NOT NULL,
    symbol  TEXT,
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (type);
CREATE TABLE IF NOT EXISTS snapshots (
    id    BIGSERIAL PRIMARY KEY,
    ts    BIGINT NOT NULL,
    state JSONB NOT NULL
);
"""


class PostgresEventStore:
    def __init__(self, dsn: str):
        import psycopg2  # lazy
        self._psycopg2 = psycopg2
        self.dsn = dsn
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = True
        with self.conn.cursor() as cur:
            cur.execute(_DDL)
        logger.info("Postgres event store ready")

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def append(self, event: dict) -> None:
        payload = event.get("payload", {})
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO events (ts, type, symbol, payload) VALUES (%s, %s, %s, %s)",
                (
                    int(event.get("ts", self._now_ms())),
                    event.get("event_type", "UNKNOWN"),
                    payload.get("symbol"),
                    json.dumps(payload, default=str),
                ),
            )

    def load_events(self, limit: Optional[int] = None) -> List[dict]:
        q = "SELECT ts, type, payload FROM events ORDER BY id ASC"
        if limit:
            q += f" LIMIT {int(limit)}"
        with self.conn.cursor() as cur:
            cur.execute(q)
            rows = cur.fetchall()
        return [{"ts": r[0], "event_type": r[1], "payload": r[2]} for r in rows]

    def snapshot(self, state: dict) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO snapshots (ts, state) VALUES (%s, %s)",
                (self._now_ms(), json.dumps(state, default=str)),
            )

    def latest_snapshot(self) -> Optional[dict]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT state FROM snapshots ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
        return row[0] if row else None

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
