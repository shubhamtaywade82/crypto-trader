"""
crypto_trader.execution.adapters.sandbox — SandboxExecutor

Sandbox mode: real exchange API, but pointed at the testnet.
Real API calls, fake money.  Behaviour is identical to LiveExecutor
except the base URL resolves to the sandbox/testnet endpoint.

CoinDCX sandbox: https://public.coindcx.com (testnet)
The COINDCX_SANDBOX_URL env var overrides the default.

Use sandbox to validate the full execution path (order state machine,
reconciliation, user stream) before going live — without risking capital.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Dict, List, Optional

from ...wallet import Order, OrderType, PositionSide
from ...exchanges.adapter_protocol import NormalisedBalance
from ...exchanges.coindcx_execution import CoinDCXExecutionEngine
from ...exchanges.coindcx_client import CoinDCXClient
from ...exchanges.instrument_mapper import InstrumentMapper

logger = logging.getLogger("crypto_trader.execution.adapters.sandbox")

# Default CoinDCX testnet URL.  Override via COINDCX_SANDBOX_URL env var.
_DEFAULT_SANDBOX_URL = "https://public.coindcx.com"


class SandboxExecutor:
    """
    Routes all order operations to the exchange testnet.

    Identical execution path to LiveExecutor — same CoinDCXExecutionEngine,
    same order state machine, same reconciliation — but the base URL points
    to the sandbox so no real money is at risk.

    The safe_mode gate is NOT enforced for sandbox (no LIVE_TRADING_ENABLED
    required) because no capital is at stake.
    """

    mode_label = "sandbox"

    def __init__(self, engine: CoinDCXExecutionEngine):
        self._engine = engine
        self.circuit_breaker = getattr(engine, "circuit_breaker", None)
        self.mapper = getattr(engine, "mapper", None)
        self.client = getattr(engine, "client", None)

    @classmethod
    def from_config(cls, cfg) -> "SandboxExecutor":
        """Build a SandboxExecutor from a TradingConfig."""
        import os
        sandbox_url = (
            os.environ.get("COINDCX_SANDBOX_URL")
            or getattr(cfg, "coindcx_sandbox_url", None)
            or _DEFAULT_SANDBOX_URL
        )
        client = CoinDCXClient(
            api_key=cfg.coindcx_api_key,
            api_secret=cfg.coindcx_api_secret,
            base_url=sandbox_url,
        )
        mapper = InstrumentMapper(client, margin_currency=cfg.coindcx_margin_currency)
        engine = CoinDCXExecutionEngine(
            client=client,
            mapper=mapper,
            leverage=cfg.max_leverage,
            margin_currency=cfg.coindcx_margin_currency,
            # Sandbox is not real money — bypass the live-money safety gate.
            i_understand_real_money=False,
        )
        return cls(engine)

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
            "[SANDBOX] place_order %s %s qty=%s type=%s",
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
        raw = self._engine.get_balances()
        if isinstance(raw, list) and raw and hasattr(raw[0], "wallet_balance"):
            return raw
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

    def sync_balance(self) -> Decimal:
        return self._engine.sync_balance()

    def get_usdt_conversion(self) -> Optional[Decimal]:
        return self._engine.get_usdt_conversion()
