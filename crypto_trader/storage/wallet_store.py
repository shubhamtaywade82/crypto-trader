"""
crypto_trader.storage.wallet_store — Wallet persistence adapter
================================================================
Abstracts SQLite/Postgres so wallet.py has no direct DB calls.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional, Tuple, Any

logger = logging.getLogger("crypto_trader.storage.wallet_store")


class WalletStore:
    """Persistence adapter for EnhancedFuturesWallet.

    Uses Postgres when database_url is provided, else SQLite.
    All SQL uses %s placeholders internally; SQLite adapter translates.
    """

    def __init__(
        self,
        database_url: str = "",
        sqlite_path: str = "",
        namespace: str = "default",
        symbol: str = "GLOBAL",
    ):
        self.database_url = database_url
        self.sqlite_path = sqlite_path
        self.namespace = namespace
        self.symbol = symbol
        self._use_postgres = bool(database_url)
        self._pg_pool = None
        if self._use_postgres:
            self._init_pg_pool()
        self.init_schema()

    def _init_pg_pool(self):
        try:
            import psycopg2
            from psycopg2 import pool as pgpool
            self._pg_pool = pgpool.SimpleConnectionPool(1, 5, self.database_url)
        except Exception as e:
            logger.error("WalletStore: failed to init Postgres pool: %s", e)
            raise

    @contextmanager
    def _conn(self):
        if self._use_postgres:
            conn = self._pg_pool.getconn()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                self._pg_pool.putconn(conn)
        else:
            with sqlite3.connect(self.sqlite_path) as conn:
                yield conn

    def _q(self, sql: str) -> str:
        """Convert %s placeholders to ? for SQLite."""
        if not self._use_postgres:
            return sql.replace("%s", "?")
        return sql

    def init_schema(self):
        with self._conn() as conn:
            if self._use_postgres:
                cur = conn.cursor()
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS wallet_events (
                        id BIGSERIAL PRIMARY KEY,
                        ts BIGINT NOT NULL,
                        event_type TEXT NOT NULL,
                        symbol TEXT,
                        namespace TEXT,
                        payload_json TEXT NOT NULL
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_wallet_events_ts ON wallet_events(ts)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_wallet_events_type ON wallet_events(event_type)")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS wallet_orders (
                        order_id TEXT PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        side TEXT NOT NULL,
                        order_type TEXT NOT NULL,
                        quantity TEXT NOT NULL,
                        filled_quantity TEXT NOT NULL,
                        avg_fill_price TEXT NOT NULL,
                        status TEXT NOT NULL,
                        reduce_only BOOLEAN NOT NULL DEFAULT FALSE,
                        trigger_price TEXT,
                        limit_price TEXT,
                        expires_at BIGINT,
                        updated_ts BIGINT NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS wallet_fills (
                        id BIGSERIAL PRIMARY KEY,
                        order_id TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        side TEXT NOT NULL,
                        quantity TEXT NOT NULL,
                        fill_price TEXT NOT NULL,
                        fee TEXT NOT NULL,
                        ts BIGINT NOT NULL,
                        sequence INTEGER NOT NULL
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_wallet_fills_order_id ON wallet_fills(order_id)")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS wallet_funding (
                        id BIGSERIAL PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        rate TEXT NOT NULL,
                        notional TEXT NOT NULL,
                        amount TEXT NOT NULL,
                        ts BIGINT NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS wallet_fees (
                        id BIGSERIAL PRIMARY KEY,
                        symbol TEXT,
                        reason TEXT,
                        amount TEXT NOT NULL,
                        ts BIGINT NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS wallet_snapshots (
                        id BIGSERIAL PRIMARY KEY,
                        ts BIGINT NOT NULL,
                        namespace TEXT,
                        wallet_balance TEXT NOT NULL,
                        realized_pnl_total TEXT NOT NULL,
                        state_json TEXT NOT NULL
                    )
                """)
                # Backwards-compat: add namespace column if table was created before this change
                cur.execute("""
                    ALTER TABLE wallet_snapshots ADD COLUMN IF NOT EXISTS namespace TEXT
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS arb_events (
                        seq BIGSERIAL PRIMARY KEY,
                        ts BIGINT,
                        arb_id TEXT,
                        event_type TEXT,
                        payload TEXT
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS arb_positions (
                        arb_id TEXT PRIMARY KEY,
                        symbol TEXT,
                        state TEXT,
                        snapshot TEXT,
                        updated_ts BIGINT
                    )
                """)
                conn.commit()
            else:
                # SQLite schema — original table names (backward compat)
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts INTEGER NOT NULL, event_type TEXT NOT NULL,
                        symbol TEXT, namespace TEXT, payload_json TEXT NOT NULL
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS orders (
                        order_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
                        order_type TEXT NOT NULL, quantity TEXT NOT NULL, filled_quantity TEXT NOT NULL,
                        avg_fill_price TEXT NOT NULL, status TEXT NOT NULL,
                        reduce_only INTEGER NOT NULL DEFAULT 0, trigger_price TEXT,
                        limit_price TEXT, expires_at INTEGER, updated_ts INTEGER NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS fills (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, symbol TEXT NOT NULL,
                        side TEXT NOT NULL, quantity TEXT NOT NULL, fill_price TEXT NOT NULL,
                        fee TEXT NOT NULL, ts INTEGER NOT NULL, sequence INTEGER NOT NULL
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_fills_order_id ON fills(order_id)")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS funding (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                        rate TEXT NOT NULL, notional TEXT NOT NULL, amount TEXT NOT NULL, ts INTEGER NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS fees (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, reason TEXT,
                        amount TEXT NOT NULL, ts INTEGER NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS snapshots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
                        namespace TEXT,
                        wallet_balance TEXT NOT NULL, realized_pnl_total TEXT NOT NULL, state_json TEXT NOT NULL
                    )
                """)
                conn.commit()

    # Table name helpers (Postgres uses wallet_ prefix, SQLite uses original names)
    def _t(self, name: str) -> str:
        if self._use_postgres:
            return f"wallet_{name}"
        return name

    def append_event(self, ts: int, event_type: str, symbol: str, namespace: str, payload_json: str):
        tbl = self._t("events")
        with self._conn() as conn:
            if self._use_postgres:
                cur = conn.cursor()
                cur.execute(
                    f"INSERT INTO {tbl} (ts, event_type, symbol, namespace, payload_json) VALUES (%s, %s, %s, %s, %s)",
                    (ts, event_type, symbol, namespace, payload_json)
                )
            else:
                conn.execute(
                    f"INSERT INTO {tbl} (ts, event_type, symbol, namespace, payload_json) VALUES (?, ?, ?, ?, ?)",
                    (ts, event_type, symbol, namespace, payload_json)
                )

    def iter_events(self) -> List[Tuple]:
        tbl = self._t("events")
        with self._conn() as conn:
            if self._use_postgres:
                cur = conn.cursor()
                cur.execute(f"SELECT ts, event_type, symbol, namespace, payload_json FROM {tbl} ORDER BY ts ASC, id ASC")
                return cur.fetchall()
            else:
                return conn.execute(
                    f"SELECT ts, event_type, symbol, namespace, payload_json FROM {tbl} ORDER BY ts ASC, id ASC"
                ).fetchall()

    def count_events(self) -> int:
        tbl = self._t("events")
        with self._conn() as conn:
            if self._use_postgres:
                cur = conn.cursor()
                cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                return cur.fetchone()[0]
            else:
                return conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]

    def get_events_by_symbol(self, symbol: str) -> List[Tuple]:
        tbl = self._t("events")
        sql = f"SELECT ts, event_type, symbol, namespace, payload_json FROM {tbl} WHERE symbol=%s ORDER BY ts ASC, id ASC"
        with self._conn() as conn:
            if self._use_postgres:
                cur = conn.cursor()
                cur.execute(sql, (symbol,))
                return cur.fetchall()
            else:
                return conn.execute(sql.replace("%s", "?"), (symbol,)).fetchall()

    def get_events_by_type(self, event_type: str, limit: int = 100) -> List[Tuple]:
        tbl = self._t("events")
        sql = f"SELECT ts, event_type, symbol, namespace, payload_json FROM {tbl} WHERE event_type=%s ORDER BY ts DESC LIMIT %s"
        with self._conn() as conn:
            if self._use_postgres:
                cur = conn.cursor()
                cur.execute(sql, (event_type, limit))
                return cur.fetchall()
            else:
                return conn.execute(sql.replace("%s", "?"), (event_type, limit)).fetchall()

    def get_funding_last_ts(self, symbol: str) -> int:
        tbl = self._t("funding")
        sql = f"SELECT MAX(ts) FROM {tbl} WHERE symbol=%s"
        with self._conn() as conn:
            if self._use_postgres:
                cur = conn.cursor()
                cur.execute(sql, (symbol,))
                row = cur.fetchone()
            else:
                row = conn.execute(sql.replace("%s", "?"), (symbol,)).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def get_funding_history(self, symbol: str) -> List[Tuple]:
        tbl = self._t("funding")
        sql = f"SELECT ts, rate, notional, amount FROM {tbl} WHERE symbol=%s ORDER BY ts ASC"
        with self._conn() as conn:
            if self._use_postgres:
                cur = conn.cursor()
                cur.execute(sql, (symbol,))
                return cur.fetchall()
            else:
                return conn.execute(sql.replace("%s", "?"), (symbol,)).fetchall()

    def get_latest_snapshot(self) -> Optional[Tuple]:
        tbl = self._t("snapshots")
        # Strict namespace isolation: paper/live modes must never share snapshots.
        sql = f"SELECT ts, wallet_balance, realized_pnl_total, state_json FROM {tbl} WHERE namespace = %s ORDER BY ts DESC LIMIT 1"
        with self._conn() as conn:
            if self._use_postgres:
                cur = conn.cursor()
                cur.execute(sql, (self.namespace,))
                return cur.fetchone()
            else:
                return conn.execute(sql.replace("%s", "?"), (self.namespace,)).fetchone()

    def iter_snapshots(self) -> List[Tuple]:
        """All retained snapshots oldest-first (ts, wallet_balance,
        realized_pnl_total, state_json). Note: snapshots are rotated, so this is
        a short tail — for a full equity curve prefer deriving from closed-trade
        outcomes (see analytics.metrics.equity_curve_from_records)."""
        tbl = self._t("snapshots")
        sql = f"SELECT ts, wallet_balance, realized_pnl_total, state_json FROM {tbl} WHERE namespace = %s ORDER BY ts ASC"
        with self._conn() as conn:
            if self._use_postgres:
                cur = conn.cursor()
                cur.execute(sql, (self.namespace,))
                return cur.fetchall()
            else:
                return conn.execute(sql.replace("%s", "?"), (self.namespace,)).fetchall()

    def save_snapshot(self, ts: int, wallet_balance: str, realized_pnl_total: str, state_json: str,
                      max_snapshots: int = 10):
        tbl = self._t("snapshots")
        sql = f"INSERT INTO {tbl} (ts, namespace, wallet_balance, realized_pnl_total, state_json) VALUES (%s, %s, %s, %s, %s)"
        with self._conn() as conn:
            if self._use_postgres:
                cur = conn.cursor()
                cur.execute(sql, (ts, self.namespace, wallet_balance, realized_pnl_total, state_json))
                # Rotate old snapshots for THIS namespace only
                cur.execute(
                    f"""DELETE FROM {tbl} WHERE namespace = %s AND id NOT IN (
                        SELECT id FROM {tbl} WHERE namespace = %s ORDER BY ts DESC LIMIT %s
                    )""",
                    (self.namespace, self.namespace, max_snapshots),
                )
            else:
                conn.execute(sql.replace("%s", "?"), (ts, self.namespace, wallet_balance, realized_pnl_total, state_json))
                rows = conn.execute(
                    f"SELECT id FROM {tbl} WHERE namespace = ? ORDER BY ts DESC LIMIT ?", (self.namespace, max_snapshots)
                ).fetchall()
                ids_to_keep = [r[0] for r in rows]
                if ids_to_keep:
                    placeholders = ",".join(["?"] * len(ids_to_keep))
                    conn.execute(f"DELETE FROM {tbl} WHERE namespace = ? AND id NOT IN ({placeholders})",
                                 (self.namespace,) + tuple(ids_to_keep))

    def persist_domain_event(self, et: str, payload: dict, ts: int):
        """Persist ORDER/FILL/FUNDING/FEE domain events to dedicated tables."""
        with self._conn() as conn:
            self._persist_inner(conn, et, payload, ts)

    def _persist_inner(self, conn, et: str, payload: dict, ts: int):
        pg = self._use_postgres
        cur = conn.cursor() if pg else None

        def ex(sql, params):
            if pg:
                cur.execute(sql, params)
            else:
                conn.execute(sql.replace("%s", "?"), params)

        tbl_orders = self._t("orders")
        tbl_fills = self._t("fills")
        tbl_funding = self._t("funding")
        tbl_fees = self._t("fees")

        if et == "ORDER_CREATED":
            if pg:
                ex(f"""
                    INSERT INTO {tbl_orders} (order_id, symbol, side, order_type, quantity, filled_quantity,
                        avg_fill_price, status, reduce_only, trigger_price, limit_price, expires_at, updated_ts)
                    VALUES (%s,%s,%s,%s,%s,'0','0',%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(order_id) DO UPDATE SET
                        status=EXCLUDED.status, updated_ts=EXCLUDED.updated_ts
                """, (
                    payload.get("order_id"), payload.get("symbol"), payload.get("side"),
                    payload.get("order_type", "MARKET"), str(payload.get("quantity", "0")),
                    payload.get("status", "NEW"), payload.get("reduce_only", False),
                    str(payload.get("trigger_price")) if payload.get("trigger_price") is not None else None,
                    str(payload.get("limit_price")) if payload.get("limit_price") is not None else None,
                    payload.get("expires_at"), ts,
                ))
            else:
                ex(f"""
                    INSERT OR REPLACE INTO {tbl_orders}
                    (order_id, symbol, side, order_type, quantity, filled_quantity, avg_fill_price,
                     status, reduce_only, trigger_price, limit_price, expires_at, updated_ts)
                    VALUES (%s,%s,%s,%s,%s,'0','0',%s,%s,%s,%s,%s,%s)
                """, (
                    payload.get("order_id"), payload.get("symbol"), payload.get("side"),
                    payload.get("order_type", "MARKET"), str(payload.get("quantity", "0")),
                    payload.get("status", "NEW"), 1 if payload.get("reduce_only", False) else 0,
                    str(payload.get("trigger_price")) if payload.get("trigger_price") is not None else None,
                    str(payload.get("limit_price")) if payload.get("limit_price") is not None else None,
                    payload.get("expires_at"), ts,
                ))

        elif et in ("ORDER_FILLED", "ORDER_PARTIALLY_FILLED", "ORDER_CANCELLED", "ORDER_REJECTED", "ORDER_EXPIRED"):
            oid = payload.get("order_id")
            if et in ("ORDER_FILLED", "ORDER_PARTIALLY_FILLED"):
                ex(
                    f"INSERT INTO {tbl_fills} (order_id, symbol, side, quantity, fill_price, fee, ts, sequence) "
                    f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (oid, payload.get("symbol"), payload.get("side"), str(payload.get("quantity", "0")),
                     str(payload.get("fill_price", "0")), str(payload.get("fee", "0")), ts,
                     int(payload.get("sequence", 1)))
                )
            # Update order row
            if pg:
                cur.execute(f"SELECT quantity, filled_quantity FROM {tbl_orders} WHERE order_id=%s", (oid,))
                row = cur.fetchone()
            else:
                row = conn.execute(
                    f"SELECT quantity, filled_quantity FROM {tbl_orders} WHERE order_id=?", (oid,)
                ).fetchone()
            if row:
                from decimal import Decimal
                qty = Decimal(str(row[0]))
                filled = Decimal(str(row[1]))
                if et in ("ORDER_FILLED", "ORDER_PARTIALLY_FILLED"):
                    filled += Decimal(str(payload.get("quantity", "0")))
                status = payload.get("status") or {
                    "ORDER_CANCELLED": "CANCELLED", "ORDER_REJECTED": "REJECTED",
                    "ORDER_EXPIRED": "EXPIRED", "ORDER_FILLED": "FILLED",
                }.get(et, "FILLED")
                ex(
                    f"UPDATE {tbl_orders} SET filled_quantity=%s, avg_fill_price=%s, status=%s, "
                    f"updated_ts=%s WHERE order_id=%s",
                    (str(filled), str(payload.get("fill_price", "0")), status, ts, oid)
                )

        elif et == "FUNDING_APPLIED":
            ex(
                f"INSERT INTO {tbl_funding} (symbol, rate, notional, amount, ts) VALUES (%s,%s,%s,%s,%s)",
                (payload.get("symbol"), str(payload.get("rate", "0")), str(payload.get("notional", "0")),
                 str(payload.get("amount", "0")), ts)
            )

        elif et == "FEE_CHARGED":
            ex(
                f"INSERT INTO {tbl_fees} (symbol, reason, amount, ts) VALUES (%s,%s,%s,%s)",
                (payload.get("symbol"), payload.get("reason"), str(payload.get("amount", "0")), ts)
            )
