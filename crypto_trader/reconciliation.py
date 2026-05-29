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
            # 1. Fetch Actual State (Exchange)
            exchange_positions = self.execution_engine.get_positions()
            exchange_open_orders = self.execution_engine.get_open_orders(symbol)
            
            # Find the active position on the exchange for this symbol
            ex_pos = None
            for p in exchange_positions:
                if p.get("symbol") == symbol:
                    ex_pos = p
                    break
                    
            # 2. Fetch Desired State (Local)
            local_pos = self.wallet.get_open_position(symbol)
            
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
            
            # 5. Missing Stop Loss Detection
            # If we have a position, ensure there is at least one active STOP order.
            if ex_qty > 0 and local_pos:
                has_stop = False
                for o in exchange_open_orders:
                    order_type = str(o.get("order_type", o.get("type", ""))).upper()
                    if order_type == "STOP_MARKET" or order_type == "STOP_LOSS_MARKET" or order_type == "STOP":
                        has_stop = True
                        break
                        
                if not has_stop and local_pos.sl_price and local_pos.sl_price > 0:
                    logger.error(f"[RECONCILIATION] CRITICAL: Missing venue stop-loss for {symbol} at {local_pos.sl_price}!")
                    # To be perfectly safe, trigger kill switch instead of blind-recreating,
                    # because we want to ensure the position isn't running naked without intention.
                    msg = f"Missing venue stop-loss for open {symbol} position."
                    self.risk_manager.trigger_kill_switch(msg)
                    return False
                    
            return True
            
        except Exception as e:
            logger.error(f"[RECONCILIATION] Error during reconciliation for {symbol}: {e}")
            return True
