"""
crypto_trader.execution.account_sync — Exchange account snapshot
=================================================================
Fetches and normalizes the venue's authoritative account state (balances,
positions, open orders, recent fills) into plain dicts the reconciler compares
against the wallet's event-sourced state. CoinDCX is the source of truth.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("crypto_trader.execution.account_sync")


@dataclass
class AccountSnapshot:
    balances: Dict[str, float] = field(default_factory=dict)
    positions: Dict[str, dict] = field(default_factory=dict)   # symbol -> {qty, side, entry_price, ...}
    open_orders: List[dict] = field(default_factory=list)
    fills: List[dict] = field(default_factory=list)
    ok: bool = True
    error: Optional[str] = None


class AccountSync:
    def __init__(self, execution_engine):
        self.engine = execution_engine

    def snapshot(self, symbol: Optional[str] = None) -> AccountSnapshot:
        try:
            balances = self.engine.get_balances()
            positions = {p["symbol"]: p for p in self.engine.get_positions()}
            open_orders = self.engine.get_open_orders(symbol)
            fills = self.engine.get_fills(symbol)
            return AccountSnapshot(
                balances=balances,
                positions=positions,
                open_orders=open_orders,
                fills=fills,
            )
        except Exception as e:
            logger.error("Account snapshot failed: %s", e)
            return AccountSnapshot(ok=False, error=str(e))
