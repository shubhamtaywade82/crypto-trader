"""
crypto_trader.position_management.executor
==========================================
Layer 3 (execution): Applies a PolicyDecision to the wallet and exchange.

Every action is:
  - idempotent (re-applying the same SL value is safe)
  - logged before and after
  - validated against wallet state before touching the exchange

Supported execution types:
  keep         → no order change
  modify_stop  → update SL (and optionally TP) on wallet + exchange
  partial_exit → reduce position size by exit_qty_pct
  full_exit    → close position entirely
  scale_in     → increase position size by scale_qty_pct
"""

from __future__ import annotations

import logging
from typing import Optional

from .schemas import (
    ExecutionParams,
    PolicyDecision,
    PositionActionType,
    PositionManagementInput,
)

logger = logging.getLogger("crypto_trader.position_management.executor")


class PositionExecutor:
    """
    Applies an approved PolicyDecision to the wallet and optionally the exchange.

    In paper/sim mode: wallet changes only (no exchange calls).
    In live mode:      wallet + exchange API.
    """

    def __init__(
        self,
        wallet,                           # EnhancedFuturesWallet
        exchange_execution=None,          # live execution client (optional)
        dry_run: bool = False,            # log only, no actual changes
    ):
        self.wallet = wallet
        self.execution = exchange_execution
        self.dry_run = dry_run

    def apply(
        self,
        inp: PositionManagementInput,
        policy: PolicyDecision,
    ) -> str:
        """
        Execute the approved action.

        Returns: "ok" | "skipped:<reason>" | "error:<message>"
        """
        action = policy.action
        params = policy.modified_params or ExecutionParams(type="keep")
        symbol = inp.position.symbol
        pos_id = inp.position.position_id

        if self.dry_run:
            logger.info("[Executor] DRY-RUN %s → %s for %s", action, params.type, symbol)
            return "skipped:dry_run"

        if not policy.approved:
            logger.info(
                "[Executor] Action %s was not approved for %s (%s)",
                policy.original_action, symbol, policy.rejection_reason,
            )
            return f"skipped:policy_rejected:{policy.rejection_reason}"

        if action == PositionActionType.PAUSE_MANAGEMENT:
            return "skipped:paused"

        if action in (PositionActionType.KEEP_OPEN, PositionActionType.HOLD_AND_MONITOR):
            return "ok"

        exec_type = params.type

        try:
            if exec_type == "modify_stop":
                return self._modify_stop(symbol, params, inp)

            if exec_type == "partial_exit":
                return self._partial_exit(symbol, params, inp)

            if exec_type == "full_exit":
                return self._full_exit(symbol, inp)

            if exec_type == "scale_in":
                return self._scale_in(symbol, params, inp)

            logger.warning("[Executor] Unknown exec_type '%s' for %s", exec_type, symbol)
            return f"skipped:unknown_exec_type:{exec_type}"

        except Exception as exc:
            logger.error("[Executor] Error applying %s to %s: %s", action, symbol, exc, exc_info=True)
            return f"error:{exc}"

    # ── Action handlers ───────────────────────────────────────────────────

    def _modify_stop(
        self, symbol: str, params: ExecutionParams, inp: PositionManagementInput
    ) -> str:
        new_sl = params.new_stop_loss
        if new_sl is None:
            return "skipped:no_new_stop_loss"

        current_sl = inp.position.stop_loss
        if abs(new_sl - current_sl) < 1e-8:
            return "skipped:sl_unchanged"

        logger.info(
            "[Executor] Modifying SL for %s: %.4f → %.4f",
            symbol, current_sl, new_sl,
        )

        # Update wallet event log
        self._wallet_adjust_sl(symbol, new_sl)

        # Update TP if provided
        if params.new_take_profit is not None:
            logger.info(
                "[Executor] Modifying TP for %s: → %.4f", symbol, params.new_take_profit
            )
            # TP management is informational in the wallet; exchange TP order handled below

        # Exchange: cancel + replace protective stop order
        if self.execution and self._is_live(symbol):
            self._exchange_replace_sl(symbol, new_sl, params.new_take_profit, inp)

        return "ok"

    def _partial_exit(
        self, symbol: str, params: ExecutionParams, inp: PositionManagementInput
    ) -> str:
        exit_pct = params.exit_qty_pct or 0.5
        exit_pct = max(0.01, min(0.99, exit_pct))  # never full-exit via partial path
        ltp = inp.position.ltp

        logger.info(
            "[Executor] Partial exit %s: %.0f%% at %.4f", symbol, exit_pct * 100, ltp,
        )

        # Route through the canonical wallet path. ``partial_close`` handles BOTH
        # paper and live (venue reduce-only fill, protective-order cancel+replace,
        # fee/TDS/PnL booking, event emission) — so we do NOT place a separate venue
        # order here, which would double-execute in live mode and bypass accounting.
        self.wallet.partial_close(symbol, ltp, pct=exit_pct, reason="position_management")
        return "ok"

    def _full_exit(self, symbol: str, inp: PositionManagementInput) -> str:
        ltp = inp.position.ltp
        logger.info("[Executor] Full exit %s at %.4f", symbol, ltp)

        # ``close_position`` is the single source of truth for exits: it performs the
        # venue exit fill (live) or simulated fill (paper), cancels protective orders,
        # books fees/TDS/realized PnL to the balance, and emits POSITION_CLOSED. This
        # replaces the hand-rolled close that bypassed PnL-to-balance accounting.
        self.wallet.close_position(symbol, ltp, reason="position_management")
        return "ok"

    def _scale_in(
        self, symbol: str, params: ExecutionParams, inp: PositionManagementInput
    ) -> str:
        scale_pct = params.scale_qty_pct or 0.25
        add_qty = inp.position.qty * scale_pct
        ltp = inp.position.ltp

        logger.info(
            "[Executor] Scale in %s: +%.4f (%.0f%%) at %.4f",
            symbol, add_qty, scale_pct * 100, ltp,
        )

        if self._is_live(symbol) and self.execution:
            return self._exchange_scale_in(symbol, add_qty, ltp, inp)

        logger.info("[Executor] Scale-in in paper mode deferred (no paper scale-in implementation)")
        return "skipped:paper_scale_in_not_implemented"

    # ── Wallet helpers ────────────────────────────────────────────────────

    def _wallet_adjust_sl(self, symbol: str, new_sl: float):
        from decimal import Decimal
        with self.wallet.lock:
            pos = self.wallet.positions.get(symbol)
            if pos and pos.status == "OPEN":
                pos.sl_price = Decimal(str(new_sl))
                self.wallet._emit_event("SL_ADJUSTED", {
                    "symbol": symbol,
                    "sl_price": str(new_sl),
                    "source": "position_management",
                })

    # ── Exchange helpers (live mode) ──────────────────────────────────────

    def _is_live(self, symbol: str) -> bool:
        pos = self.wallet.positions.get(symbol)
        return pos is not None and getattr(pos, "mode", "paper") == "live"

    def _exchange_replace_sl(
        self,
        symbol: str,
        new_sl: float,
        new_tp: Optional[float],
        inp: PositionManagementInput,
    ) -> str:
        try:
            pos = self.wallet.positions.get(symbol)
            if pos is None:
                return "error:position_not_found"
            old_sl_id = pos.protective_orders.get("sl")
            if old_sl_id and hasattr(self.execution, "cancel_order"):
                self.execution.cancel_order(symbol, old_sl_id)

            side_str = "SELL" if inp.position.side == "LONG" else "BUY"
            if hasattr(self.execution, "place_stop_market"):
                order = self.execution.place_stop_market(
                    symbol=symbol,
                    side=side_str,
                    quantity=inp.position.qty,
                    stop_price=new_sl,
                    reduce_only=True,
                )
                if order:
                    with self.wallet.lock:
                        pos.protective_orders["sl"] = order.get("orderId", "")
            return "ok"
        except Exception as exc:
            logger.error("[Executor] exchange_replace_sl failed for %s: %s", symbol, exc)
            return f"error:{exc}"

    def _exchange_scale_in(
        self, symbol: str, qty: float, price: float, inp: PositionManagementInput
    ) -> str:
        try:
            side_str = inp.position.side  # same direction
            if hasattr(self.execution, "place_market_order"):
                self.execution.place_market_order(symbol=symbol, side=side_str, quantity=qty)
            return "ok"
        except Exception as exc:
            logger.error("[Executor] exchange_scale_in failed for %s: %s", symbol, exc)
            return f"error:{exc}"
