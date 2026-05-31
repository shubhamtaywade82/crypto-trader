"""
crypto_trader.position_management
==================================
AI-driven position management system — 3-layer architecture:

  Layer 1 (State)
    PositionStateEngine     — canonical PositionSnapshot per open position
    MarketContextEngine     — live market regime, ATR, VWAP, structure
    PositionSyncService     — discovers ALL exchange positions (manual + bot)

  Layer 2 (Decision)
    ActionScorer            — deterministic 0-100 health score
    AIAssessor              — LLM-backed recommendation (advisory only)
    StopLossCalculator      — deterministic SL computation
    TakeProfitCalculator    — deterministic TP computation

  Layer 3 (Execution)
    PolicyGuard             — hard invariant enforcement
    PositionExecutor        — applies approved actions to wallet + exchange
    ProtectionManager       — places SL/TP for unprotected positions
    AuditLogger             — full audit trail (JSONL + Postgres)
    ReassessmentLoop        — background evaluation orchestrator

Quick start::

    from crypto_trader.position_management import build_manager

    manager = build_manager(wallet, data_feed=feed, llm_router=router)
    manager.start()
"""

from .schemas import (
    PositionActionType,
    PositionSnapshot,
    MarketContext,
    WalletContext,
    PortfolioContext,
    PositionConstraints,
    PositionManagementInput,
    PositionManagementDecision,
    PolicyDecision,
    AuditRecord,
)
from .state_engine import PositionStateEngine
from .market_context_engine import MarketContextEngine
from .action_scorer import score_position, score_to_candidate_actions
from .ai_assessor import AIAssessor
from .policy_guard import PolicyGuard
from .executor import PositionExecutor
from .audit_logger import AuditLogger
from .reassessment_loop import ReassessmentLoop
from .sync_service import PositionSyncService, ExternalPosition, PositionSource, PositionSyncState
from .sl_tp_calculator import StopLossCalculator, TakeProfitCalculator
from .protection_manager import ProtectionManager


def build_manager(
    wallet,
    data_feed=None,
    ws_feed=None,
    llm_router=None,
    exchange_execution=None,
    rest_client=None,
    ws_user_stream=None,
    database_url: str = "",
    max_portfolio_risk: float = 4_000.0,
    dry_run: bool = False,
    tick_interval_s: float = 1.0,
    scheduled_interval_s: float = 60.0,
) -> "PositionManager":
    """
    Factory: wire all components and return a ready-to-start PositionManager.
    """
    return PositionManager(
        wallet=wallet,
        data_feed=data_feed,
        ws_feed=ws_feed,
        llm_router=llm_router,
        exchange_execution=exchange_execution,
        rest_client=rest_client,
        ws_user_stream=ws_user_stream,
        database_url=database_url,
        max_portfolio_risk=max_portfolio_risk,
        dry_run=dry_run,
        tick_interval_s=tick_interval_s,
        scheduled_interval_s=scheduled_interval_s,
    )


class PositionManager:
    """
    Top-level façade: wires all components and exposes a clean API.

    Usage::

        manager = build_manager(wallet, data_feed=feed)
        manager.start()
        manager.on_tick("BTCUSDT", 106000.0)
        manager.on_candle_close("BTCUSDT")
        manager.stop()
    """

    def __init__(
        self,
        wallet,
        data_feed=None,
        ws_feed=None,
        llm_router=None,
        exchange_execution=None,
        rest_client=None,
        ws_user_stream=None,
        database_url: str = "",
        max_portfolio_risk: float = 4_000.0,
        dry_run: bool = False,
        tick_interval_s: float = 1.0,
        scheduled_interval_s: float = 60.0,
    ):
        self.state_engine = PositionStateEngine(
            wallet=wallet,
            max_portfolio_risk=max_portfolio_risk,
        )
        self.mce = MarketContextEngine(
            data_feed=data_feed,
            ws_feed=ws_feed,
        )
        self.assessor = AIAssessor(llm_router=llm_router)
        self.guard = PolicyGuard()
        self.executor = PositionExecutor(
            wallet=wallet,
            exchange_execution=exchange_execution,
            dry_run=dry_run,
        )
        self.audit = AuditLogger(database_url=database_url)
        self.loop = ReassessmentLoop(
            state_engine=self.state_engine,
            market_ctx_engine=self.mce,
            ai_assessor=self.assessor,
            policy_guard=self.guard,
            executor=self.executor,
            audit_logger=self.audit,
            tick_interval_s=tick_interval_s,
            scheduled_interval_s=scheduled_interval_s,
        )
        self.sync_service = PositionSyncService(
            rest_client=rest_client,
            ws_client=ws_user_stream,
            wallet=wallet,
            on_new_position=self._on_new_external_position,
            on_protection_required=self._on_protection_required,
        )
        self.protection_manager = ProtectionManager(
            execution_client=exchange_execution,
            data_feed=data_feed,
            dry_run=dry_run,
        )

    def start(self):
        self.sync_service.start()
        self.loop.start()

    def stop(self):
        self.loop.stop()
        self.sync_service.stop()

    def on_tick(self, symbol: str, ltp: float):
        self.sync_service.on_ws_ltp_update(symbol, ltp)
        self.loop.on_tick(symbol, ltp)

    def on_candle_close(self, symbol: str):
        self.loop.on_candle_close(symbol)

    def on_fill(self, symbol: str):
        self.loop.on_fill(symbol)

    def on_ws_position_event(self, data: dict):
        self.sync_service.on_ws_position_update(data)

    def force_reassess(self, symbol: str):
        self.loop.force_reassess(symbol, trigger="manual")

    def _on_new_external_position(self, pos: ExternalPosition):
        import logging
        logging.getLogger("crypto_trader.position_management").info(
            "🔍 External position detected: %s %s %.4f @ %.4f (source=%s)",
            pos.symbol, pos.side, pos.qty, pos.entry_price, pos.source,
        )

    def _on_protection_required(self, pos: ExternalPosition):
        import logging
        logging.getLogger("crypto_trader.position_management").warning(
            "⚠️  Position %s %s has no SL/TP — calculating protection",
            pos.symbol, pos.side,
        )
        result = self.protection_manager.assess_and_protect(pos)
        if result.get("error"):
            logging.getLogger("crypto_trader.position_management").error(
                "Failed to calculate protection for %s: %s", pos.symbol, result["error"]
            )
        else:
            self.sync_service.mark_protected(pos.exchange_id)


__all__ = [
    "PositionManager",
    "build_manager",
    "PositionActionType",
    "PositionSnapshot",
    "MarketContext",
    "WalletContext",
    "PortfolioContext",
    "PositionConstraints",
    "PositionManagementInput",
    "PositionManagementDecision",
    "PolicyDecision",
    "AuditRecord",
    "PositionStateEngine",
    "MarketContextEngine",
    "score_position",
    "score_to_candidate_actions",
    "AIAssessor",
    "PolicyGuard",
    "PositionExecutor",
    "AuditLogger",
    "ReassessmentLoop",
    "PositionSyncService",
    "ExternalPosition",
    "PositionSource",
    "PositionSyncState",
    "StopLossCalculator",
    "TakeProfitCalculator",
    "ProtectionManager",
]
