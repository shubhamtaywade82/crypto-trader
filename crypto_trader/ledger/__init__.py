"""
crypto_trader.ledger — Double-entry wallet & position ledger.

Three-layer architecture
------------------------
Layer 1 (source of truth):  PostgreSQL via ``LedgerDB``
Layer 2 (hot projection):   Redis via ``RedisLayer``
Layer 3 (per-process cache): In-memory (caller's responsibility)

Quick-start (SQLite dev mode, no Redis needed):

    from crypto_trader.ledger import LedgerDB, LedgerService

    db = LedgerDB()          # SQLite dev mode
    db.bootstrap()           # Create tables

    svc = LedgerService(db)
    svc.deposit("USDT", Decimal("10000"))
"""

from .db import LedgerDB
from .event_types import (
    DEPOSIT,
    WITHDRAWAL,
    CONVERSION_DEBIT,
    CONVERSION_CREDIT,
    MARGIN_RESERVE,
    MARGIN_RELEASE,
    FEE_DEBIT,
    FUNDING_DEBIT,
    FUNDING_CREDIT,
    REALIZED_PNL_CREDIT,
    REALIZED_PNL_DEBIT,
    RECON_ADJUST,
    OPENING_BALANCE,
)
from .fx_service import FXService, StaleRateError
from .models import (
    CurrencyAccount,
    Fill,
    FxRate,
    LedgerEntry,
    Order,
    Position,
    ReconciliationResult,
    WalletSnapshot,
)
from .position_engine import PositionEngine, compute_liquidation_price
from .reconciliation import ReconciliationWorker
from .redis_layer import RedisLayer
from .service import LedgerService
from .wallet_projection import WalletProjectionService

__all__ = [
    # DB
    "LedgerDB",
    # Services
    "LedgerService",
    "FXService",
    "WalletProjectionService",
    "PositionEngine",
    "ReconciliationWorker",
    "RedisLayer",
    # Models
    "CurrencyAccount",
    "LedgerEntry",
    "FxRate",
    "WalletSnapshot",
    "Position",
    "Order",
    "Fill",
    "ReconciliationResult",
    # Event types
    "DEPOSIT",
    "WITHDRAWAL",
    "CONVERSION_DEBIT",
    "CONVERSION_CREDIT",
    "MARGIN_RESERVE",
    "MARGIN_RELEASE",
    "FEE_DEBIT",
    "FUNDING_DEBIT",
    "FUNDING_CREDIT",
    "REALIZED_PNL_CREDIT",
    "REALIZED_PNL_DEBIT",
    "RECON_ADJUST",
    "OPENING_BALANCE",
    # Helpers
    "StaleRateError",
    "compute_liquidation_price",
]
