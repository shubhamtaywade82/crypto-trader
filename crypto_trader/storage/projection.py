"""
crypto_trader.storage.projection — relational read-model over the event store
==============================================================================
The event journal (``events`` JSONB) remains the source of truth. This module
derives queryable projection tables from it so a UI / analytics can run plain
SQL (``SELECT * FROM active_positions WHERE mode='live'``) instead of replaying
JSONB. It is a *projection*: rebuildable at any time from the event stream.

Two pieces:

* :class:`ProjectionState` — a pure, in-memory reducer (``apply(event)``) that is
  trivially testable and dialect-agnostic.
* :class:`PostgresProjection` — persists the reduced rows into ``active_positions``,
  ``orders`` and ``fills`` (lazy ``psycopg2``). Subscribe ``.apply`` to the wallet
  event hook for live updates, or call ``rebuild_from(events)`` on boot.

A ``RECON_ADJUST`` event lets the reconciler inject truth corrections that the
projection applies just like any other event.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger("crypto_trader.storage.projection")


@dataclass
class ProjectionState:
    """Reduces domain events into orders / active positions / fills."""
    mode: str = "live"
    orders: Dict[str, dict] = field(default_factory=dict)
    positions: Dict[str, dict] = field(default_factory=dict)  # active only, keyed by symbol
    fills: List[dict] = field(default_factory=list)

    def apply(self, event: dict) -> None:
        et = event.get("event_type")
        p = event.get("payload", {}) or {}
        ts = int(event.get("ts", 0) or 0)
        mode = p.get("mode", self.mode)
        sym = p.get("symbol")

        if et == "ORDER_CREATED":
            oid = p.get("order_id")
            if oid:
                self.orders[oid] = {
                    "exchange_order_id": oid, "symbol": sym, "side": p.get("side"),
                    "mode": mode, "order_type": p.get("order_type", "MARKET"),
                    "qty": _f(p.get("quantity")), "filled_qty": 0.0,
                    "avg_fill_price": 0.0, "status": p.get("status", "NEW"),
                    "created_at": ts,
                }
        elif et in ("ORDER_FILLED", "ORDER_PARTIALLY_FILLED"):
            oid = p.get("order_id")
            o = self.orders.get(oid)
            if o:
                o["filled_qty"] = o.get("filled_qty", 0.0) + _f(p.get("quantity"))
                o["avg_fill_price"] = _f(p.get("fill_price")) or o["avg_fill_price"]
                o["status"] = p.get("status", "FILLED")
            self.fills.append({
                "exchange_order_id": oid, "symbol": sym, "side": p.get("side"),
                "mode": mode, "price": _f(p.get("fill_price")), "qty": _f(p.get("quantity")),
                "fee": _f(p.get("fee")), "ts": ts,
            })
        elif et in ("ORDER_CANCELLED", "ORDER_REJECTED", "ORDER_EXPIRED"):
            oid = p.get("order_id")
            if oid in self.orders:
                self.orders[oid]["status"] = et.replace("ORDER_", "")
        elif et == "POSITION_OPENED":
            if sym:
                self.positions[sym] = {
                    "symbol": sym, "mode": mode, "side": p.get("side"),
                    "qty": _f(p.get("quantity")),
                    "avg_price": _f(p.get("execution_price", p.get("entry_price"))),
                    "status": "OPEN", "last_event_ts": ts,
                }
        elif et == "POSITION_PARTIALLY_CLOSED":
            pos = self.positions.get(sym)
            if pos:
                pos["qty"] = max(0.0, pos["qty"] - _f(p.get("closed_qty")))
                pos["last_event_ts"] = ts
        elif et in ("POSITION_CLOSED", "LIQUIDATION"):
            self.positions.pop(sym, None)
        elif et == "RECON_ADJUST":
            # Truth correction injected by the reconciler.
            if sym:
                qty = _f(p.get("quantity"))
                if qty <= 0:
                    self.positions.pop(sym, None)
                else:
                    self.positions[sym] = {
                        "symbol": sym, "mode": mode, "side": p.get("side"),
                        "qty": qty, "avg_price": _f(p.get("avg_price")),
                        "status": "OPEN", "last_event_ts": ts,
                    }

    def rebuild(self, events: Iterable[dict]) -> "ProjectionState":
        for e in events:
            self.apply(e)
        return self


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


_DDL = """
CREATE TABLE IF NOT EXISTS active_positions (
    symbol        TEXT NOT NULL,
    mode          TEXT NOT NULL,
    side          TEXT,
    qty           NUMERIC,
    avg_price     NUMERIC,
    status        TEXT,
    last_event_ts BIGINT,
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (symbol, mode)
);
CREATE TABLE IF NOT EXISTS orders (
    exchange_order_id TEXT NOT NULL,
    mode              TEXT NOT NULL,
    symbol            TEXT,
    side              TEXT,
    order_type        TEXT,
    qty               NUMERIC,
    filled_qty        NUMERIC,
    avg_fill_price    NUMERIC,
    status            TEXT,
    created_at        BIGINT,
    PRIMARY KEY (exchange_order_id, mode)
);
CREATE TABLE IF NOT EXISTS fills (
    id                BIGSERIAL PRIMARY KEY,
    exchange_order_id TEXT,
    symbol            TEXT,
    side              TEXT,
    mode              TEXT,
    price             NUMERIC,
    qty               NUMERIC,
    fee               NUMERIC,
    ts                BIGINT
);
CREATE INDEX IF NOT EXISTS idx_active_positions_mode ON active_positions (mode);
CREATE INDEX IF NOT EXISTS idx_orders_mode_status ON orders (mode, status);
"""


class PostgresProjection:
    """Persists the projection into Postgres. Subscribe ``apply`` to the wallet
    event hook, or call ``rebuild_from`` on boot to materialize from the journal."""

    def __init__(self, dsn: str, *, mode: str = "live"):
        import psycopg2  # lazy
        self._psycopg2 = psycopg2
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = True
        self.mode = mode
        with self.conn.cursor() as cur:
            cur.execute(_DDL)
        logger.info("Postgres projection ready")

    def apply(self, event: dict) -> None:
        """Apply one event to the projection tables (idempotent upserts).

        Events whose effect depends on prior state (ORDER_FILLED, cancellations,
        partial closes) are applied as incremental SQL against the already-
        persisted row, because each call uses a fresh reducer that has no memory
        of earlier events in this process. ``rebuild_from`` keeps the stateful
        reducer for full replays.
        """
        st = ProjectionState(mode=self.mode)
        st.apply(event)
        et = event.get("event_type")
        p = event.get("payload", {}) or {}
        mode = p.get("mode", self.mode)
        with self.conn.cursor() as cur:
            for o in st.orders.values():
                self._upsert_order(cur, o)
            for sym, pos in st.positions.items():
                self._upsert_position(cur, pos)
            for f in st.fills:
                self._insert_fill(cur, f)

            if et in ("ORDER_FILLED", "ORDER_PARTIALLY_FILLED"):
                oid = p.get("order_id")
                if oid:
                    cur.execute(
                        """UPDATE orders
                              SET filled_qty = COALESCE(filled_qty, 0) + %s,
                                  avg_fill_price = COALESCE(NULLIF(%s, 0), avg_fill_price),
                                  status = %s
                            WHERE exchange_order_id = %s AND mode = %s""",
                        (_f(p.get("quantity")), _f(p.get("fill_price")),
                         p.get("status", "FILLED"), oid, mode),
                    )
            elif et in ("ORDER_CANCELLED", "ORDER_REJECTED", "ORDER_EXPIRED"):
                oid = p.get("order_id")
                if oid:
                    cur.execute(
                        "UPDATE orders SET status=%s WHERE exchange_order_id=%s AND mode=%s",
                        (et.replace("ORDER_", ""), oid, mode),
                    )
            elif et == "POSITION_PARTIALLY_CLOSED":
                sym = p.get("symbol")
                if sym:
                    cur.execute(
                        """UPDATE active_positions
                              SET qty = GREATEST(0, COALESCE(qty, 0) - %s),
                                  last_event_ts = %s, updated_at = NOW()
                            WHERE symbol=%s AND mode=%s""",
                        (_f(p.get("closed_qty")), int(event.get("ts", 0) or 0), sym, mode),
                    )
            elif et in ("POSITION_CLOSED", "LIQUIDATION"):
                sym = p.get("symbol")
                if sym:
                    cur.execute("DELETE FROM active_positions WHERE symbol=%s AND mode=%s", (sym, mode))

    def rebuild_from(self, events: Iterable[dict]) -> None:
        """Truncate and rematerialize the projection from the event stream."""
        st = ProjectionState(mode=self.mode).rebuild(events)
        with self.conn.cursor() as cur:
            cur.execute("TRUNCATE active_positions, orders, fills RESTART IDENTITY")
            for o in st.orders.values():
                self._upsert_order(cur, o)
            for pos in st.positions.values():
                self._upsert_position(cur, pos)
            for f in st.fills:
                self._insert_fill(cur, f)

    # ── row writers ──────────────────────────────────────────────────────────
    def _upsert_position(self, cur, pos: dict) -> None:
        cur.execute(
            """INSERT INTO active_positions (symbol, mode, side, qty, avg_price, status, last_event_ts, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s, NOW())
               ON CONFLICT (symbol, mode) DO UPDATE SET
                 side=EXCLUDED.side, qty=EXCLUDED.qty, avg_price=EXCLUDED.avg_price,
                 status=EXCLUDED.status, last_event_ts=EXCLUDED.last_event_ts, updated_at=NOW()""",
            (pos["symbol"], pos.get("mode", self.mode), pos.get("side"), pos.get("qty"),
             pos.get("avg_price"), pos.get("status"), pos.get("last_event_ts")),
        )

    def _upsert_order(self, cur, o: dict) -> None:
        cur.execute(
            """INSERT INTO orders (exchange_order_id, mode, symbol, side, order_type, qty,
                                   filled_qty, avg_fill_price, status, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (exchange_order_id, mode) DO UPDATE SET
                 filled_qty=EXCLUDED.filled_qty, avg_fill_price=EXCLUDED.avg_fill_price,
                 status=EXCLUDED.status""",
            (o["exchange_order_id"], o.get("mode", self.mode), o.get("symbol"), o.get("side"),
             o.get("order_type"), o.get("qty"), o.get("filled_qty"), o.get("avg_fill_price"),
             o.get("status"), o.get("created_at")),
        )

    def _insert_fill(self, cur, f: dict) -> None:
        cur.execute(
            """INSERT INTO fills (exchange_order_id, symbol, side, mode, price, qty, fee, ts)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (f.get("exchange_order_id"), f.get("symbol"), f.get("side"), f.get("mode", self.mode),
             f.get("price"), f.get("qty"), f.get("fee"), f.get("ts")),
        )

    # ── query helpers (for the UI / analytics) ───────────────────────────────
    def open_positions(self, mode: Optional[str] = None) -> List[dict]:
        """Active positions, optionally filtered by mode ('paper'/'live')."""
        q = "SELECT symbol, mode, side, qty, avg_price, status, last_event_ts FROM active_positions"
        args: tuple = ()
        if mode:
            q += " WHERE mode=%s"
            args = (mode,)
        with self.conn.cursor() as cur:
            cur.execute(q, args)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def pnl_summary(self, mode: Optional[str] = None) -> dict:
        """Aggregate realized fees/qty by mode — split or combined for the UI."""
        q = ("SELECT mode, COUNT(*) AS fills, COALESCE(SUM(fee),0) AS total_fees, "
             "COALESCE(SUM(qty),0) AS total_qty FROM fills")
        args: tuple = ()
        if mode:
            q += " WHERE mode=%s"
            args = (mode,)
        q += " GROUP BY mode"
        with self.conn.cursor() as cur:
            cur.execute(q, args)
            cols = [c[0] for c in cur.description]
            return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
