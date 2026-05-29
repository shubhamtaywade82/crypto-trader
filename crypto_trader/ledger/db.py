"""
Database connection pool and query helpers for the ledger layer.
Uses psycopg2 for Postgres (production) with a thread-safe pool.
Falls back to SQLite (development) when DATABASE_URL is unset.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger("crypto_trader.ledger.db")


class _JsonEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, cls=_JsonEncoder)


def _now_ms() -> int:
    return int(time.time() * 1000)


# ─────────────────────────────────────────────────────────────
# Postgres pool
# ─────────────────────────────────────────────────────────────

class LedgerDB:
    """
    Thread-safe database wrapper for the ledger.

    Supports Postgres (psycopg2 pool) and SQLite (single-file, WAL mode).
    All public methods use the same interface regardless of backend.

    Usage:
        db = LedgerDB.from_url(os.environ.get("DATABASE_URL", ""))
        with db.cursor() as cur:
            cur.execute("SELECT 1")
    """

    def __init__(self, database_url: str = "", sqlite_path: str = "ledger_dev.db"):
        self._lock = threading.RLock()
        self._backend = "postgres" if database_url else "sqlite"
        self._pool = None
        self._sqlite_conn = None

        if self._backend == "postgres":
            try:
                import psycopg2.pool
                self._pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=10,
                    dsn=database_url,
                )
                logger.info("[LedgerDB] Postgres pool connected")
            except ImportError:
                raise RuntimeError("psycopg2 required for production ledger: pip install psycopg2-binary")
        else:
            import sqlite3
            self._sqlite_conn = sqlite3.connect(sqlite_path, check_same_thread=False)
            self._sqlite_conn.row_factory = sqlite3.Row
            self._sqlite_conn.execute("PRAGMA journal_mode=WAL")
            self._sqlite_conn.execute("PRAGMA synchronous=NORMAL")
            self._sqlite_conn.commit()
            logger.info("[LedgerDB] SQLite dev mode: %s", sqlite_path)

    @classmethod
    def from_url(cls, database_url: str = "", sqlite_path: str = "ledger_dev.db") -> "LedgerDB":
        return cls(database_url=database_url, sqlite_path=sqlite_path)

    # ── SQL dialect helpers ───────────────────────────────────

    def _adapt(self, sql: str) -> str:
        """Translate %s → ? for SQLite."""
        if self._backend == "sqlite":
            return sql.replace("%s", "?")
        return sql

    def _jsonb(self, obj: Any) -> Any:
        """Serialize dict to JSON string (SQLite) or psycopg2.extras.Json (Postgres)."""
        if self._backend == "sqlite":
            return _dumps(obj)
        from psycopg2.extras import Json
        return Json(obj)

    # ── Context manager ───────────────────────────────────────

    @contextmanager
    def cursor(self) -> Iterator[Any]:
        if self._backend == "postgres":
            conn = self._pool.getconn()
            try:
                with conn.cursor() as cur:
                    yield cur
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                self._pool.putconn(conn)
        else:
            with self._lock:
                cur = self._sqlite_conn.cursor()
                try:
                    yield cur
                    self._sqlite_conn.commit()
                except Exception:
                    self._sqlite_conn.rollback()
                    raise

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """Explicit multi-statement transaction."""
        if self._backend == "postgres":
            conn = self._pool.getconn()
            try:
                with conn.cursor() as cur:
                    yield cur
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                self._pool.putconn(conn)
        else:
            with self._lock:
                cur = self._sqlite_conn.cursor()
                try:
                    cur.execute("BEGIN")
                    yield cur
                    self._sqlite_conn.commit()
                except Exception:
                    self._sqlite_conn.rollback()
                    raise

    # ── DDL bootstrap ─────────────────────────────────────────

    def bootstrap(self) -> None:
        """Create tables if they don't exist (dev / fresh deploy)."""
        import os
        schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
        if self._backend == "postgres":
            try:
                with open(schema_path) as f:
                    sql = f.read()
                with self.cursor() as cur:
                    cur.execute(sql)
                logger.info("[LedgerDB] Schema applied (Postgres)")
            except Exception as e:
                logger.warning("[LedgerDB] Schema bootstrap warning: %s", e)
        else:
            self._bootstrap_sqlite()

    def _bootstrap_sqlite(self) -> None:
        """Simplified schema for SQLite dev mode."""
        ddl = [
            """CREATE TABLE IF NOT EXISTS currency_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL DEFAULT 'default',
                currency TEXT NOT NULL,
                ledger_balance TEXT NOT NULL DEFAULT '0',
                available_balance TEXT NOT NULL DEFAULT '0',
                locked_balance TEXT NOT NULL DEFAULT '0',
                updated_at INTEGER NOT NULL,
                UNIQUE(user_id, currency))""",

            """CREATE TABLE IF NOT EXISTS ledger_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                currency TEXT NOT NULL,
                direction TEXT NOT NULL,
                amount TEXT NOT NULL,
                running_balance TEXT NOT NULL,
                event_type TEXT NOT NULL,
                reference_type TEXT,
                reference_id TEXT,
                rate_snapshot_id INTEGER,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL)""",

            """CREATE TABLE IF NOT EXISTS fx_rates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_currency TEXT NOT NULL,
                to_currency TEXT NOT NULL,
                bid TEXT NOT NULL,
                ask TEXT NOT NULL,
                mid TEXT NOT NULL,
                source TEXT NOT NULL,
                captured_at INTEGER NOT NULL)""",

            """CREATE TABLE IF NOT EXISTS wallet_snapshots_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                venue TEXT NOT NULL,
                account_id TEXT NOT NULL DEFAULT 'default',
                settlement_currency TEXT NOT NULL DEFAULT 'USDT',
                wallet_balance TEXT NOT NULL DEFAULT '0',
                margin_balance TEXT NOT NULL DEFAULT '0',
                available_balance TEXT NOT NULL DEFAULT '0',
                locked_balance TEXT NOT NULL DEFAULT '0',
                unrealized_pnl TEXT NOT NULL DEFAULT '0',
                realized_pnl TEXT NOT NULL DEFAULT '0',
                funding_accrued TEXT NOT NULL DEFAULT '0',
                fees_accrued TEXT NOT NULL DEFAULT '0',
                snapshot_at INTEGER NOT NULL)""",

            """CREATE TABLE IF NOT EXISTS positions_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_uid TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL DEFAULT 'default',
                venue TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty TEXT NOT NULL,
                entry_price_avg TEXT NOT NULL,
                mark_price TEXT,
                notional TEXT,
                leverage INTEGER NOT NULL DEFAULT 1,
                initial_margin TEXT,
                maintenance_margin TEXT,
                margin_used TEXT,
                liquidation_price TEXT,
                unrealized_pnl TEXT NOT NULL DEFAULT '0',
                realized_pnl TEXT NOT NULL DEFAULT '0',
                unrealized_pnl_pct TEXT NOT NULL DEFAULT '0',
                funding_accrued TEXT NOT NULL DEFAULT '0',
                fees_accrued TEXT NOT NULL DEFAULT '0',
                tp_price TEXT,
                sl_price TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                playbook TEXT,
                strategy TEXT,
                opened_at INTEGER NOT NULL,
                closed_at INTEGER,
                updated_at INTEGER NOT NULL)""",

            """CREATE TABLE IF NOT EXISTS orders_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_order_id TEXT NOT NULL UNIQUE,
                exchange_order_id TEXT,
                venue TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                intent TEXT NOT NULL DEFAULT 'entry',
                qty TEXT NOT NULL,
                price TEXT,
                stop_price TEXT,
                reduce_only INTEGER NOT NULL DEFAULT 0,
                post_only INTEGER NOT NULL DEFAULT 0,
                time_in_force TEXT DEFAULT 'GTC',
                status TEXT NOT NULL DEFAULT 'pending',
                filled_qty TEXT NOT NULL DEFAULT '0',
                avg_fill_price TEXT,
                remaining_qty TEXT,
                fee TEXT NOT NULL DEFAULT '0',
                fee_currency TEXT DEFAULT 'USDT',
                position_id INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL)""",

            """CREATE TABLE IF NOT EXISTS fills_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                exchange_trade_id TEXT,
                venue TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty TEXT NOT NULL,
                price TEXT NOT NULL,
                fee TEXT NOT NULL DEFAULT '0',
                fee_currency TEXT DEFAULT 'USDT',
                is_maker INTEGER NOT NULL DEFAULT 0,
                position_id INTEGER,
                created_at INTEGER NOT NULL)""",

            """CREATE TABLE IF NOT EXISTS reconciliation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                venue TEXT NOT NULL,
                check_type TEXT NOT NULL,
                status TEXT NOT NULL,
                local_value TEXT,
                exchange_value TEXT,
                delta TEXT,
                action_taken TEXT,
                created_at INTEGER NOT NULL)""",
        ]
        with self.cursor() as cur:
            for stmt in ddl:
                cur.execute(stmt)
        logger.info("[LedgerDB] SQLite schema bootstrapped")

    def close(self) -> None:
        if self._backend == "postgres" and self._pool:
            self._pool.closeall()
        elif self._sqlite_conn:
            self._sqlite_conn.close()
