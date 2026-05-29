"""
ReconciliationWorker — compare local ledger state against exchange truth.

What it does
------------
1. Fetch the exchange's authoritative wallet balance and open positions.
2. Compare with the local projection (from WalletProjectionService /
   PositionEngine).
3. If drift exceeds the configured tolerance, log it, persist a
   reconciliation_logs row, and optionally freeze new orders.
4. For positions, compute the delta per symbol and emit a ``ReconciliationResult``
   so the caller can decide whether to auto-repair or alert.

What it does NOT do
-------------------
* It does NOT automatically modify the ledger (that is the operator's job).
  ``recon_adjust()`` on LedgerService exists for emergency corrections.
* It does NOT connect to exchanges directly — it accepts a duck-typed
  ``ExchangeSnapshot`` dict so the same worker can be used with any venue.

Usage::

    worker = ReconciliationWorker(db, position_engine, user_id="default")
    result = worker.run(venue="binance", exchange_snapshot={
        "wallet_balance": "5000.00",
        "available_balance": "3200.00",
        "positions": [
            {"symbol": "BTCUSDT", "qty": "0.05", "entry_price": "68000", ...}
        ],
    })
    if result.status == "drift":
        alert(result)
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .db import LedgerDB
from .models import Position, ReconciliationResult
from .position_engine import PositionEngine
from .wallet_projection import WalletProjectionService

logger = logging.getLogger("crypto_trader.ledger.reconciliation")

_D = Decimal
_ZERO = _D("0")

_NOW_MS = lambda: int(time.time() * 1000)


class ReconciliationWorker:
    """
    Stateless reconciliation worker — instantiate once, call ``run()``
    on a schedule (e.g. every 30 s from a background thread).

    Parameters
    ----------
    db:
        Shared LedgerDB.
    position_engine:
        PositionEngine for the same user_id.
    user_id:
        Owner of the positions/wallet being reconciled.
    balance_tolerance_usdt:
        Maximum acceptable absolute difference between local and exchange
        wallet balance before the result is flagged as ``drift``.
        Default 1.00 USDT.
    position_qty_tolerance:
        Maximum acceptable fractional difference in position size.
        Default 0.5% (0.005).
    """

    def __init__(
        self,
        db: LedgerDB,
        position_engine: PositionEngine,
        user_id: str = "default",
        balance_tolerance_usdt: _D = _D("1.00"),
        position_qty_tolerance: _D = _D("0.005"),
    ) -> None:
        self._db = db
        self._pe = position_engine
        self._user_id = user_id
        self._bal_tol = balance_tolerance_usdt
        self._qty_tol = position_qty_tolerance
        self._projection = WalletProjectionService(db, user_id)

    # ── Public ────────────────────────────────────────────────────────────

    def run(
        self,
        venue: str,
        exchange_snapshot: Dict[str, Any],
    ) -> List[ReconciliationResult]:
        """
        Execute a full reconciliation for the given venue.

        ``exchange_snapshot`` expected shape::

            {
                "wallet_balance":    "5000.00",   # str or float
                "available_balance": "3200.00",
                "positions": [
                    {
                        "symbol":      "BTCUSDT",
                        "side":        "LONG",    # or "SHORT"
                        "qty":         "0.05",    # absolute size
                        "entry_price": "68000.0",
                    },
                    ...
                ],
            }

        Returns a list of ReconciliationResult objects (one per check).
        """
        results: List[ReconciliationResult] = []

        # ── 1. Balance reconciliation ─────────────────────────────────────
        bal_result = self._check_balance(venue, exchange_snapshot)
        results.append(bal_result)
        self._persist(bal_result)

        # ── 2. Position reconciliation ────────────────────────────────────
        pos_results = self._check_positions(venue, exchange_snapshot)
        for r in pos_results:
            results.append(r)
            self._persist(r)

        ok_count  = sum(1 for r in results if r.status == "ok")
        bad_count = len(results) - ok_count
        logger.info(
            "[Reconciliation] venue=%s ok=%d drift/error=%d",
            venue, ok_count, bad_count,
        )
        return results

    def is_healthy(
        self,
        venue: str,
        exchange_snapshot: Dict[str, Any],
    ) -> bool:
        """Convenience wrapper — returns True only if all checks pass."""
        return all(r.status == "ok" for r in self.run(venue, exchange_snapshot))

    # ── Balance check ─────────────────────────────────────────────────────

    def _check_balance(
        self, venue: str, snap: Dict[str, Any]
    ) -> ReconciliationResult:
        ex_balance = _D(str(snap.get("wallet_balance", 0)))
        local_bal  = self._projection.account_balance("USDT")
        if local_bal is None:
            return ReconciliationResult(
                venue=venue,
                check_type="balance",
                status="error",
                local_value=None,
                exchange_value={"wallet_balance": str(ex_balance)},
                action_taken="no_local_account",
            )

        local_ledger = local_bal["ledger_balance"]
        delta = abs(ex_balance - local_ledger)

        status = "ok" if delta <= self._bal_tol else "drift"
        if status == "drift":
            logger.warning(
                "[Reconciliation] Balance drift on %s: local=%s exchange=%s delta=%s",
                venue, local_ledger, ex_balance, delta,
            )

        return ReconciliationResult(
            venue=venue,
            check_type="balance",
            status=status,
            local_value={"ledger_balance": str(local_ledger)},
            exchange_value={"wallet_balance": str(ex_balance)},
            delta={"abs_delta_usdt": str(delta)},
            action_taken="none",
        )

    # ── Position check ────────────────────────────────────────────────────

    def _check_positions(
        self, venue: str, snap: Dict[str, Any]
    ) -> List[ReconciliationResult]:
        exchange_positions: Dict[str, Dict] = {}
        for p in snap.get("positions", []):
            sym  = p.get("symbol", "")
            qty  = _D(str(p.get("qty", p.get("positionAmt", 0))))
            if sym and abs(qty) > _ZERO:
                exchange_positions[sym] = {
                    "symbol": sym,
                    "qty": qty,
                    "side": p.get("side", "LONG" if qty > _ZERO else "SHORT"),
                    "entry_price": _D(str(p.get("entry_price", p.get("entryPrice", 0)))),
                }

        local_positions: Dict[str, Position] = {
            p.symbol: p for p in self._pe.list_open_positions(venue)
        }

        results: List[ReconciliationResult] = []

        # Check positions that exist on exchange
        for sym, ex_pos in exchange_positions.items():
            local = local_positions.get(sym)

            if local is None:
                results.append(ReconciliationResult(
                    venue=venue,
                    check_type="position",
                    status="drift",
                    local_value=None,
                    exchange_value={"symbol": sym, "qty": str(ex_pos["qty"])},
                    delta={"missing_locally": True},
                    action_taken="alert",
                ))
                logger.warning(
                    "[Reconciliation] Position on exchange not in local: %s %s",
                    venue, sym,
                )
                continue

            qty_delta = abs(local.qty - abs(ex_pos["qty"]))
            qty_pct   = qty_delta / abs(ex_pos["qty"]) if ex_pos["qty"] != _ZERO else _ZERO
            status    = "ok" if qty_pct <= self._qty_tol else "drift"

            results.append(ReconciliationResult(
                venue=venue,
                check_type="position",
                status=status,
                local_value={"symbol": sym, "qty": str(local.qty),
                             "entry": str(local.entry_price_avg)},
                exchange_value={"symbol": sym, "qty": str(ex_pos["qty"]),
                                "entry": str(ex_pos["entry_price"])},
                delta={"qty_delta": str(qty_delta), "qty_pct": str(qty_pct)},
                action_taken="none",
            ))

            if status == "drift":
                logger.warning(
                    "[Reconciliation] Position qty drift %s/%s: local=%s exchange=%s",
                    venue, sym, local.qty, ex_pos["qty"],
                )

        # Check positions that exist locally but NOT on exchange (ghost positions)
        for sym, local in local_positions.items():
            if sym not in exchange_positions:
                results.append(ReconciliationResult(
                    venue=venue,
                    check_type="position",
                    status="drift",
                    local_value={"symbol": sym, "qty": str(local.qty)},
                    exchange_value=None,
                    delta={"ghost_position": True},
                    action_taken="alert",
                ))
                logger.warning(
                    "[Reconciliation] Ghost position locally not on exchange: %s %s",
                    venue, sym,
                )

        if not results:
            results.append(ReconciliationResult(
                venue=venue,
                check_type="position",
                status="ok",
                local_value={"open_count": 0},
                exchange_value={"open_count": 0},
            ))

        return results

    # ── Persistence ───────────────────────────────────────────────────────

    def _persist(self, result: ReconciliationResult) -> None:
        import json
        sql = self._db._adapt(
            "INSERT INTO reconciliation_logs "
            "(venue, check_type, status, local_value, exchange_value, "
            " delta, action_taken, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
        )
        try:
            with self._db.cursor() as cur:
                cur.execute(sql, (
                    result.venue,
                    result.check_type,
                    result.status,
                    self._db._jsonb(result.local_value),
                    self._db._jsonb(result.exchange_value),
                    self._db._jsonb(result.delta),
                    result.action_taken or "",
                    result.created_at,
                ))
        except Exception as exc:
            logger.error("[Reconciliation] Failed to persist result: %s", exc)

    def get_recent_logs(
        self,
        venue: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return recent reconciliation log rows (newest first)."""
        if venue:
            sql = self._db._adapt(
                "SELECT venue, check_type, status, local_value, exchange_value, "
                "delta, action_taken, created_at "
                "FROM reconciliation_logs "
                "WHERE venue = %s ORDER BY created_at DESC LIMIT %s"
            )
            params = (venue, limit)
        else:
            sql = self._db._adapt(
                "SELECT venue, check_type, status, local_value, exchange_value, "
                "delta, action_taken, created_at "
                "FROM reconciliation_logs "
                "ORDER BY created_at DESC LIMIT %s"
            )
            params = (limit,)

        with self._db.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        return [
            {
                "venue": r[0], "check_type": r[1], "status": r[2],
                "local_value": r[3], "exchange_value": r[4],
                "delta": r[5], "action_taken": r[6], "created_at": r[7],
            }
            for r in rows
        ]
