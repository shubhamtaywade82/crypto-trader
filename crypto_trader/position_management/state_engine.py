"""
crypto_trader.position_management.state_engine
===============================================
Layer 1: Builds a canonical PositionSnapshot for every open position by
combining wallet state, live prices, and portfolio aggregates.

Updated on:
  - every tick / mark-price event
  - every order fill
  - every margin/wallet change
  - feed reconnect (data_age_ms / is_stale reset)
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from .schemas import (
    MarketContext,
    PortfolioContext,
    PositionConstraints,
    PositionManagementInput,
    PositionSnapshot,
    WalletContext,
)
from ..wallet import EnhancedFuturesWallet, PositionSide

logger = logging.getLogger("crypto_trader.position_management.state_engine")


class PositionStateEngine:
    """
    Reads every open position from the wallet and assembles PositionSnapshot
    objects enriched with wallet and portfolio aggregates.

    The market context fields (regime, ATR, structure …) are left at their
    defaults here and filled in by MarketContextEngine before the snapshot
    reaches the decision layer.
    """

    def __init__(
        self,
        wallet: EnhancedFuturesWallet,
        max_portfolio_risk: float = 4_000.0,
        risk_per_trade_default: float = 1_000.0,
    ):
        self.wallet = wallet
        self.max_portfolio_risk = max_portfolio_risk
        self.risk_per_trade_default = risk_per_trade_default

    # ── Public API ────────────────────────────────────────────────────────

    def build_all_snapshots(
        self,
        prices: Dict[str, float],          # symbol → current mark price
        market_contexts: Optional[Dict[str, MarketContext]] = None,
    ) -> List[PositionManagementInput]:
        """
        Returns one PositionManagementInput per open position.

        Args:
            prices: latest mark prices keyed by symbol.
            market_contexts: optional pre-computed market contexts from
                MarketContextEngine (keyed by symbol).  Missing symbols
                receive empty MarketContext defaults.
        """
        wallet_ctx = self._build_wallet_context()
        portfolio_ctx = self._build_portfolio_context(prices)

        inputs: List[PositionManagementInput] = []
        with self.wallet.lock:
            for symbol, pos in self.wallet.positions.items():
                if pos.status != "OPEN":
                    continue
                ltp = prices.get(symbol, float(pos.entry_price))
                snapshot = self._build_snapshot(symbol, pos, ltp, wallet_ctx, portfolio_ctx)
                mctx = (market_contexts or {}).get(symbol, MarketContext(symbol=symbol))
                snapshot = self._merge_market_context(snapshot, mctx)
                constraints = self._constraints_for_playbook(pos.playbook.value)
                inputs.append(
                    PositionManagementInput(
                        position=snapshot,
                        market=mctx,
                        wallet=wallet_ctx,
                        portfolio=portfolio_ctx,
                        constraints=constraints,
                    )
                )

        return inputs

    def build_snapshot(
        self,
        symbol: str,
        ltp: float,
        market_context: Optional[MarketContext] = None,
    ) -> Optional[PositionManagementInput]:
        """Build a single-position PositionManagementInput.  Returns None if
        the symbol has no open position."""
        with self.wallet.lock:
            pos = self.wallet.positions.get(symbol)
            if pos is None or pos.status != "OPEN":
                return None

        prices = {symbol: ltp}
        wallet_ctx = self._build_wallet_context()
        portfolio_ctx = self._build_portfolio_context(prices)
        snapshot = self._build_snapshot(symbol, pos, ltp, wallet_ctx, portfolio_ctx)
        mctx = market_context or MarketContext(symbol=symbol)
        snapshot = self._merge_market_context(snapshot, mctx)
        constraints = self._constraints_for_playbook(pos.playbook.value)
        return PositionManagementInput(
            position=snapshot,
            market=mctx,
            wallet=wallet_ctx,
            portfolio=portfolio_ctx,
            constraints=constraints,
        )

    # ── Private helpers ───────────────────────────────────────────────────

    def _build_snapshot(
        self,
        symbol: str,
        pos,                               # EnhancedPosition
        ltp: float,
        wallet_ctx: WalletContext,
        portfolio_ctx: PortfolioContext,
    ) -> PositionSnapshot:
        from decimal import Decimal

        entry = float(pos.entry_price)
        sl = float(pos.sl_price)
        qty = float(pos.remaining_quantity)
        notional = qty * ltp

        # Unrealized PnL
        if pos.side == PositionSide.LONG:
            upnl = (ltp - entry) * qty
        else:
            upnl = (entry - ltp) * qty
        upnl_pct = upnl / (entry * qty) if entry * qty else 0.0

        # R-multiple: (current move) / (initial risk per unit)
        risk_per_unit = abs(entry - sl) if sl and sl != entry else 1.0
        r_multiple = 0.0
        if risk_per_unit > 0:
            if pos.side == PositionSide.LONG:
                r_multiple = (ltp - entry) / risk_per_unit
            else:
                r_multiple = (entry - ltp) / risk_per_unit

        # Age
        now_ms = int(time.time() * 1000)
        age_ms = now_ms - pos.open_time if pos.open_time else 0
        age_hours = age_ms / 3_600_000

        # First TP level price (if available)
        tp_price: Optional[float] = None
        if pos.tp_levels:
            tp_price = float(pos.tp_levels[0].get("price", 0)) or None

        # Per-trade risk (entry-to-SL * qty)
        risk_per_trade = abs(entry - sl) * qty if sl else self.risk_per_trade_default

        return PositionSnapshot(
            position_id=f"{symbol}_{pos.open_time}",
            symbol=symbol,
            side=pos.side.value,
            qty=qty,
            entry_price=entry,
            ltp=ltp,
            unrealized_pnl=upnl,
            unrealized_pnl_pct=upnl_pct,
            stop_loss=sl,
            take_profit=tp_price,
            tp_levels=pos.tp_levels,
            trailing_active=pos.trailing_active,
            trailing_stop_price=pos.trailing_stop_price,
            open_time_ms=pos.open_time,
            age_hours=age_hours,
            leverage=pos.leverage,
            margin_used=float(pos.margin_used),
            notional=notional,
            r_multiple=r_multiple,
            playbook=pos.playbook.value,
            mode=pos.mode,
            # Wallet aggregates
            wallet_balance=wallet_ctx.equity,
            blocked_capital=wallet_ctx.blocked_capital,
            free_capital=wallet_ctx.free_capital,
            margin_used_portfolio=wallet_ctx.used_margin,
            # Portfolio aggregates
            total_open_positions=portfolio_ctx.open_positions,
            correlation_risk=portfolio_ctx.correlation_risk,
            total_portfolio_risk=portfolio_ctx.current_total_risk,
            max_portfolio_risk=self.max_portfolio_risk,
            risk_budget_remaining=max(0.0, self.max_portfolio_risk - portfolio_ctx.current_total_risk),
            risk_per_trade=risk_per_trade,
        )

    def _merge_market_context(
        self, snapshot: PositionSnapshot, mctx: MarketContext
    ) -> PositionSnapshot:
        """Copy market fields from MarketContext into PositionSnapshot."""
        return snapshot.model_copy(
            update=dict(
                market_regime=mctx.regime,
                volatility_state=mctx.volatility_state,
                liquidity_state=mctx.liquidity,
                trend_direction=mctx.trend,
                market_structure=mctx.structure,
                atr_pct=mctx.atr_pct,
                vwap_distance_pct=mctx.vwap_distance_pct,
                momentum_state=mctx.momentum,
                event_risk=mctx.event_risk,
                near_support=mctx.near_support,
                near_resistance=mctx.near_resistance,
                spread_pct=mctx.spread_pct,
                funding_rate=mctx.funding_rate,
                oi_change_pct=mctx.oi_change_pct,
                data_age_ms=mctx.data_age_ms,
                is_stale=mctx.is_stale,
            )
        )

    def _build_wallet_context(self) -> WalletContext:
        w = self.wallet
        equity = float(w.margin_balance)
        used = float(
            sum(p.margin_used for p in w.positions.values() if p.status == "OPEN")
        )
        free = max(0.0, float(w.available_balance))
        blocked = used
        upnl = float(w.unrealized_pnl_total)
        ratio = used / equity if equity else 0.0
        return WalletContext(
            equity=equity,
            free_capital=free,
            blocked_capital=blocked,
            used_margin=used,
            unrealized_pnl_total=upnl,
            margin_ratio=ratio,
        )

    def _build_portfolio_context(self, prices: Dict[str, float]) -> PortfolioContext:
        open_count = 0
        total_notional = 0.0
        total_risk = 0.0

        with self.wallet.lock:
            for sym, pos in self.wallet.positions.items():
                if pos.status != "OPEN":
                    continue
                open_count += 1
                ltp = prices.get(sym, float(pos.entry_price))
                notional = float(pos.remaining_quantity) * ltp
                total_notional += notional
                sl = float(pos.sl_price)
                entry = float(pos.entry_price)
                qty = float(pos.remaining_quantity)
                risk = abs(entry - sl) * qty if sl and sl != entry else 0.0
                total_risk += risk

        return PortfolioContext(
            open_positions=open_count,
            correlation_risk="high" if open_count >= 5 else ("moderate" if open_count >= 3 else "low"),
            max_total_risk=self.max_portfolio_risk,
            current_total_risk=total_risk,
            concentration_pct=0.0,    # filled per-position by caller if needed
            net_delta_exposure=total_notional,
        )

    @staticmethod
    def _constraints_for_playbook(playbook: str) -> PositionConstraints:
        """Return management constraints based on playbook type."""
        if playbook == "SWING":
            return PositionConstraints(
                allow_scale_in=True,
                allow_partial_exit=True,
                allow_breakeven_trail=True,
                allow_average_down=False,
                allow_loosen_sl=False,
                min_r_for_breakeven=1.0,
                min_r_for_partial_exit=2.0,
                trail_atr_multiplier=2.0,
                max_scale_in_count=1,
            )
        # INTRADAY and all other playbooks
        return PositionConstraints(
            allow_scale_in=False,
            allow_partial_exit=True,
            allow_breakeven_trail=True,
            allow_average_down=False,
            allow_loosen_sl=False,
            min_r_for_breakeven=1.0,
            min_r_for_partial_exit=2.0,
            trail_atr_multiplier=1.5,
            max_scale_in_count=0,
        )
