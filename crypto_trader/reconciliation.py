"""
crypto_trader.reconciliation — Exchange State Consistency Layer
===============================================================
Implements PRD §18 and §11.5 (Exchange State Consistency Layer).
Continuously reconciles `desired_state` (local wallet) vs `actual_exchange_state`.
Handles phantom positions, missing stop-losses, and orphan orders.
"""

import logging
from typing import Optional
from decimal import Decimal

from .wallet import EnhancedFuturesWallet, PositionSide
from .risk import RiskManager

logger = logging.getLogger("crypto_trader.reconciliation")


class ExchangeStateReconciler:
    """
    Compares the internal deterministic wallet state against the live exchange state.
    Triggers kill switches on severe deviations or silently corrects minor orphans.
    """

    def __init__(self, execution_engine, wallet: EnhancedFuturesWallet, risk_manager: RiskManager):
        self.execution_engine = execution_engine
        self.wallet = wallet
        self.risk_manager = risk_manager

    def reconcile_symbol(self, symbol: str) -> bool:
        """
        Runs the state reconciliation algorithm for a specific symbol.
        Returns False if a critical state desynchronization was detected (triggering a halt).
        """
        if not self.execution_engine:
            return True
            
        try:
            # 0. Synchronize live balance if running in live mode
            if getattr(self.wallet, "live_execution", False) and self.execution_engine is not None:
                try:
                    live_bal = self.execution_engine.sync_balance()
                    if live_bal is not None:
                        mc = self.execution_engine.margin_currency.upper()
                        if mc != "USDT":
                            conv = float(self.execution_engine.get_usdt_conversion()) or 1.0
                            live_bal = live_bal / conv
                        old_bal = float(self.wallet.wallet_balance)
                        new_bal = float(live_bal)
                        if abs(old_bal - new_bal) > 0.01:
                            self.wallet.wallet_balance = self.wallet._to_decimal(live_bal)
                            logger.info(
                                "[RECONCILIATION] Synchronized wallet balance with exchange: "
                                "%.4f USDT -> %.4f USDT", old_bal, new_bal
                            )
                except Exception as e:
                    logger.error("[RECONCILIATION] Failed to sync wallet balance with exchange: %s", e)

            # 1. Fetch Actual State (Exchange)
            exchange_positions = self.execution_engine.get_positions()
            exchange_open_orders = self.execution_engine.get_open_orders(symbol)
            
            # Find the active position on the exchange for this symbol
            ex_pos = None
            for p in exchange_positions:
                if p.get("symbol") == symbol:
                    ex_pos = p
                    break

            # Sync leverage with local DB
            if getattr(self.wallet, "live_execution", False) and self.execution_engine:
                if ex_pos and "leverage" in ex_pos:
                    venue_lev = int(ex_pos["leverage"])
                    if not hasattr(self.wallet, "symbol_leverages"):
                        self.wallet.symbol_leverages = {}
                    self.wallet.symbol_leverages[symbol] = venue_lev
                    if symbol == self.wallet.symbol:
                        self.wallet.leverage = venue_lev
                    if hasattr(self.execution_engine, "leverage"):
                        self.execution_engine.leverage = venue_lev
                    logger.info("[RECONCILIATION] Synced leverage for %s from active position: %d", symbol, venue_lev)
                else:
                    if hasattr(self.wallet, "sync_leverage_from_venue"):
                        self.wallet.sync_leverage_from_venue(symbol)
                    
            # 2. Fetch Desired State (Local)
            local_pos = self.wallet.get_open_position(symbol)
            if local_pos and getattr(local_pos, "mode", "paper") != "live":
                local_pos = None
            
            # 3. Position Size Reconciliation
            ex_qty = Decimal(str(ex_pos.get("quantity", "0"))) if ex_pos else Decimal("0")
            local_qty = local_pos.quantity if local_pos else Decimal("0")
            
            # Allow for very tiny fractional differences due to exchange rounding
            if abs(ex_qty - local_qty) > Decimal("0.0001"):
                if ex_qty > 0 and local_qty == 0:
                    msg = f"Phantom position detected on exchange: {ex_qty} {symbol}. Local state is FLAT."
                    logger.critical(f"[RECONCILIATION] {msg}")
                    self.risk_manager.trigger_kill_switch(msg)
                    return False
                elif local_qty > 0 and ex_qty == 0:
                    msg = f"Missing position on exchange: expected {local_qty} {symbol}, but exchange is FLAT."
                    logger.critical(f"[RECONCILIATION] {msg}")
                    self.risk_manager.trigger_kill_switch(msg)
                    return False
                else:
                    msg = f"Position size desync: exchange={ex_qty} local={local_qty} {symbol}. Halting for safety."
                    logger.critical(f"[RECONCILIATION] {msg}")
                    self.risk_manager.trigger_kill_switch(msg)
                    return False
                    
            # 4. Orphaned Order Cleanup
            # If we are flat, we shouldn't have any stop/TP orders dangling on the exchange.
            if ex_qty == 0:
                for o in exchange_open_orders:
                    # Cancel any leftover reduce_only, STOP, or TAKE_PROFIT orders
                    order_type = str(o.get("order_type", o.get("type", ""))).upper()
                    if o.get("reduce_only") or order_type in ("STOP_MARKET", "TAKE_PROFIT"):
                        logger.warning(f"[RECONCILIATION] Canceling orphaned {order_type} order {o.get('id')} for {symbol}")
                        try:
                            self.execution_engine.cancel_order(o.get("id"))
                        except Exception as e:
                            logger.error(f"[RECONCILIATION] Failed to cancel orphaned order: {e}")
            
            # 5. Missing Stop Loss Detection & Recreation (PRD §18 / SRD §5.3)
            # If we have a position, ensure there is at least one active STOP order.
            # Only meaningful when the wallet hosts venue-resident stops; in paper
            # mode or when venue SL is disabled, a missing venue stop is expected.
            venue_sl_active = (
                getattr(self.wallet, "live_execution", False)
                and getattr(self.wallet, "venue_sl_enabled", False)
            )
            if ex_qty > 0 and local_pos and venue_sl_active:
                has_stop = False
                for o in exchange_open_orders:
                    order_type = str(o.get("order_type", o.get("type", ""))).upper()
                    if order_type in ("STOP_MARKET", "STOP_LOSS_MARKET", "STOP"):
                        has_stop = True
                        break

                if not has_stop and local_pos.sl_price and local_pos.sl_price > 0:
                    logger.error(
                        f"[RECONCILIATION] Missing venue stop-loss for {symbol} at "
                        f"{local_pos.sl_price} — recreating immediately."
                    )
                    # Per spec, recreate the protective stop rather than halting on a
                    # recoverable gap (e.g. a crash between fill and SL placement, or a
                    # venue-side cancel). Kill-switch only if recreation genuinely fails,
                    # i.e. the position is naked and unfixable.
                    placed = {}
                    try:
                        placed = self.wallet._place_protective_orders(local_pos)
                    except Exception as e:
                        logger.critical(
                            f"[RECONCILIATION] Stop-loss recreation raised for {symbol}: {e}"
                        )
                    if placed.get("sl"):
                        logger.warning(
                            f"[RECONCILIATION] Recreated venue stop-loss for {symbol} at "
                            f"{local_pos.sl_price} (order {placed['sl']})."
                        )
                    else:
                        msg = f"Missing venue stop-loss for open {symbol} position; recreation failed."
                        logger.critical(f"[RECONCILIATION] {msg}")
                        self.risk_manager.trigger_kill_switch(msg)
                        return False

            return True
            
        except Exception as e:
            logger.error(f"[RECONCILIATION] Error during reconciliation for {symbol}: {e}")
            return True
