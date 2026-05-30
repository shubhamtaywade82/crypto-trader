"""
crypto_trader.execution.adapters.paper — PaperExecutor

Paper mode: everything happens inside the local database.
No exchange orders are ever sent.

Fill simulation
---------------
Market orders are filled immediately at mark_price ± spread model.
The spread model applies a configurable per-side coefficient
(paper_cdcx_spread_coeff) to approximate thin CoinDCX liquidity.
A collar rejects fills that deviate beyond paper_collar_pct from mark.

Source of truth: Postgres ledger (or SQLite in dev).
Balance / position reads come from the local wallet, never the exchange.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Dict, List, Optional

from ...wallet import EnhancedFuturesWallet, Order, OrderType, PositionSide
from ...exchanges.adapter_protocol import NormalisedBalance
from ...exchanges.instrument_mapper import InstrumentSpec

logger = logging.getLogger("crypto_trader.execution.adapters.paper")


class PaperExecutor:
    """
    Simulates exchange behaviour entirely within the local database.

    This is the only executor used in PAPER mode.  It wraps
    EnhancedFuturesWallet and satisfies ExecutorProtocol.
    """

    mode_label = "paper"

    def __init__(self, wallet: EnhancedFuturesWallet):
        self.wallet = wallet
        self.mapper = _MockMapper()

    # ── ExecutorProtocol ─────────────────────────────────────────────────

    def place_order(
        self,
        symbol: str,
        side: PositionSide,
        quantity: Decimal,
        order_type: OrderType,
        *,
        trigger_price: Optional[Decimal] = None,
        limit_price: Optional[Decimal] = None,
        reduce_only: bool = False,
        expires_at: Optional[int] = None,
        client_order_id: Optional[str] = None,
        leverage: Optional[float] = None,
    ) -> Order:
        logger.debug(
            "[PAPER] place_order %s %s qty=%s type=%s",
            side.value, symbol, quantity, order_type.value,
        )
        return self.wallet.place_pending_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            trigger_price=trigger_price,
            limit_price=limit_price,
            reduce_only=reduce_only,
            expires_at=expires_at,
        )

    def cancel_order(self, order_id: str) -> bool:
        return self.wallet.cancel_order(order_id)

    def sync_positions(self) -> Dict[str, dict]:
        with self.wallet.lock:
            return {
                s: {
                    "symbol": p.symbol,
                    "side": p.side.value,
                    "quantity": p.remaining_quantity,
                    "entry_price": p.entry_price,
                }
                for s, p in self.wallet.positions.items()
                if p.status == "OPEN"
            }

    def get_balances(self) -> List[NormalisedBalance]:
        w = self.wallet
        wb = Decimal(str(getattr(w, "wallet_balance", 0)))
        avail = Decimal(str(getattr(w, "available_balance", 0)))
        upnl = Decimal(str(getattr(w, "unrealized_pnl_total", 0)))
        mb = Decimal(str(getattr(w, "margin_balance", wb + upnl)))
        locked = mb - avail if mb > avail else Decimal("0")
        return [
            NormalisedBalance(
                asset="USDT",
                wallet_balance=wb,
                available_balance=avail,
                locked_balance=locked,
                unrealized_pnl=upnl,
            )
        ]


class _MockMapper:
    """Minimal instrument spec stub used by paper mode (no exchange call)."""

    def get_spec(self, symbol: str) -> InstrumentSpec:
        return InstrumentSpec(
            internal_symbol=symbol,
            pair=symbol,
            price_increment=Decimal("0.0001"),
            quantity_increment=Decimal("0.001"),
            min_quantity=Decimal("0.001"),
            min_notional=Decimal("6.0"),
            max_leverage=20,
            maker_fee_rate=0.0002,
            taker_fee_rate=0.0005,
            status="active",
        )
