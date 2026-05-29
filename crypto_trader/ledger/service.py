"""
LedgerService — double-entry bookkeeping engine.

Every value movement (deposit, fee, PnL, conversion, margin lock/release)
is recorded as a pair of debit/credit journal entries.  The ledger_balance
on currency_accounts is always derived from replaying these entries — it is
never set directly.

Thread-safe via per-account row-level locking in Postgres (SELECT … FOR UPDATE).
In SQLite dev mode, the entire LedgerDB uses an RLock, so serialisation is
implicitly guaranteed.
"""

from __future__ import annotations

import json
import logging
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional

from .db import LedgerDB
from .models import CurrencyAccount, FxRate, LedgerEntry, WalletSnapshot
from . import event_types as ET

logger = logging.getLogger("crypto_trader.ledger.service")

_D = Decimal
_ZERO = _D("0")
_TAKER_FEE = _D("0.0005")   # 0.05 %  — conservative default; overridden per venue


def _now_ms() -> int:
    return int(time.time() * 1000)


class LedgerService:
    """
    All mutations to currency balances go through here.

    Typical call sequences
    ──────────────────────
    Deposit:
        service.deposit("INR", amount)

    INR → USDT conversion:
        fx = fx_service.get_rate("INR", "USDT")
        service.convert(inr_amount, fx)

    Open trade (margin reserve):
        service.reserve_margin("USDT", margin_amount, reference_id=order_id)

    Close trade:
        service.release_margin("USDT", margin_amount, reference_id=position_id)
        service.book_realized_pnl("USDT", pnl, reference_id=position_id)

    Fee / Funding:
        service.charge_fee("USDT", fee_amount, reference_id=order_id)
        service.book_funding("USDT", net_amount, reference_id=...)
    """

    def __init__(self, db: LedgerDB, user_id: str = "default"):
        self.db = db
        self.user_id = user_id

    # ── Account management ────────────────────────────────────

    def ensure_account(self, currency: str) -> int:
        """Get or create a currency account; return its id."""
        with self.db.transaction() as cur:
            if self.db._backend == "postgres":
                cur.execute(
                    """INSERT INTO currency_accounts (user_id, currency, updated_at)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (user_id, currency) DO NOTHING
                       RETURNING id""",
                    (self.user_id, currency, _now_ms()),
                )
                row = cur.fetchone()
                if row:
                    return row[0]
                cur.execute(
                    "SELECT id FROM currency_accounts WHERE user_id=%s AND currency=%s",
                    (self.user_id, currency),
                )
            else:
                cur.execute(
                    """INSERT OR IGNORE INTO currency_accounts
                       (user_id, currency, updated_at) VALUES (?, ?, ?)""",
                    (self.user_id, currency, _now_ms()),
                )
                cur.execute(
                    "SELECT id FROM currency_accounts WHERE user_id=? AND currency=?",
                    (self.user_id, currency),
                )
            row = cur.fetchone()
            return row[0]

    def get_account(self, currency: str) -> CurrencyAccount:
        account_id = self.ensure_account(currency)
        ph = "%s" if self.db._backend == "postgres" else "?"
        with self.db.cursor() as cur:
            cur.execute(
                f"SELECT id, currency, ledger_balance, available_balance, locked_balance "
                f"FROM currency_accounts WHERE id={ph}",
                (account_id,),
            )
            row = cur.fetchone()
        return CurrencyAccount(
            id=row[0],
            currency=row[1],
            ledger_balance=_D(str(row[2])),
            available_balance=_D(str(row[3])),
            locked_balance=_D(str(row[4])),
            user_id=self.user_id,
        )

    # ── Core double-entry writer ──────────────────────────────

    def _post_entry(
        self,
        cur,
        account_id: int,
        currency: str,
        direction: str,
        amount: Decimal,
        event_type: str,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
        rate_snapshot_id: Optional[int] = None,
        metadata: Optional[Dict] = None,
    ) -> Decimal:
        """
        Append a single ledger entry and update the account balance.
        Returns the new running_balance.
        Must be called inside an open transaction/cursor.
        """
        ph = "%s" if self.db._backend == "postgres" else "?"

        # Lock the account row (Postgres only — gives per-account serialisation)
        if self.db._backend == "postgres":
            cur.execute(
                f"SELECT ledger_balance FROM currency_accounts WHERE id={ph} FOR UPDATE",
                (account_id,),
            )
        else:
            cur.execute(
                f"SELECT ledger_balance FROM currency_accounts WHERE id={ph}",
                (account_id,),
            )
        row = cur.fetchone()
        current = _D(str(row[0]))

        sign = _D("1") if direction == "credit" else _D("-1")
        new_balance = current + sign * amount

        if new_balance < _ZERO and event_type not in (
            ET.REALIZED_PNL_DEBIT, ET.FEE_DEBIT, ET.FUNDING_DEBIT, ET.RECON_ADJUST
        ):
            raise ValueError(
                f"Ledger balance would go negative: {currency} "
                f"current={current} delta={sign * amount}"
            )

        meta_json = self.db._jsonb(metadata or {})
        cur.execute(
            self.db._adapt(
                """INSERT INTO ledger_entries
                   (account_id, currency, direction, amount, running_balance,
                    event_type, reference_type, reference_id, rate_snapshot_id,
                    metadata, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""
            ),
            (
                account_id, currency, direction,
                str(amount), str(new_balance),
                event_type, reference_type, reference_id, rate_snapshot_id,
                meta_json, _now_ms(),
            ),
        )
        cur.execute(
            self.db._adapt(
                "UPDATE currency_accounts SET ledger_balance=%s, updated_at=%s WHERE id=%s"
            ),
            (str(new_balance), _now_ms(), account_id),
        )
        return new_balance

    # ── Public economic events ────────────────────────────────

    def deposit(self, currency: str, amount: Decimal, reference_id: Optional[str] = None) -> None:
        amount = _D(str(amount))
        if amount <= _ZERO:
            raise ValueError(f"Deposit amount must be positive, got {amount}")
        account_id = self.ensure_account(currency)
        with self.db.transaction() as cur:
            new_bal = self._post_entry(
                cur, account_id, currency, "credit", amount,
                ET.DEPOSIT, ET.REF_MANUAL, reference_id,
            )
            cur.execute(
                self.db._adapt(
                    "UPDATE currency_accounts SET available_balance=%s WHERE id=%s"
                ),
                (str(new_bal), account_id),
            )
        logger.info("[LEDGER] Deposit %s %s → balance %s", amount, currency, new_bal)

    def withdraw(self, currency: str, amount: Decimal, reference_id: Optional[str] = None) -> None:
        amount = _D(str(amount))
        acct = self.get_account(currency)
        if amount > acct.available_balance:
            raise ValueError(
                f"Insufficient available {currency}: have {acct.available_balance}, need {amount}"
            )
        with self.db.transaction() as cur:
            new_bal = self._post_entry(
                cur, acct.id, currency, "debit", amount,
                ET.WITHDRAWAL, ET.REF_MANUAL, reference_id,
            )
            cur.execute(
                self.db._adapt(
                    "UPDATE currency_accounts SET available_balance=%s WHERE id=%s"
                ),
                (str(new_bal), acct.id),
            )
        logger.info("[LEDGER] Withdrawal %s %s → balance %s", amount, currency, new_bal)

    def convert(
        self,
        inr_amount: Decimal,
        fx: FxRate,
        reference_id: Optional[str] = None,
    ) -> Decimal:
        """
        INR → USDT conversion.
        Uses ask rate (conservative — we pay more INR per USDT when buying).
        Returns the USDT amount credited.
        """
        inr_amount = _D(str(inr_amount))
        rate = fx.rate_for_buy()                    # ask: we buy USDT
        usdt_received = (inr_amount / rate).quantize(_D("0.00000001"), rounding=ROUND_DOWN)

        inr_acct = self.get_account("INR")
        usdt_acct_id = self.ensure_account("USDT")

        if inr_amount > inr_acct.available_balance:
            raise ValueError(
                f"Insufficient INR: have {inr_acct.available_balance}, need {inr_amount}"
            )

        # Persist FX rate snapshot first
        fx_snap_id = self._save_fx_rate(fx)

        with self.db.transaction() as cur:
            # Debit INR
            self._post_entry(
                cur, inr_acct.id, "INR", "debit", inr_amount,
                ET.CONVERSION_DEBIT, ET.REF_CONVERSION, reference_id,
                rate_snapshot_id=fx_snap_id,
                metadata={"rate": str(rate), "usdt_received": str(usdt_received)},
            )
            # Credit USDT
            new_usdt = self._post_entry(
                cur, usdt_acct_id, "USDT", "credit", usdt_received,
                ET.CONVERSION_CREDIT, ET.REF_CONVERSION, reference_id,
                rate_snapshot_id=fx_snap_id,
                metadata={"rate": str(rate), "inr_spent": str(inr_amount)},
            )
            # Update available balances
            cur.execute(
                self.db._adapt(
                    "UPDATE currency_accounts SET available_balance=ledger_balance-locked_balance WHERE id=%s"
                ),
                (inr_acct.id,),
            )
            cur.execute(
                self.db._adapt(
                    "UPDATE currency_accounts SET available_balance=%s WHERE id=%s"
                ),
                (str(new_usdt), usdt_acct_id),
            )

        logger.info(
            "[LEDGER] Converted INR %s → USDT %s @ %s", inr_amount, usdt_received, rate
        )
        return usdt_received

    def reserve_margin(
        self,
        currency: str,
        amount: Decimal,
        reference_id: Optional[str] = None,
    ) -> None:
        """Lock margin for an open order/position."""
        amount = _D(str(amount))
        acct = self.get_account(currency)
        if amount > acct.available_balance:
            raise ValueError(
                f"Insufficient {currency} to reserve: available={acct.available_balance} need={amount}"
            )
        with self.db.transaction() as cur:
            self._post_entry(
                cur, acct.id, currency, "debit", amount,
                ET.MARGIN_RESERVE, ET.REF_ORDER, reference_id,
            )
            cur.execute(
                self.db._adapt(
                    """UPDATE currency_accounts
                       SET locked_balance = locked_balance + %s,
                           available_balance = available_balance - %s
                       WHERE id = %s"""
                ),
                (str(amount), str(amount), acct.id),
            )
        logger.debug("[LEDGER] Margin reserved %s %s ref=%s", amount, currency, reference_id)

    def release_margin(
        self,
        currency: str,
        amount: Decimal,
        reference_id: Optional[str] = None,
    ) -> None:
        """Release previously locked margin back to available."""
        amount = _D(str(amount))
        acct = self.get_account(currency)
        release = min(amount, acct.locked_balance)
        if release <= _ZERO:
            logger.warning("[LEDGER] release_margin called but locked_balance=0")
            return
        with self.db.transaction() as cur:
            self._post_entry(
                cur, acct.id, currency, "credit", release,
                ET.MARGIN_RELEASE, ET.REF_POSITION, reference_id,
            )
            cur.execute(
                self.db._adapt(
                    """UPDATE currency_accounts
                       SET locked_balance = locked_balance - %s,
                           available_balance = available_balance + %s
                       WHERE id = %s"""
                ),
                (str(release), str(release), acct.id),
            )
        logger.debug("[LEDGER] Margin released %s %s ref=%s", release, currency, reference_id)

    def book_realized_pnl(
        self,
        currency: str,
        pnl: Decimal,
        reference_id: Optional[str] = None,
    ) -> None:
        """
        Book realized PnL after position close.
        pnl can be positive (profit) or negative (loss).
        """
        pnl = _D(str(pnl))
        account_id = self.ensure_account(currency)
        if pnl == _ZERO:
            return
        direction = "credit" if pnl > _ZERO else "debit"
        event_type = ET.REALIZED_PNL_CREDIT if pnl > _ZERO else ET.REALIZED_PNL_DEBIT
        with self.db.transaction() as cur:
            new_bal = self._post_entry(
                cur, account_id, currency, direction, abs(pnl),
                event_type, ET.REF_POSITION, reference_id,
                metadata={"pnl": str(pnl)},
            )
            # PnL goes to available balance (margin was already released separately)
            sign = _D("1") if pnl > _ZERO else _D("-1")
            cur.execute(
                self.db._adapt(
                    "UPDATE currency_accounts SET available_balance=available_balance+%s WHERE id=%s"
                ),
                (str(sign * abs(pnl)), account_id),
            )
        logger.info("[LEDGER] Realized PnL %s %s ref=%s", pnl, currency, reference_id)

    def charge_fee(
        self,
        currency: str,
        fee: Decimal,
        reference_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> None:
        fee = _D(str(fee))
        if fee <= _ZERO:
            return
        account_id = self.ensure_account(currency)
        with self.db.transaction() as cur:
            self._post_entry(
                cur, account_id, currency, "debit", fee,
                ET.FEE_DEBIT, ET.REF_ORDER, reference_id, metadata=metadata or {},
            )
            cur.execute(
                self.db._adapt(
                    "UPDATE currency_accounts SET available_balance=available_balance-%s WHERE id=%s"
                ),
                (str(fee), account_id),
            )
        logger.debug("[LEDGER] Fee charged %s %s ref=%s", fee, currency, reference_id)

    def book_funding(
        self,
        currency: str,
        net_amount: Decimal,
        reference_id: Optional[str] = None,
    ) -> None:
        """net_amount > 0 = received, < 0 = paid."""
        net_amount = _D(str(net_amount))
        if net_amount == _ZERO:
            return
        account_id = self.ensure_account(currency)
        direction = "credit" if net_amount > _ZERO else "debit"
        event_type = ET.FUNDING_CREDIT if net_amount > _ZERO else ET.FUNDING_DEBIT
        with self.db.transaction() as cur:
            self._post_entry(
                cur, account_id, currency, direction, abs(net_amount),
                event_type, ET.REF_FUNDING, reference_id,
            )
            sign = _D("1") if net_amount > _ZERO else _D("-1")
            cur.execute(
                self.db._adapt(
                    "UPDATE currency_accounts SET available_balance=available_balance+%s WHERE id=%s"
                ),
                (str(sign * abs(net_amount)), account_id),
            )

    def recon_adjust(
        self,
        currency: str,
        delta: Decimal,
        reason: str,
    ) -> None:
        """Emergency reconciliation adjustment — logs and corrects drift."""
        delta = _D(str(delta))
        if delta == _ZERO:
            return
        account_id = self.ensure_account(currency)
        direction = "credit" if delta > _ZERO else "debit"
        with self.db.transaction() as cur:
            self._post_entry(
                cur, account_id, currency, direction, abs(delta),
                ET.RECON_ADJUST, ET.REF_RECON, None,
                metadata={"reason": reason, "delta": str(delta)},
            )
            sign = _D("1") if delta > _ZERO else _D("-1")
            cur.execute(
                self.db._adapt(
                    """UPDATE currency_accounts
                       SET available_balance=available_balance+%s,
                           ledger_balance=ledger_balance+%s
                       WHERE id=%s"""
                ),
                (str(sign * abs(delta)), str(sign * abs(delta)), account_id),
            )
        logger.warning("[LEDGER] RECON ADJUST %s %s: %s", delta, currency, reason)

    # ── Query helpers ─────────────────────────────────────────

    def get_balance(self, currency: str) -> Decimal:
        acct = self.get_account(currency)
        return acct.ledger_balance

    def get_available(self, currency: str) -> Decimal:
        acct = self.get_account(currency)
        return acct.available_balance

    def get_history(
        self,
        currency: str,
        limit: int = 100,
        event_type: Optional[str] = None,
    ) -> List[Dict]:
        ph = "%s" if self.db._backend == "postgres" else "?"
        acct = self.get_account(currency)
        if event_type:
            sql = (
                f"SELECT * FROM ledger_entries WHERE account_id={ph} AND event_type={ph} "
                f"ORDER BY created_at DESC LIMIT {ph}"
            )
            params = (acct.id, event_type, limit)
        else:
            sql = (
                f"SELECT * FROM ledger_entries WHERE account_id={ph} "
                f"ORDER BY created_at DESC LIMIT {ph}"
            )
            params = (acct.id, limit)

        with self.db.cursor() as cur:
            cur.execute(self.db._adapt(sql), params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def verify_balance_integrity(self, currency: str) -> bool:
        """Recompute balance from entries and compare to stored value."""
        ph = "%s" if self.db._backend == "postgres" else "?"
        acct = self.get_account(currency)
        with self.db.cursor() as cur:
            cur.execute(
                self.db._adapt(
                    f"SELECT direction, amount FROM ledger_entries WHERE account_id={ph}"
                ),
                (acct.id,),
            )
            computed = _ZERO
            for direction, amount in cur.fetchall():
                amt = _D(str(amount))
                computed += amt if direction == "credit" else -amt
        match = computed == acct.ledger_balance
        if not match:
            logger.error(
                "[LEDGER] Integrity violation for %s: stored=%s computed=%s",
                currency, acct.ledger_balance, computed,
            )
        return match

    # ── FX rate persistence ───────────────────────────────────

    def _save_fx_rate(self, fx: FxRate) -> int:
        with self.db.transaction() as cur:
            cur.execute(
                self.db._adapt(
                    """INSERT INTO fx_rates (from_currency, to_currency, bid, ask, mid, source, captured_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)"""
                ),
                (
                    fx.from_currency, fx.to_currency,
                    str(fx.bid), str(fx.ask), str(fx.mid),
                    fx.source, fx.captured_at,
                ),
            )
            if self.db._backend == "postgres":
                cur.execute("SELECT lastval()")
            else:
                cur.execute("SELECT last_insert_rowid()")
            return cur.fetchone()[0]

    def save_wallet_snapshot(self, snap: WalletSnapshot) -> None:
        with self.db.transaction() as cur:
            cur.execute(
                self.db._adapt(
                    """INSERT INTO wallet_snapshots_v2
                       (venue, account_id, settlement_currency,
                        wallet_balance, margin_balance, available_balance,
                        locked_balance, unrealized_pnl, realized_pnl,
                        funding_accrued, fees_accrued, snapshot_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""
                ),
                (
                    snap.venue, snap.account_id, snap.settlement_currency,
                    str(snap.wallet_balance), str(snap.margin_balance),
                    str(snap.available_balance), str(snap.locked_balance),
                    str(snap.unrealized_pnl), str(snap.realized_pnl),
                    str(snap.funding_accrued), str(snap.fees_accrued),
                    snap.snapshot_at,
                ),
            )
