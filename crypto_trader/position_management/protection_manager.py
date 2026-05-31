"""
crypto_trader.position_management.protection_manager
=====================================================
Detects positions that are missing SL or TP and creates protective orders.

This is triggered immediately when a new position is discovered (including
manual positions opened outside the bot).  The AI is NOT asked to supply
price levels — the StopLossCalculator and TakeProfitCalculator do that.

Flow:
  ExternalPosition (no SL/TP)
    → assess_protection_needs()
    → StopLossCalculator.calculate(style)
    → TakeProfitCalculator.calculate(style)
    → place_protection_orders()
    → sync_service.mark_protected()
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from .sl_tp_calculator import StopLossCalculator, TakeProfitCalculator
from .sync_service import ExternalPosition

logger = logging.getLogger("crypto_trader.position_management.protection_manager")


class ProtectionManager:
    """
    Computes and places SL/TP orders for unprotected positions.

    Uses deterministic calculators — the LLM may inform the *style choice*
    but never outputs raw price levels.
    """

    def __init__(
        self,
        execution_client=None,     # live execution (CoinDCX)
        data_feed=None,            # for fetching candle data
        default_sl_style: str = "swing_low",
        default_tp_style: str = "multi_level",
        default_rr_ratios: Optional[list] = None,
        default_atr_multiplier: float = 1.5,
        timeframe: str = "1h",
        candle_limit: int = 200,
        dry_run: bool = False,
    ):
        self.execution = execution_client
        self.data_feed = data_feed
        self.sl_style = default_sl_style
        self.tp_style = default_tp_style
        self.rr_ratios = default_rr_ratios or [2.0, 3.5]
        self.atr_mult = default_atr_multiplier
        self.timeframe = timeframe
        self.candle_limit = candle_limit
        self.dry_run = dry_run

    def assess_and_protect(self, pos: ExternalPosition) -> dict:
        """
        Calculate missing SL/TP and optionally place orders.
        Returns a dict with 'sl', 'tp_levels', 'actions_taken'.
        """
        result = {"sl": None, "tp_levels": [], "actions_taken": [], "error": None}

        df = self._fetch_candles(pos.symbol)
        if df is None or len(df) < 20:
            result["error"] = "insufficient_candle_data"
            logger.warning("[ProtectionMgr] Skipping %s — no candle data", pos.symbol)
            return result

        # Calculate SL if missing
        if not pos.has_stop_loss:
            sl_calc = StopLossCalculator(
                df=df,
                symbol=pos.symbol,
                side=pos.side,
                entry_price=pos.entry_price,
                atr_multiplier=self.atr_mult,
            )
            sl_result = sl_calc.calculate(style=self.sl_style)
            pos.stop_loss = sl_result.price
            result["sl"] = sl_result.price
            logger.info(
                "[ProtectionMgr] Calculated SL for %s %s: %.4f (%s) — %s",
                pos.symbol, pos.side, sl_result.price,
                sl_result.style, sl_result.invalidation_note,
            )
            result["actions_taken"].append(f"sl_calculated:{sl_result.style}")

        # Calculate TP if missing
        if not pos.has_take_profit and pos.stop_loss:
            tp_calc = TakeProfitCalculator(
                df=df,
                symbol=pos.symbol,
                side=pos.side,
                entry_price=pos.entry_price,
                stop_loss=pos.stop_loss,
            )
            tp_result = tp_calc.calculate(style=self.tp_style, rr_ratios=self.rr_ratios)
            if tp_result.levels:
                pos.take_profit = tp_result.levels[0]
                result["tp_levels"] = tp_result.levels
                logger.info(
                    "[ProtectionMgr] Calculated TP for %s %s: %s (%s)",
                    pos.symbol, pos.side,
                    [f"{t:.4f}" for t in tp_result.levels], tp_result.style,
                )
                result["actions_taken"].append(f"tp_calculated:{tp_result.style}")

        # Place orders if execution is available and not dry-run
        if not self.dry_run and self.execution is not None:
            order_result = self._place_protection_orders(pos, result)
            result["actions_taken"].extend(order_result)

        return result

    def _place_protection_orders(self, pos: ExternalPosition, calc_result: dict) -> list:
        """Place SL and TP orders on the exchange."""
        actions = []
        side_for_close = "SELL" if pos.side == "LONG" else "BUY"

        # Place SL as stop-market order
        if calc_result.get("sl") and pos.stop_loss:
            try:
                if hasattr(self.execution, "place_stop_loss_order"):
                    order = self.execution.place_stop_loss_order(
                        symbol=pos.symbol,
                        side=side_for_close,
                        quantity=pos.qty,
                        stop_price=pos.stop_loss,
                        reduce_only=True,
                    )
                    logger.info(
                        "[ProtectionMgr] SL order placed for %s: %.4f", pos.symbol, pos.stop_loss
                    )
                    actions.append("sl_order_placed")
                elif hasattr(self.execution, "place_stop_market"):
                    self.execution.place_stop_market(
                        symbol=pos.symbol,
                        side=side_for_close,
                        quantity=pos.qty,
                        stop_price=pos.stop_loss,
                        reduce_only=True,
                    )
                    actions.append("sl_order_placed")
            except Exception as exc:
                logger.error("[ProtectionMgr] Failed to place SL for %s: %s", pos.symbol, exc)
                actions.append(f"sl_order_error:{exc}")

        # Place TP (first level) as take-profit order
        tp_levels = calc_result.get("tp_levels", [])
        if tp_levels and pos.take_profit:
            try:
                first_tp = tp_levels[0]
                tp_qty = pos.qty if len(tp_levels) == 1 else pos.qty * 0.5
                if hasattr(self.execution, "place_take_profit_order"):
                    self.execution.place_take_profit_order(
                        symbol=pos.symbol,
                        side=side_for_close,
                        quantity=tp_qty,
                        price=first_tp,
                        reduce_only=True,
                    )
                    actions.append("tp_order_placed")
                elif hasattr(self.execution, "place_limit_order"):
                    self.execution.place_limit_order(
                        symbol=pos.symbol,
                        side=side_for_close,
                        quantity=tp_qty,
                        price=first_tp,
                        reduce_only=True,
                    )
                    actions.append("tp_order_placed")
            except Exception as exc:
                logger.error("[ProtectionMgr] Failed to place TP for %s: %s", pos.symbol, exc)
                actions.append(f"tp_order_error:{exc}")

        return actions

    def _fetch_candles(self, symbol: str) -> Optional[pd.DataFrame]:
        if self.data_feed is None:
            return None
        try:
            if hasattr(self.data_feed, "get_klines"):
                return self.data_feed.get_klines(
                    symbol, interval=self.timeframe, limit=self.candle_limit
                )
        except Exception as exc:
            logger.warning("[ProtectionMgr] Failed to fetch candles for %s: %s", symbol, exc)
        return None
