"""
crypto_trader.execution.adapters.live — LiveExecutor

Live mode: all orders go to the real exchange (CoinDCX).
The exchange is the source of truth for balances and positions.
The local database is a projection (cached read model), not the truth.

All real-money operations are gated by safe_mode checks in
CoinDCXExecutionEngine — this adapter does not bypass them.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Dict, List, Optional

from ...wallet import Order, OrderType, PositionSide
from ...exchanges.adapter_protocol import NormalisedBalance, NormalisedPosition
from ...exchanges.coindcx_execution import CoinDCXExecutionEngine

logger = logging.getLogger("crypto_trader.execution.adapters.live")


class LiveExecutor:
    """
    Routes all order operations to the real CoinDCX exchange.

    Balances and positions are fetched from the exchange — the local DB
    is only a projection for dashboard / analytics.
    """

    mode_label = "live"

    def __init__(self, engine: CoinDCXExecutionEngine):
        self._engine = engine
        # Expose circuit breaker and mapper so engine_live can inspect them.
        self.circuit_breaker = getattr(engine, "circuit_breaker", None)
        self.mapper = getattr(engine, "mapper", None)
        self.client = getattr(engine, "client", None)

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
        logger.info(
            "[LIVE] place_order %s %s qty=%s type=%s",
            side.value, symbol, quantity, order_type.value,
        )
        return self._engine.place_order(
            symbol, side, quantity, order_type,
            trigger_price=trigger_price,
            limit_price=limit_price,
            reduce_only=reduce_only,
            expires_at=expires_at,
            client_order_id=client_order_id,
            leverage=leverage,
        )

    def cancel_order(self, order_id: str) -> bool:
        return self._engine.cancel_order(order_id)

    def sync_positions(self) -> Dict[str, dict]:
        return self._engine.sync_positions()

    def get_balances(self) -> List[NormalisedBalance]:
        """Fetch live balances from the exchange."""
        raw = self._engine.get_balances()
        if isinstance(raw, list) and raw and hasattr(raw[0], "wallet_balance"):
            return raw  # already NormalisedBalance
        # Fallback: wrap raw dicts if the engine returns them
        out: List[NormalisedBalance] = []
        for item in (raw if isinstance(raw, list) else [raw]):
            if isinstance(item, dict):
                out.append(NormalisedBalance(
                    asset=item.get("asset", "USDT"),
                    wallet_balance=Decimal(str(item.get("wallet_balance", 0))),
                    available_balance=Decimal(str(item.get("available_balance", 0))),
                    locked_balance=Decimal(str(item.get("locked_balance", 0))),
                    unrealized_pnl=Decimal(str(item.get("unrealized_pnl", 0))),
                ))
        return out

    # ── Passthrough helpers used by engine_live ───────────────────────────

    def sync_balance(self) -> Decimal:
        return self._engine.sync_balance()

    def get_usdt_conversion(self) -> Optional[Decimal]:
        return self._engine.get_usdt_conversion()
