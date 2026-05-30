"""
crypto_trader.ledger.balance_adapter — BalanceAdapter

Abstracts the source of truth for wallet balance by trading mode.

    PAPER   → LocalBalanceAdapter  — reads from Postgres ledger / local wallet
    SANDBOX → ExchangeBalanceAdapter — reads from exchange testnet
    LIVE    → ExchangeBalanceAdapter — reads from real exchange

The wallet engine calls get_snapshot() to build the WalletSnapshot that
the rest of the system (risk engine, portfolio engine, UI) consumes.
Both adapters return the same WalletSnapshot shape — callers never need
to know which mode is active.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

logger = logging.getLogger("crypto_trader.ledger.balance_adapter")


@dataclass
class WalletSnapshot:
    """
    Canonical wallet state used by all trading modes.

    Paper:   derived from local ledger arithmetic.
    Sandbox/Live: derived from exchange-reported values.
    """
    wallet_balance: Decimal
    margin_balance: Decimal
    available_balance: Decimal
    locked_balance: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal

    @property
    def equity(self) -> Decimal:
        return self.wallet_balance + self.unrealized_pnl


@runtime_checkable
class BalanceAdapter(Protocol):
    """Source-of-truth abstraction for account balance."""

    def get_snapshot(self) -> WalletSnapshot:
        """Return the current wallet snapshot."""
        ...

    @property
    def source_label(self) -> str:
        """'local' for paper, 'exchange' for sandbox/live."""
        ...


class LocalBalanceAdapter:
    """
    Paper mode: Postgres ledger is the source of truth.

    All values are derived from local event-sourced wallet state.
    The exchange is never consulted.
    """

    source_label = "local"

    def __init__(self, wallet):
        self._wallet = wallet

    def get_snapshot(self) -> WalletSnapshot:
        w = self._wallet
        wb = Decimal(str(getattr(w, "wallet_balance", 0)))
        upnl = Decimal(str(getattr(w, "unrealized_pnl_total", 0)))
        rpnl = Decimal(str(getattr(w, "realized_pnl_total", 0)))
        # available_balance and margin_balance may be properties on the wallet
        avail = Decimal(str(getattr(w, "available_balance", 0)))
        mb = Decimal(str(getattr(w, "margin_balance", wb + upnl)))
        locked = mb - avail if mb > avail else Decimal("0")
        return WalletSnapshot(
            wallet_balance=wb,
            margin_balance=mb,
            available_balance=avail,
            locked_balance=locked,
            unrealized_pnl=upnl,
            realized_pnl=rpnl,
        )


class ExchangeBalanceAdapter:
    """
    Sandbox / Live mode: exchange is the source of truth.

    The local DB is kept as a projection (derived read model) for
    analytics and the dashboard, but balance decisions always use
    the exchange-reported values fetched here.
    """

    source_label = "exchange"

    def __init__(self, executor, asset: str = "USDT"):
        self._executor = executor
        self._asset = asset

    def get_snapshot(self) -> WalletSnapshot:
        balances = self._executor.get_balances()
        target = next(
            (b for b in balances if b.asset.upper() == self._asset.upper()),
            None,
        )
        if target is None:
            logger.warning(
                "[ExchangeBalanceAdapter] %s balance not found in exchange response — "
                "returning zero snapshot", self._asset,
            )
            zero = Decimal("0")
            return WalletSnapshot(
                wallet_balance=zero, margin_balance=zero,
                available_balance=zero, locked_balance=zero,
                unrealized_pnl=zero, realized_pnl=zero,
            )

        wb = target.wallet_balance
        upnl = target.unrealized_pnl
        locked = target.locked_balance
        avail = target.available_balance
        return WalletSnapshot(
            wallet_balance=wb,
            margin_balance=wb + upnl,
            available_balance=avail,
            locked_balance=locked,
            unrealized_pnl=upnl,
            realized_pnl=Decimal("0"),  # exchange doesn't surface session realized PnL directly
        )


def build_balance_adapter(mode_label: str, wallet=None, executor=None, asset: str = "USDT") -> BalanceAdapter:
    """
    Factory: return the correct BalanceAdapter for the active mode.

    Args:
        mode_label: 'paper', 'sandbox', or 'live'
        wallet:     EnhancedFuturesWallet (required for paper)
        executor:   ExecutorProtocol implementation (required for sandbox/live)
        asset:      Margin currency to track (default 'USDT')
    """
    if mode_label == "paper":
        if wallet is None:
            raise ValueError("LocalBalanceAdapter requires wallet")
        return LocalBalanceAdapter(wallet)
    if mode_label in ("sandbox", "live"):
        if executor is None:
            raise ValueError("ExchangeBalanceAdapter requires executor")
        return ExchangeBalanceAdapter(executor, asset=asset)
    raise ValueError(f"Unknown mode_label: {mode_label!r}")
