"""
crypto_trader.wallet — Futures Position & Account Manager
=========================================================
Tracks positions, PnL, margin, and persists state to disk.
Supports partial closes, trailing stops, and time stops.
"""
from __future__ import annotations

from ._wallet import (
    DATA_DIR,
    PositionSide,
    Playbook,
    OrderStatus,
    OrderStateMachine,
    OrderType,
    Order,
    Fill,
    PortfolioState,
    PortfolioReducer,
    EnhancedPosition,
    Severity,
    InvariantRule,
    EnhancedFuturesWallet,
    ExecutionEngine,
    PaperExecutionEngine,
)

__all__ = [
    "DATA_DIR",
    "PositionSide",
    "Playbook",
    "OrderStatus",
    "OrderStateMachine",
    "OrderType",
    "Order",
    "Fill",
    "PortfolioState",
    "PortfolioReducer",
    "EnhancedPosition",
    "Severity",
    "InvariantRule",
    "EnhancedFuturesWallet",
    "ExecutionEngine",
    "PaperExecutionEngine",
]
