"""
WalletProjectionService — computes a live WalletSnapshot from ledger + positions.

This is the "projection" layer that synthesises the canonical wallet view
from first principles every time it is called.  It does NOT maintain state
between calls (that is Redis's job in the hot layer).

Design contract
---------------
* ``build_snapshot(venue, positions)`` is the single public entry point.
* All monetary arithmetic uses Decimal throughout.
* The result is a ``WalletSnapshot`` dataclass that can be serialised and
  pushed to Redis / returned to callers.
* This class is intentionally side-effect-free after construction: it reads
  the DB but never writes it.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Dict, List, Optional

from .db import LedgerDB
from .models import Position, WalletSnapshot

logger = logging.getLogger("crypto_trader.ledger.wallet_projection")

_D = Decimal
_ZERO = _D("0")


class WalletProjectionService:
    """
    Compute a WalletSnapshot by reading currency_accounts and an
    externally-supplied list of open positions.

    Parameters
    ----------
    db:
        Shared LedgerDB (read-only queries here).
    user_id:
        Owner of the accounts being projected.
    settlement_currency:
        The currency used for margin / PnL (almost always 'USDT').
    """

    def __init__(
        self,
        db: LedgerDB,
        user_id: str = "default",
        settlement_currency: str = "USDT",
    ) -> None:
        self._db = db
        self._user_id = user_id
        self._settlement = settlement_currency

    # ── Public ────────────────────────────────────────────────────────────

    def build_snapshot(
        self,
        venue: str,
        positions: Optional[List[Position]] = None,
    ) -> WalletSnapshot:
        """
        Build a full WalletSnapshot for ``venue``.

        ``positions`` should be the list of *open* Position objects
        already refreshed with the latest mark price (so unrealized_pnl
        is current). Pass ``[]`` if there are no open positions.
        """
        positions = positions or []

        # ── Read ledger account ──────────────────────────────────────────
        acct = self._load_account(self._settlement)
        if acct is None:
            # No account yet — return a zero snapshot
            return WalletSnapshot(
                venue=venue,
                settlement_currency=self._settlement,
                account_id=self._user_id,
            )

        ledger_bal, available_bal, locked_bal = acct

        # ── Aggregate position metrics ───────────────────────────────────
        unrealized_pnl = _ZERO
        funding_accrued = _ZERO
        fees_accrued    = _ZERO
        additional_locked = _ZERO   # margin_used by open positions

        for pos in positions:
            if pos.status != "open":
                continue
            unrealized_pnl  += pos.unrealized_pnl
            funding_accrued += pos.funding_accrued
            fees_accrued    += pos.fees_accrued
            if pos.margin_used is not None:
                additional_locked += pos.margin_used

        # ── Derive wallet-level fields ────────────────────────────────────
        # wallet_balance  = ledger (realised only, no open-PnL)
        # margin_balance  = wallet_balance + unrealized_pnl
        # locked_balance  = what's reserved for open margin
        # available       = what can still be used for new orders
        wallet_balance  = ledger_bal
        margin_balance  = wallet_balance + unrealized_pnl
        # locked_balance from ledger (reserve/release events) OR
        # the sum of position margin_used — take the max to avoid drift.
        effective_locked = max(locked_bal, additional_locked)

        # Read realised PnL total from ledger (sum of PnL entries)
        realized_pnl = self._query_realized_pnl()

        snap = WalletSnapshot(
            venue=venue,
            settlement_currency=self._settlement,
            wallet_balance=wallet_balance,
            margin_balance=margin_balance,
            available_balance=available_bal,
            locked_balance=effective_locked,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=realized_pnl,
            funding_accrued=funding_accrued,
            fees_accrued=fees_accrued,
            account_id=self._user_id,
        )
        return snap

    def account_balance(self, currency: str) -> Optional[Dict[str, _D]]:
        """
        Return {ledger_balance, available_balance, locked_balance} for a
        currency, or None if no account exists.
        """
        row = self._load_account(currency)
        if row is None:
            return None
        return {
            "ledger_balance":    row[0],
            "available_balance": row[1],
            "locked_balance":    row[2],
        }

    def replay_balance(self, currency: str) -> _D:
        """
        Replay all ledger entries to recompute the authoritative balance.
        Useful for integrity checks; not meant for hot path.
        """
        sql = self._db._adapt(
            "SELECT id FROM currency_accounts "
            "WHERE user_id = %s AND currency = %s"
        )
        with self._db.cursor() as cur:
            cur.execute(sql, (self._user_id, currency.upper()))
            row = cur.fetchone()
        if row is None:
            return _ZERO

        acct_id = row[0]
        sql2 = self._db._adapt(
            "SELECT direction, amount FROM ledger_entries "
            "WHERE account_id = %s ORDER BY id ASC"
        )
        balance = _ZERO
        with self._db.cursor() as cur:
            cur.execute(sql2, (acct_id,))
            for direction, amount in cur.fetchall():
                amt = _D(str(amount))
                balance = balance + amt if direction == "credit" else balance - amt
        return balance

    # ── Private ───────────────────────────────────────────────────────────

    def _load_account(
        self, currency: str
    ) -> Optional[tuple]:
        """Return (ledger_balance, available_balance, locked_balance) or None."""
        sql = self._db._adapt(
            "SELECT ledger_balance, available_balance, locked_balance "
            "FROM currency_accounts "
            "WHERE user_id = %s AND currency = %s"
        )
        with self._db.cursor() as cur:
            cur.execute(sql, (self._user_id, currency.upper()))
            row = cur.fetchone()
        if row is None:
            return None
        return (_D(str(row[0])), _D(str(row[1])), _D(str(row[2])))

    def _query_realized_pnl(self) -> _D:
        """
        Sum all REALIZED_PNL_CREDIT and REALIZED_PNL_DEBIT entries for this user.
        Returns net realized PnL (positive = net profit).
        """
        sql = self._db._adapt(
            "SELECT le.direction, le.amount "
            "FROM ledger_entries le "
            "JOIN currency_accounts ca ON ca.id = le.account_id "
            "WHERE ca.user_id = %s "
            "  AND ca.currency = %s "
            "  AND le.event_type IN (%s, %s)"
        )
        total = _ZERO
        try:
            with self._db.cursor() as cur:
                cur.execute(sql, (
                    self._user_id,
                    self._settlement,
                    "REALIZED_PNL_CREDIT",
                    "REALIZED_PNL_DEBIT",
                ))
                for direction, amount in cur.fetchall():
                    amt = _D(str(amount))
                    total = total + amt if direction == "credit" else total - amt
        except Exception as exc:
            logger.warning("[WalletProjection] PnL query failed: %s", exc)
        return total
