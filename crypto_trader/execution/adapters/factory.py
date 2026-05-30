"""
crypto_trader.execution.adapters.factory — build_executor()

Single factory function that maps TradingMode → concrete Executor.

Everything above this layer codes against ExecutorProtocol, not against
Paper/Sandbox/Live directly.  This is the only place in the codebase that
has mode-specific branching for execution.

    PAPER   → PaperExecutor   (local DB only)
    SANDBOX → SandboxExecutor (exchange testnet)
    LIVE    → LiveExecutor    (real exchange, real money)
"""
from __future__ import annotations

import logging
from typing import Union

from ...config import TradingConfig, TradingMode
from ...wallet import EnhancedFuturesWallet
from .paper import PaperExecutor
from .sandbox import SandboxExecutor
from .live import LiveExecutor

logger = logging.getLogger("crypto_trader.execution.adapters.factory")

Executor = Union[PaperExecutor, SandboxExecutor, LiveExecutor]


def build_executor(cfg: TradingConfig, wallet: EnhancedFuturesWallet) -> Executor:
    """
    Return the correct executor for the configured trading mode.

    Paper:   No exchange interaction.  Fills are simulated locally.
    Sandbox: Real API calls to testnet.  No real money.
    Live:    Real API calls to production.  Real money.  Requires safe_mode gate.
    """
    mode = cfg.mode

    if mode == TradingMode.PAPER:
        logger.info("[EXECUTOR] mode=paper — simulated fills, local DB is source of truth")
        return PaperExecutor(wallet)

    if mode == TradingMode.SANDBOX:
        logger.info("[EXECUTOR] mode=sandbox — real API, testnet, fake money")
        return SandboxExecutor.from_config(cfg)

    if mode == TradingMode.LIVE:
        logger.info("[EXECUTOR] mode=live — real API, real money")
        return _build_live_executor(cfg)

    raise ValueError(f"Unknown TradingMode: {mode!r}")


def _build_live_executor(cfg: TradingConfig) -> LiveExecutor:
    from ...exchanges.coindcx_execution import CoinDCXExecutionEngine
    from ...exchanges.coindcx_client import CoinDCXClient
    from ...exchanges.instrument_mapper import InstrumentMapper

    client = CoinDCXClient(
        api_key=cfg.coindcx_api_key,
        api_secret=cfg.coindcx_api_secret,
        base_url=cfg.coindcx_base_url,
    )
    mapper = InstrumentMapper(client, margin_currency=cfg.coindcx_margin_currency)
    engine = CoinDCXExecutionEngine(
        client=client,
        mapper=mapper,
        leverage=cfg.max_leverage,
        margin_currency=cfg.coindcx_margin_currency,
        i_understand_real_money=True,
    )
    return LiveExecutor(engine)
