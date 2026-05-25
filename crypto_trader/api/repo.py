"""
crypto_trader.api.repo — read-only data access for the dashboard
=================================================================
Reads the Postgres projection (active_positions / orders / fills) the bot writes.
Defined behind a small Protocol so the API can be tested with an in-memory fake
(no database required).
"""
from __future__ import annotations

import time
from typing import List, Optional, Protocol


class ProjectionRepo(Protocol):
    def positions(self, mode: Optional[str] = None) -> List[dict]: ...
    def orders(self, mode: Optional[str] = None, limit: int = 50) -> List[dict]: ...
    def fills(self, mode: Optional[str] = None, limit: int = 50) -> List[dict]: ...
    def pnl(self, mode: Optional[str] = None) -> dict: ...
    def health(self) -> dict: ...


class PostgresRepo:
    """Read layer over the projection tables (lazy psycopg2)."""

    def __init__(self, dsn: str):
        import psycopg2  # lazy
        self._connect = lambda: psycopg2.connect(dsn)
        self.conn = self._connect()
        self.conn.autocommit = True

    def _rows(self, sql: str, args: tuple = ()) -> List[dict]:
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def positions(self, mode: Optional[str] = None) -> List[dict]:
        sql = "SELECT symbol, mode, side, qty, avg_price, status, last_event_ts FROM active_positions"
        if mode:
            return self._rows(sql + " WHERE mode=%s ORDER BY symbol", (mode,))
        return self._rows(sql + " ORDER BY symbol")

    def orders(self, mode: Optional[str] = None, limit: int = 50) -> List[dict]:
        sql = ("SELECT exchange_order_id, mode, symbol, side, order_type, qty, filled_qty, "
               "avg_fill_price, status, created_at FROM orders")
        if mode:
            return self._rows(sql + " WHERE mode=%s ORDER BY created_at DESC LIMIT %s", (mode, limit))
        return self._rows(sql + " ORDER BY created_at DESC LIMIT %s", (limit,))

    def fills(self, mode: Optional[str] = None, limit: int = 50) -> List[dict]:
        sql = "SELECT exchange_order_id, symbol, side, mode, price, qty, fee, ts FROM fills"
        if mode:
            return self._rows(sql + " WHERE mode=%s ORDER BY ts DESC LIMIT %s", (mode, limit))
        return self._rows(sql + " ORDER BY ts DESC LIMIT %s", (limit,))

    def pnl(self, mode: Optional[str] = None) -> dict:
        sql = ("SELECT mode, COUNT(*) AS fills, COALESCE(SUM(fee),0) AS total_fees, "
               "COALESCE(SUM(qty),0) AS total_qty FROM fills")
        rows = self._rows(sql + (" WHERE mode=%s" if mode else "") + " GROUP BY mode", (mode,) if mode else ())
        return {r["mode"]: r for r in rows}

    def health(self) -> dict:
        try:
            last = self._rows("SELECT MAX(last_event_ts) AS ts FROM active_positions")
            last_ts = (last[0]["ts"] if last else None) or 0
            age_ms = int(time.time() * 1000) - int(last_ts) if last_ts else None
            return {"db": "ok", "last_event_ts": last_ts, "last_event_age_ms": age_ms}
        except Exception as e:
            return {"db": "error", "error": str(e)}
