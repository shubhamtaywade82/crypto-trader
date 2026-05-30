"""
crypto_trader.ledger.position_adapter — PositionAdapter

Abstracts the source of truth for open positions by trading mode.

    PAPER   → LocalPositionAdapter   — reads from Postgres positions_v2 table
    SANDBOX → ExchangePositionAdapter — reads from exchange testnet
    LIVE    → ExchangePositionAdapter — reads from real exchange

Both adapters return List[NormalisedPosition] — callers are mode-agnostic.

Live PnL note (per architecture spec):
  - Paper: PnL calculated locally from entry_price vs mark_price.
  - Sandbox/Live: PnL comes from exchange-reported values; do not
    override with local calculations to avoid discrepancy.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import List, Protocol, runtime_checkable

from ..exchanges.adapter_protocol import NormalisedPosition

logger = logging.getLogger("crypto_trader.ledger.position_adapter")


@runtime_checkable
class PositionAdapter(Protocol):
    """Source-of-truth abstraction for open positions."""

    def get_positions(self) -> List[NormalisedPosition]:
        """Return all currently open positions."""
        ...

    @property
    def source_label(self) -> str:
        """'local' for paper, 'exchange' for sandbox/live."""
        ...


class LocalPositionAdapter:
    """
    Paper mode: local wallet / Postgres is the source of truth.

    Positions are derived from the event-sourced wallet state.
    PnL is calculated locally as (mark_price - entry_price) × qty.
    """

    source_label = "local"

    def __init__(self, wallet):
        self._wallet = wallet

    def get_positions(self) -> List[NormalisedPosition]:
        positions = []
        with self._wallet.lock:
            for sym, pos in self._wallet.positions.items():
                if pos.status != "OPEN":
                    continue
                qty = Decimal(str(pos.remaining_quantity))
                entry = Decimal(str(pos.entry_price or 0))
                mark = Decimal(str(pos.mark_price if hasattr(pos, "mark_price") and pos.mark_price else entry))
                notional = qty * mark
                lev = int(getattr(pos, "leverage", 1) or 1)

                if pos.side.value == "LONG":
                    upnl = (mark - entry) * qty
                else:
                    upnl = (entry - mark) * qty

                positions.append(NormalisedPosition(
                    symbol=sym,
                    side=pos.side.value,
                    qty=qty,
                    entry_price=entry,
                    mark_price=mark,
                    notional=notional,
                    leverage=lev,
                    unrealized_pnl=upnl,
                ))
        return positions


class ExchangePositionAdapter:
    """
    Sandbox / Live mode: exchange is the source of truth for positions.

    Use exchange-reported mark_price and unrealized_pnl — do not
    override with local calculations to prevent discrepancies between
    what the exchange shows and what the system acts on.
    """

    source_label = "exchange"

    def __init__(self, executor):
        self._executor = executor

    def get_positions(self) -> List[NormalisedPosition]:
        raw = self._executor.sync_positions()
        if not raw:
            return []

        # sync_positions returns Dict[str, dict]; convert to NormalisedPosition
        positions = []
        for sym, pos in raw.items():
            if isinstance(pos, NormalisedPosition):
                positions.append(pos)
                continue
            if isinstance(pos, dict):
                qty = Decimal(str(pos.get("quantity", 0)))
                entry = Decimal(str(pos.get("entry_price", 0)))
                mark = Decimal(str(pos.get("mark_price", entry)))
                upnl = Decimal(str(pos.get("unrealized_pnl", 0)))
                notional = Decimal(str(pos.get("notional", qty * mark)))
                lev = int(pos.get("leverage", 1) or 1)
                side = pos.get("side", "LONG")
                positions.append(NormalisedPosition(
                    symbol=sym,
                    side=side,
                    qty=qty,
                    entry_price=entry,
                    mark_price=mark,
                    notional=notional,
                    leverage=lev,
                    unrealized_pnl=upnl,
                ))
        return positions


def build_position_adapter(mode_label: str, wallet=None, executor=None) -> PositionAdapter:
    """
    Factory: return the correct PositionAdapter for the active mode.

    Args:
        mode_label: 'paper', 'sandbox', or 'live'
        wallet:     EnhancedFuturesWallet (required for paper)
        executor:   ExecutorProtocol implementation (required for sandbox/live)
    """
    if mode_label == "paper":
        if wallet is None:
            raise ValueError("LocalPositionAdapter requires wallet")
        return LocalPositionAdapter(wallet)
    if mode_label in ("sandbox", "live"):
        if executor is None:
            raise ValueError("ExchangePositionAdapter requires executor")
        return ExchangePositionAdapter(executor)
    raise ValueError(f"Unknown mode_label: {mode_label!r}")
