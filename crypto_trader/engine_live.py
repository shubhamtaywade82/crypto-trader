"""
crypto_trader.engine_live — Single gated production entrypoint
===============================================================
The ONE supported way to run the bot against real capital. Selects the
execution engine by ``MODE`` (paper | testnet | live) and, for live/testnet,
refuses to trade unless a startup self-test passes:

    * config is complete and valid (leverage <= cap, symbol set)
    * CoinDCX credentials authenticate (balances readable)
    * the traded instrument loads (tick/step/min-notional known & satisfiable)
    * the event store (Postgres) is reachable
    * a market-data price is available (Binance primary, CoinDCX fallback)
    * boot reconciliation is healthy (no unresolved desync)

AI remains advisory-only; it is never on the order path here.

Run:  python -m crypto_trader.engine_live
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional, Tuple

from .config import TradingConfig, TradingMode, load_config
from .events import EventBus, KillSwitchTriggeredEvent, bus as global_bus
from .risk import RiskManager
from .wallet import EnhancedFuturesWallet, PaperExecutionEngine

logger = logging.getLogger("crypto_trader.engine_live")


class LiveGateBlocked(Exception):
    """Raised when live/testnet mode fails the startup self-test."""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    critical: bool = True


class LiveTradingSystem:
    def __init__(self, cfg: Optional[TradingConfig] = None, bus: Optional[EventBus] = None):
        self.cfg = cfg or load_config()
        self.bus = bus or global_bus
        self.risk = RiskManager()
        self.event_store = None
        self.execution_engine = None
        self.router = None
        self.reconciler = None

        self.wallet = EnhancedFuturesWallet(
            symbol=self.cfg.symbol,
            initial_balance=self.cfg.initial_balance,
            leverage=self.cfg.max_leverage,
        )

    # ── construction ──────────────────────────────────────────────────────
    def _build_execution_engine(self):
        if self.cfg.mode == TradingMode.PAPER:
            return PaperExecutionEngine(self.wallet)
        from .exchanges.coindcx_execution import CoinDCXExecutionEngine
        return CoinDCXExecutionEngine(
            api_key=self.cfg.coindcx_api_key,
            api_secret=self.cfg.coindcx_api_secret,
            leverage=self.cfg.max_leverage,
        )

    def _build_market_data(self):
        from .exchanges.market_data_router import MarketDataRouter
        return MarketDataRouter.from_config(self.cfg, bus=self.bus)

    def _build_event_store(self):
        from .storage import get_event_store
        return get_event_store(self.cfg.database_url)

    # ── self-test gate ────────────────────────────────────────────────────
    def preflight(self) -> List[Check]:
        checks: List[Check] = []

        cfg_errors = self.cfg.validate()
        checks.append(Check("config", not cfg_errors, "; ".join(cfg_errors) or "valid"))

        # Event store
        try:
            self.event_store = self._build_event_store()
            checks.append(Check("event_store", True, type(self.event_store).__name__, critical=self.cfg.is_live))
        except Exception as e:
            checks.append(Check("event_store", False, str(e), critical=self.cfg.is_live))

        # Market data: at least one source yields a price
        try:
            self.router = self._build_market_data()
            price = self.router.get_mark_price(self.cfg.symbol)
            checks.append(Check("market_data", price > 0, f"{self.router.active_source} @ {price}"))
        except Exception as e:
            checks.append(Check("market_data", False, str(e)))

        self.execution_engine = self._build_execution_engine()

        if self.cfg.is_live:
            checks.extend(self._live_venue_checks())
        else:
            checks.append(Check("mode", True, "paper (simulated fills)", critical=False))

        return checks

    def _live_venue_checks(self) -> List[Check]:
        checks: List[Check] = []
        # Credentials authenticate
        usdt_balance = 0.0
        try:
            balances = self.execution_engine.get_balances()
            usdt_balance = float(balances.get("USDT", 0.0))
            checks.append(Check("coindcx_auth", True, f"USDT={usdt_balance}"))
        except Exception as e:
            checks.append(Check("coindcx_auth", False, str(e)))

        # Instrument loads + leverage cap + min-notional satisfiable at current price
        try:
            spec = self.execution_engine.mapper.get_spec(self.cfg.symbol)
            lev_ok = self.cfg.max_leverage <= spec.max_leverage
            checks.append(Check("leverage_cap", lev_ok,
                                f"cfg {self.cfg.max_leverage}x <= venue max {spec.max_leverage}x"))
            price = self.router.get_mark_price(self.cfg.symbol) if self.router else 0.0
            min_qty_notional = float(spec.min_quantity) * price
            # Affordability uses the REAL venue USDT margin, not the internal balance.
            affordable = usdt_balance * self.cfg.max_leverage
            ok = price > 0 and float(spec.min_notional) <= affordable
            detail = (f"USDT={usdt_balance}, affordable_notional={affordable:.2f}, "
                      f"min_notional={spec.min_notional}, min_qty_notional={min_qty_notional:.2f}")
            if not ok and usdt_balance <= 0:
                detail += "  [account not funded with USDT margin]"
            checks.append(Check("min_notional", ok, detail))
        except Exception as e:
            checks.append(Check("instrument", False, str(e)))

        # Boot reconciliation healthy
        try:
            from .execution.reconciler import Reconciler
            self.reconciler = Reconciler(self.wallet, self.execution_engine, self.risk, bus=self.bus)
            mismatches = self.reconciler.reconcile(self.cfg.symbol)
            unresolved = [m for m in mismatches if not m.repaired]
            # Block startup if truth is unverifiable (transient) OR there is a
            # real unresolved desync OR a kill switch is already latched.
            if self.reconciler.snapshot_failed:
                checks.append(Check("reconciliation", False, "venue snapshot unavailable"))
            elif self.risk.kill_switch:
                checks.append(Check("reconciliation", False, self.risk.kill_switch_reason or "kill switch active"))
            else:
                checks.append(Check("reconciliation", not unresolved,
                                    f"{len(unresolved)} unresolved" if unresolved else "healthy"))
        except Exception as e:
            checks.append(Check("reconciliation", False, str(e)))

        return checks

    def startup_self_test(self) -> Tuple[bool, List[Check]]:
        checks = self.preflight()
        for c in checks:
            flag = "OK " if c.ok else "FAIL"
            logger.info("[SELF-TEST] %-16s %s  %s", c.name, flag, c.detail)
        critical_failures = [c for c in checks if c.critical and not c.ok]
        passed = not critical_failures
        if self.cfg.is_live and not passed:
            names = ", ".join(c.name for c in critical_failures)
            self.bus.publish(KillSwitchTriggeredEvent(
                reason=f"startup self-test failed: {names}", source="startup"))
            raise LiveGateBlocked(
                f"LIVE TRADING BLOCKED — failed checks: {names}. "
                f"Fix config/credentials/venue state and retry."
            )
        return passed, checks

    # ── run ───────────────────────────────────────────────────────────────
    def start(self, signal_interval_seconds: int = 300, max_iterations: Optional[int] = None):
        logger.info("Starting LiveTradingSystem | %s", self.cfg.redacted())
        self.startup_self_test()

        if self.cfg.is_live:
            self.wallet.attach_execution_engine(self.execution_engine, live=True)
        if self.event_store is not None:
            prev = self.wallet.event_hook
            def _sink(ev):
                try:
                    self.event_store.append(ev)
                except Exception as e:  # never let journaling kill trading
                    logger.error("event store append failed: %s", e)
                if prev:
                    prev(ev)
            self.wallet.event_hook = _sink

        from .engine_ws import WebSocketTradingEngine
        engine = WebSocketTradingEngine(
            symbol=self.cfg.symbol,
            leverage=self.cfg.max_leverage,
            wallet=self.wallet,
            event_bus=self.bus,
            use_llm=False if self.cfg.mode == TradingMode.PAPER else True,
        )
        engine.risk_manager = self.risk  # share kill switch with reconciler
        engine.run_loop(signal_interval_seconds=signal_interval_seconds, max_iterations=max_iterations)


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Crypto Trader — gated live entrypoint")
    parser.add_argument("--tick", type=int, default=300, help="signal interval seconds")
    parser.add_argument("--max-iterations", type=int, default=None)
    parser.add_argument("--self-test-only", action="store_true", help="run the gate and exit")
    args = parser.parse_args()

    system = LiveTradingSystem()
    if args.self_test_only:
        passed, _ = system.startup_self_test()
        raise SystemExit(0 if passed else 1)
    system.start(signal_interval_seconds=args.tick, max_iterations=args.max_iterations)


if __name__ == "__main__":
    main()
