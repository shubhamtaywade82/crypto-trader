"""
crypto_trader.storage.sqlite_store — SQLite event journal (dev fallback)
=========================================================================
Mirrors the Postgres store's interface for local development when no
``DATABASE_URL`` is configured.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import List, Optional


class SQLiteEventStore:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, type TEXT, symbol TEXT, payload TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS snapshots ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, state TEXT)"
        )
        self.conn.commit()

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def append(self, event: dict) -> None:
        payload = event.get("payload", {})
        self.conn.execute(
            "INSERT INTO events (ts, type, symbol, payload) VALUES (?, ?, ?, ?)",
            (int(event.get("ts", self._now_ms())), event.get("event_type", "UNKNOWN"),
             payload.get("symbol"), json.dumps(payload, default=str)),
        )
        self.conn.commit()

    def load_events(self, limit: Optional[int] = None) -> List[dict]:
        q = "SELECT ts, type, payload FROM events ORDER BY id ASC"
        if limit:
            q += f" LIMIT {int(limit)}"
        rows = self.conn.execute(q).fetchall()
        return [{"ts": r[0], "event_type": r[1], "payload": json.loads(r[2])} for r in rows]

    def snapshot(self, state: dict) -> None:
        self.conn.execute(
            "INSERT INTO snapshots (ts, state) VALUES (?, ?)",
            (self._now_ms(), json.dumps(state, default=str)),
        )
        self.conn.commit()

    def latest_snapshot(self) -> Optional[dict]:
        row = self.conn.execute("SELECT state FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
