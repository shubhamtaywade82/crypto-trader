"""
PositionEngine — per-symbol position state and margin calculations.

Responsibility
--------------
* Open, partially-fill, reduce, and close positions in the database.
* Recompute all mark-price-dependent fields (notional, margin, PnL,
  liquidation price) on every mark update.
* Persist position state to ``positions_v2`` and emit a ``Position``
  dataclass on every mutation so the caller can push it to the hot layer.
* Calculate liquidation price using the simplified isolated-margin formula:

    LONG liq  = entry * (1 - 1/leverage + maintenance_margin_rate)
                  where maintenance_margin_rate ≈ 0.5% (0.005)
    SHORT liq = entry * (1 + 1/leverage - maintenance_margin_rate)

All arithmetic is Decimal; no float leaks into position state.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Dict, List, Optional
import uuid

from .db import LedgerDB
from .models import Position

logger = logging.getLogger("crypto_trader.ledger.position_engine")

_D = Decimal
_ZERO = _D("0")

# Typical isolated-margin maintenance margin rate (exchange-specific; Binance FAPI default)
_MAINTENANCE_MARGIN_RATE = _D("0.005")


def _now_ms() -> int:
    return int(time.time() * 1000)


def compute_liquidation_price(
    side: str,
    entry: _D,
    leverage: int,
    maintenance_margin_rate: _D = _MAINTENANCE_MARGIN_RATE,
) -> _D:
    """
    Simplified isolated-margin liquidation price formula.

    LONG:  liq = entry × (1 − 1/leverage + mmr)
    SHORT: liq = entry × (1 + 1/leverage − mmr)
    """
    lev = _D(leverage)
    if side == "LONG":
        return entry * (_D("1") - _D("1") / lev + maintenance_margin_rate)
    else:
        return entry * (_D("1") + _D("1") / lev - maintenance_margin_rate)


class PositionEngine:
    """
    Manages open positions for a single user across venues.

    Parameters
    ----------
    db:
        Shared LedgerDB.
    user_id:
        Owner of positions.
    maintenance_margin_rate:
        Exchange maintenance margin rate used for liquidation calc.
    """

    def __init__(
        self,
        db: LedgerDB,
        user_id: str = "default",
        maintenance_margin_rate: _D = _MAINTENANCE_MARGIN_RATE,
    ) -> None:
        self._db = db
        self._user_id = user_id
        self._mmr = maintenance_margin_rate

    # ── Open / Increase ───────────────────────────────────────────────────

    def open_position(
        self,
        venue: str,
        symbol: str,
        side: str,
        qty: _D,
        entry_price: _D,
        leverage: int = 1,
        playbook: Optional[str] = None,
        strategy: Optional[str] = None,
        tp_price: Optional[_D] = None,
        sl_price: Optional[_D] = None,
    ) -> Position:
        """
        Persist a new open position and return the Position object.

        Raises ValueError if an open position for (venue, symbol, side)
        already exists (callers should use ``add_to_position`` instead).
        """
        existing = self.get_open_position(venue, symbol)
        if existing is not None:
            raise ValueError(
                f"Open position already exists for {venue}/{symbol} "
                f"(uid={existing.position_uid}). Use add_to_position()."
            )

        pos = self._build_position(
            venue=venue,
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=entry_price,
            leverage=leverage,
            playbook=playbook,
            strategy=strategy,
            tp_price=tp_price,
            sl_price=sl_price,
        )
        self._insert_position(pos)
        logger.info("[PositionEngine] Opened %s %s %s @ %s ×%d uid=%s",
                    venue, symbol, side, entry_price, leverage, pos.position_uid)
        return pos

    def add_to_position(
        self,
        position_uid: str,
        qty_delta: _D,
        fill_price: _D,
    ) -> Position:
        """
        Increase an existing open position (average-up / pyramid).

        Recalculates entry_price_avg as VWAP and persists the update.
        """
        pos = self._load_by_uid(position_uid)
        if pos is None:
            raise ValueError(f"Position {position_uid} not found")
        if pos.status != "open":
            raise ValueError(f"Cannot add to a {pos.status} position")

        total_qty   = pos.qty + qty_delta
        total_cost  = pos.qty * pos.entry_price_avg + qty_delta * fill_price
        new_avg     = total_cost / total_qty

        pos.qty             = total_qty
        pos.entry_price_avg = new_avg
        pos.updated_at      = _now_ms()
        self._recalculate(pos, mark=fill_price)
        self._update_position(pos)
        return pos

    # ── Reduce / Close ────────────────────────────────────────────────────

    def reduce_position(
        self,
        position_uid: str,
        qty_delta: _D,
        exit_price: _D,
    ) -> Position:
        """
        Reduce position size by ``qty_delta``.

        If qty falls to zero, the position is automatically closed.
        Returns the updated Position.
        """
        pos = self._load_by_uid(position_uid)
        if pos is None:
            raise ValueError(f"Position {position_uid} not found")
        if pos.status != "open":
            raise ValueError(f"Cannot reduce a {pos.status} position")
        if qty_delta > pos.qty:
            raise ValueError(f"qty_delta {qty_delta} > position qty {pos.qty}")

        # Realised PnL for the reduced portion
        if pos.side == "LONG":
            pnl_per_unit = exit_price - pos.entry_price_avg
        else:
            pnl_per_unit = pos.entry_price_avg - exit_price
        portion_pnl = pnl_per_unit * qty_delta

        pos.qty          -= qty_delta
        pos.realized_pnl += portion_pnl
        pos.updated_at    = _now_ms()

        if pos.qty <= _ZERO:
            pos.qty      = _ZERO
            pos.status   = "closed"
            pos.closed_at = _now_ms()
            pos.unrealized_pnl = _ZERO
        else:
            self._recalculate(pos, mark=exit_price)

        self._update_position(pos)
        return pos

    def close_position(
        self,
        position_uid: str,
        exit_price: _D,
        reason: str = "manual",
    ) -> Position:
        """Close the entire position at exit_price."""
        pos = self._load_by_uid(position_uid)
        if pos is None:
            raise ValueError(f"Position {position_uid} not found")
        return self.reduce_position(position_uid, pos.qty, exit_price)

    # ── Mark price refresh ────────────────────────────────────────────────

    def refresh_marks(
        self,
        venue: str,
        marks: Dict[str, _D],
    ) -> List[Position]:
        """
        Update all open positions for ``venue`` with the latest mark prices.

        ``marks`` is a dict of {symbol: mark_price}.
        Returns list of updated Position objects (not persisted — callers
        should push these to Redis for the hot layer).
        """
        positions = self.list_open_positions(venue)
        updated = []
        for pos in positions:
            mark = marks.get(pos.symbol)
            if mark is None:
                continue
            self._recalculate(pos, mark)
            updated.append(pos)
        return updated

    # ── Queries ───────────────────────────────────────────────────────────

    def get_open_position(self, venue: str, symbol: str) -> Optional[Position]:
        """Return the open position for (venue, symbol), or None."""
        sql = self._db._adapt(
            "SELECT * FROM positions_v2 "
            "WHERE user_id = %s AND venue = %s AND symbol = %s AND status = %s "
            "ORDER BY opened_at DESC LIMIT 1"
        )
        with self._db.cursor() as cur:
            cur.execute(sql, (self._user_id, venue, symbol, "open"))
            row = cur.fetchone()
        return self._row_to_position(row) if row else None

    def list_open_positions(self, venue: Optional[str] = None) -> List[Position]:
        """Return all open positions, optionally filtered by venue."""
        if venue:
            sql = self._db._adapt(
                "SELECT * FROM positions_v2 "
                "WHERE user_id = %s AND venue = %s AND status = %s "
                "ORDER BY opened_at ASC"
            )
            params = (self._user_id, venue, "open")
        else:
            sql = self._db._adapt(
                "SELECT * FROM positions_v2 "
                "WHERE user_id = %s AND status = %s "
                "ORDER BY opened_at ASC"
            )
            params = (self._user_id, "open")

        with self._db.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [self._row_to_position(r) for r in rows if r]

    def get_by_uid(self, position_uid: str) -> Optional[Position]:
        return self._load_by_uid(position_uid)

    def update_sl_tp(
        self,
        position_uid: str,
        sl_price: Optional[_D] = None,
        tp_price: Optional[_D] = None,
    ) -> Position:
        pos = self._load_by_uid(position_uid)
        if pos is None:
            raise ValueError(f"Position {position_uid} not found")
        if sl_price is not None:
            pos.sl_price = sl_price
        if tp_price is not None:
            pos.tp_price = tp_price
        pos.updated_at = _now_ms()
        self._update_position(pos)
        return pos

    def accrue_funding(self, position_uid: str, amount: _D) -> Position:
        """Add (positive) or subtract (negative) funding to the position record."""
        pos = self._load_by_uid(position_uid)
        if pos is None:
            raise ValueError(f"Position {position_uid} not found")
        pos.funding_accrued += amount
        pos.updated_at = _now_ms()
        self._update_position(pos)
        return pos

    def accrue_fee(self, position_uid: str, fee: _D) -> Position:
        pos = self._load_by_uid(position_uid)
        if pos is None:
            raise ValueError(f"Position {position_uid} not found")
        pos.fees_accrued += fee
        pos.updated_at = _now_ms()
        self._update_position(pos)
        return pos

    # ── Private helpers ───────────────────────────────────────────────────

    def _build_position(
        self,
        venue: str,
        symbol: str,
        side: str,
        qty: _D,
        entry_price: _D,
        leverage: int,
        playbook: Optional[str],
        strategy: Optional[str],
        tp_price: Optional[_D],
        sl_price: Optional[_D],
    ) -> Position:
        notional  = qty * entry_price
        init_margin = notional / _D(leverage)
        liq_price = compute_liquidation_price(side, entry_price, leverage, self._mmr)
        pos = Position(
            venue=venue,
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price_avg=entry_price,
            leverage=leverage,
            mark_price=entry_price,
            notional=notional,
            initial_margin=init_margin,
            maintenance_margin=notional * self._mmr,
            margin_used=init_margin,
            liquidation_price=liq_price,
            unrealized_pnl=_ZERO,
            realized_pnl=_ZERO,
            tp_price=tp_price,
            sl_price=sl_price,
            playbook=playbook,
            strategy=strategy,
            user_id=self._user_id,
        )
        return pos

    def _recalculate(self, pos: Position, mark: _D) -> None:
        """In-place refresh using mark price (same as model.Position.refresh)."""
        pos.refresh(mark)
        pos.liquidation_price = compute_liquidation_price(
            pos.side, pos.entry_price_avg, pos.leverage, self._mmr
        )
        pos.maintenance_margin = pos.notional * self._mmr if pos.notional else None
        pos.margin_used = pos.initial_margin

    def _insert_position(self, pos: Position) -> None:
        sql = self._db._adapt(
            "INSERT INTO positions_v2 "
            "(position_uid, user_id, venue, symbol, side, qty, entry_price_avg, "
            " mark_price, notional, leverage, initial_margin, maintenance_margin, "
            " margin_used, liquidation_price, unrealized_pnl, realized_pnl, "
            " unrealized_pnl_pct, funding_accrued, fees_accrued, "
            " tp_price, sl_price, status, playbook, strategy, opened_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        )
        with self._db.cursor() as cur:
            cur.execute(sql, (
                pos.position_uid, pos.user_id, pos.venue, pos.symbol, pos.side,
                str(pos.qty), str(pos.entry_price_avg),
                str(pos.mark_price) if pos.mark_price else None,
                str(pos.notional) if pos.notional else None,
                pos.leverage,
                str(pos.initial_margin) if pos.initial_margin else None,
                str(pos.maintenance_margin) if pos.maintenance_margin else None,
                str(pos.margin_used) if pos.margin_used else None,
                str(pos.liquidation_price) if pos.liquidation_price else None,
                str(pos.unrealized_pnl), str(pos.realized_pnl),
                str(pos.unrealized_pnl_pct),
                str(pos.funding_accrued), str(pos.fees_accrued),
                str(pos.tp_price) if pos.tp_price else None,
                str(pos.sl_price) if pos.sl_price else None,
                pos.status, pos.playbook, pos.strategy,
                pos.opened_at, pos.updated_at,
            ))
            # Retrieve the auto-generated id
            if self._db._backend == "sqlite":
                pos.id = cur.lastrowid
            else:
                cur.execute("SELECT lastval()")
                pos.id = cur.fetchone()[0]

    def _update_position(self, pos: Position) -> None:
        sql = self._db._adapt(
            "UPDATE positions_v2 SET "
            "qty=%s, entry_price_avg=%s, mark_price=%s, notional=%s, "
            "initial_margin=%s, maintenance_margin=%s, margin_used=%s, "
            "liquidation_price=%s, unrealized_pnl=%s, realized_pnl=%s, "
            "unrealized_pnl_pct=%s, funding_accrued=%s, fees_accrued=%s, "
            "tp_price=%s, sl_price=%s, status=%s, closed_at=%s, updated_at=%s "
            "WHERE position_uid=%s"
        )
        with self._db.cursor() as cur:
            cur.execute(sql, (
                str(pos.qty), str(pos.entry_price_avg),
                str(pos.mark_price) if pos.mark_price else None,
                str(pos.notional) if pos.notional else None,
                str(pos.initial_margin) if pos.initial_margin else None,
                str(pos.maintenance_margin) if pos.maintenance_margin else None,
                str(pos.margin_used) if pos.margin_used else None,
                str(pos.liquidation_price) if pos.liquidation_price else None,
                str(pos.unrealized_pnl), str(pos.realized_pnl),
                str(pos.unrealized_pnl_pct),
                str(pos.funding_accrued), str(pos.fees_accrued),
                str(pos.tp_price) if pos.tp_price else None,
                str(pos.sl_price) if pos.sl_price else None,
                pos.status, pos.closed_at, pos.updated_at,
                pos.position_uid,
            ))

    def _load_by_uid(self, uid: str) -> Optional[Position]:
        sql = self._db._adapt(
            "SELECT * FROM positions_v2 WHERE position_uid = %s"
        )
        with self._db.cursor() as cur:
            cur.execute(sql, (uid,))
            row = cur.fetchone()
        return self._row_to_position(row) if row else None

    @staticmethod
    def _row_to_position(row) -> Optional[Position]:
        if row is None:
            return None
        # Column order must match the SELECT * order from positions_v2
        # id, position_uid, user_id, venue, symbol, side, qty, entry_price_avg,
        # mark_price, notional, leverage, initial_margin, maintenance_margin,
        # margin_used, liquidation_price, unrealized_pnl, realized_pnl,
        # unrealized_pnl_pct, funding_accrued, fees_accrued,
        # tp_price, sl_price, status, playbook, strategy,
        # opened_at, closed_at, updated_at
        def _d(v) -> Optional[_D]:
            return _D(str(v)) if v is not None else None

        return Position(
            id=row[0],
            position_uid=str(row[1]),
            user_id=str(row[2]),
            venue=str(row[3]),
            symbol=str(row[4]),
            side=str(row[5]),
            qty=_D(str(row[6])),
            entry_price_avg=_D(str(row[7])),
            mark_price=_d(row[8]),
            notional=_d(row[9]),
            leverage=int(row[10]),
            initial_margin=_d(row[11]),
            maintenance_margin=_d(row[12]),
            margin_used=_d(row[13]),
            liquidation_price=_d(row[14]),
            unrealized_pnl=_D(str(row[15])),
            realized_pnl=_D(str(row[16])),
            unrealized_pnl_pct=_D(str(row[17])),
            funding_accrued=_D(str(row[18])),
            fees_accrued=_D(str(row[19])),
            tp_price=_d(row[20]),
            sl_price=_d(row[21]),
            status=str(row[22]),
            playbook=str(row[23]) if row[23] else None,
            strategy=str(row[24]) if row[24] else None,
            opened_at=int(row[25]),
            closed_at=int(row[26]) if row[26] else None,
            updated_at=int(row[27]),
        )
