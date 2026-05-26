"""
crypto_trader.arb_book — Delta-neutral funding-arb position book
=================================================================
Funding arbitrage holds TWO legs (long spot + short perp) with no directional
SL/TP — it is a yield play tracking accrued funding and basis. That does not fit
the single-leg directional ``EnhancedPosition``/``EnhancedFuturesWallet`` model
(one position per symbol, sl/tp/time-stop, directional funding sign, directional
invariants), so arb gets its own lightweight ``ArbPosition`` + an event-sourced
``ArbBook`` with a SEPARATE SQLite journal. Keeping the journals separate means
the directional reducer never sees arb events (and vice-versa) and both remain
independently replayable for crash recovery.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("crypto_trader.arb_book")

DATA_DIR = Path.home() / ".crypto_trader"

# Lifecycle states (entry/unwind FSMs live in FundingArbManager).
ST_PLACING_PERP = "PLACING_PERP"
ST_PERP_ONLY = "PERP_ONLY"
ST_HEDGED = "HEDGED"
ST_UNWINDING_PERP = "UNWINDING_PERP"
ST_UNWINDING_SPOT = "UNWINDING_SPOT"
ST_CLOSED = "CLOSED"
_NON_TERMINAL = {ST_PLACING_PERP, ST_PERP_ONLY, ST_UNWINDING_PERP, ST_UNWINDING_SPOT}


def _d(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


@dataclass
class ArbPosition:
    arb_id: str
    symbol: str
    state: str
    spot_qty: Decimal = Decimal("0")
    perp_qty: Decimal = Decimal("0")
    spot_entry_price: Decimal = Decimal("0")
    perp_entry_price: Decimal = Decimal("0")
    entry_basis: Decimal = Decimal("0")
    accrued_funding: Decimal = Decimal("0")
    funding_payments: int = 0
    entry_fees: Decimal = Decimal("0")
    spot_order_id: str = ""
    perp_order_id: str = ""
    perp_position_id: str = ""
    open_time: int = 0
    last_funding_ts: int = 0
    close_time: Optional[int] = None
    close_reason: Optional[str] = None
    realized_pnl: Decimal = Decimal("0")

    def to_dict(self) -> dict:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, Decimal):
                d[k] = str(v)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ArbPosition":
        dec_fields = {
            "spot_qty", "perp_qty", "spot_entry_price", "perp_entry_price",
            "entry_basis", "accrued_funding", "entry_fees", "realized_pnl",
        }
        kwargs = {}
        for f in cls.__dataclass_fields__:
            if f not in d:
                continue
            kwargs[f] = _d(d[f]) if f in dec_fields and d[f] is not None else d[f]
        return cls(**kwargs)


class ArbBook:
    """Event-sourced store for funding-arb positions (own SQLite DB + JSONL)."""

    def __init__(self, namespace: str = "default", data_dir: Optional[Path] = None,
                 database_url: str = ""):
        import os as _os
        self.namespace = namespace
        self.dir = data_dir or DATA_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / f"arb_{namespace}.db"
        self.journal_path = self.dir / f"arb_events_{namespace}.jsonl"
        self._database_url = database_url or _os.getenv("DATABASE_URL", "")
        self._open: Dict[str, ArbPosition] = {}
        self._init_db()
        self._load_open()

    # ── persistence ──
    def _pg_conn(self):
        import psycopg2
        return psycopg2.connect(self._database_url)

    def _init_db(self):
        if self._database_url:
            # Tables are created by WalletStore.init_schema() when database_url is set.
            # Nothing to do here; arb_events and arb_positions already exist.
            return
        con = sqlite3.connect(self.db_path)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(
                "CREATE TABLE IF NOT EXISTS arb_events ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, arb_id TEXT, "
                "event_type TEXT, payload TEXT)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS arb_positions ("
                "arb_id TEXT PRIMARY KEY, symbol TEXT, state TEXT, snapshot TEXT, updated_ts INTEGER)"
            )
            con.commit()
        finally:
            con.close()

    def _emit(self, event_type: str, arb_id: str, payload: dict):
        ts = int(time.time() * 1000)
        rec = {"ts": ts, "arb_id": arb_id, "event_type": event_type, "payload": payload}
        if self._database_url:
            conn = self._pg_conn()
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO arb_events (ts, arb_id, event_type, payload) VALUES (%s,%s,%s,%s)",
                    (ts, arb_id, event_type, json.dumps(payload, default=str)),
                )
                conn.commit()
            finally:
                conn.close()
        else:
            con = sqlite3.connect(self.db_path)
            try:
                con.execute(
                    "INSERT INTO arb_events (ts, arb_id, event_type, payload) VALUES (?,?,?,?)",
                    (ts, arb_id, event_type, json.dumps(payload, default=str)),
                )
                con.commit()
            finally:
                con.close()
        try:
            with self.journal_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError as e:
            logger.warning("arb journal append failed: %s", e)

    def _snapshot(self, arb: ArbPosition):
        if self._database_url:
            conn = self._pg_conn()
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO arb_positions (arb_id, symbol, state, snapshot, updated_ts) "
                    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT(arb_id) DO UPDATE SET "
                    "state=EXCLUDED.state, snapshot=EXCLUDED.snapshot, updated_ts=EXCLUDED.updated_ts",
                    (arb.arb_id, arb.symbol, arb.state, json.dumps(arb.to_dict()),
                     int(time.time() * 1000)),
                )
                conn.commit()
            finally:
                conn.close()
        else:
            con = sqlite3.connect(self.db_path)
            try:
                con.execute(
                    "INSERT INTO arb_positions (arb_id, symbol, state, snapshot, updated_ts) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(arb_id) DO UPDATE SET "
                    "state=excluded.state, snapshot=excluded.snapshot, updated_ts=excluded.updated_ts",
                    (arb.arb_id, arb.symbol, arb.state, json.dumps(arb.to_dict()),
                     int(time.time() * 1000)),
                )
                con.commit()
            finally:
                con.close()

    def _load_open(self):
        if self._database_url:
            conn = self._pg_conn()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT snapshot FROM arb_positions WHERE state != %s", (ST_CLOSED,)
                )
                rows = cur.fetchall()
            finally:
                conn.close()
        else:
            con = sqlite3.connect(self.db_path)
            try:
                rows = con.execute(
                    "SELECT snapshot FROM arb_positions WHERE state != ?", (ST_CLOSED,)
                ).fetchall()
            finally:
                con.close()
        for (snap,) in rows:
            try:
                arb = ArbPosition.from_dict(json.loads(snap))
                self._open[arb.symbol] = arb
            except Exception as e:
                logger.warning("failed to load arb snapshot: %s", e)

    # ── lifecycle API (called by FundingArbManager) ──
    def open_record(self, arb: ArbPosition):
        self._open[arb.symbol] = arb
        self._emit("ARB_OPENING", arb.arb_id, arb.to_dict())
        self._snapshot(arb)

    def update(self, arb: ArbPosition, event_type: str, payload: Optional[dict] = None):
        if arb.state == ST_CLOSED:
            self._open.pop(arb.symbol, None)
        else:
            self._open[arb.symbol] = arb
        self._emit(event_type, arb.arb_id, payload or arb.to_dict())
        self._snapshot(arb)

    def record_funding(self, arb: ArbPosition, rate: Decimal, perp_notional: Decimal, amount: Decimal):
        arb.accrued_funding += _d(amount)
        arb.funding_payments += 1
        arb.last_funding_ts = int(time.time() * 1000)
        self.update(arb, "ARB_FUNDING_ACCRUED", {
            "arb_id": arb.arb_id, "rate": str(rate), "perp_notional": str(perp_notional),
            "amount": str(amount), "accrued_funding": str(arb.accrued_funding),
        })

    def close(self, arb: ArbPosition, reason: str, realized_pnl: Decimal):
        arb.state = ST_CLOSED
        arb.close_reason = reason
        arb.close_time = int(time.time() * 1000)
        arb.realized_pnl = _d(realized_pnl)
        self._open.pop(arb.symbol, None)
        self._emit("ARB_CLOSED", arb.arb_id, arb.to_dict())
        self._snapshot(arb)

    def get_open(self, symbol: str) -> Optional[ArbPosition]:
        return self._open.get(symbol)

    def all_open(self) -> List[ArbPosition]:
        return list(self._open.values())

    def non_terminal(self) -> List[ArbPosition]:
        """Open arbs stuck mid-FSM that need reconcile-then-resolve on startup."""
        return [a for a in self._open.values() if a.state in _NON_TERMINAL]
