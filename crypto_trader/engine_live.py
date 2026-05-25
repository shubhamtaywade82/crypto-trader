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
        self.risk = RiskManager(max_orders_per_minute=self.cfg.max_orders_per_minute)
        self.event_store = None
        self.execution_engine = None
        self.router = None
        self.reconciler = None
        self.user_stream = None
        self.projection = None

        self.wallet = EnhancedFuturesWallet(
            symbol=self.cfg.symbol,
            initial_balance=self.cfg.initial_balance,
            leverage=self.cfg.max_leverage,
            tds_enabled=self.cfg.tds_enabled,
            tds_rate=self.cfg.tds_rate,
            # Paper degradation only applies to simulated fills.
            paper_spread_coeff=(self.cfg.paper_cdcx_spread_coeff
                                if self.cfg.mode == TradingMode.PAPER else None),
            paper_collar_pct=self.cfg.paper_collar_pct,
            venue_sl_enabled=self.cfg.venue_sl_enabled,
            venue_tp_enabled=self.cfg.venue_tp_enabled,
            require_venue_sl=self.cfg.require_venue_sl,
            software_sl_backup_bps=self.cfg.software_sl_backup_bps,
        )

    # ── construction ──────────────────────────────────────────────────────
    def _build_execution_engine(self):
        if self.cfg.mode == TradingMode.PAPER:
            return PaperExecutionEngine(self.wallet)
        from .exchanges.coindcx_execution import CoinDCXExecutionEngine
        from .exchanges.coindcx_client import CoinDCXClient
        from .exchanges.instrument_mapper import InstrumentMapper
        client = CoinDCXClient(api_key=self.cfg.coindcx_api_key, api_secret=self.cfg.coindcx_api_secret)
        mapper = InstrumentMapper(client, margin_currency=self.cfg.coindcx_margin_currency)
        return CoinDCXExecutionEngine(
            client=client,
            mapper=mapper,
            leverage=self.cfg.max_leverage,
            margin_currency=self.cfg.coindcx_margin_currency,
            i_understand_real_money=True,
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
        from . import safe_mode
        checks: List[Check] = []

        # Safe-mode gate (PR #11 defense-in-depth): env LIVE_TRADING_ENABLED +
        # LIVE_TRADING_ACK + no HALT file, on top of MODE=live.
        gate_open = safe_mode.is_live_enabled()
        gate_detail = "open" if gate_open else (
            f"need {safe_mode.LIVE_ENV_VAR}=true, {safe_mode.ACK_ENV_VAR}='{safe_mode.ACK_PHRASE}', "
            f"no HALT file"
        )
        checks.append(Check("safe_mode_gate", gate_open, gate_detail))

        # G1: clock skew vs the venue. Advisory (Date-header resolution is ~1s),
        # so it warns rather than blocks unless drift is egregiously large.
        try:
            skew = self.execution_engine.client.measure_clock_skew_ms()
            if skew is None:
                checks.append(Check("clock_skew", True, "unavailable (probe failed)", critical=False))
            else:
                within = abs(skew) <= self.cfg.clock_skew_max_ms
                checks.append(Check("clock_skew", within,
                                    f"drift {skew:+.0f}ms (max {self.cfg.clock_skew_max_ms}ms)",
                                    critical=False))
                if not within:
                    logger.warning("[CLOCK] local clock drifts %.0fms from CoinDCX — "
                                   "sync NTP to avoid signature/window auth errors", skew)
        except Exception as e:
            checks.append(Check("clock_skew", True, f"skipped ({e})", critical=False))

        # Credentials authenticate; available margin (in the wallet's currency)
        mc = self.cfg.coindcx_margin_currency
        avail = 0.0
        usdt_equiv = 0.0
        try:
            avail = float(self.execution_engine.sync_balance())
            conv = float(self.execution_engine.get_usdt_conversion()) or 1.0
            usdt_equiv = avail / conv if mc != "USDT" else avail
            checks.append(Check("coindcx_auth", True, f"avail {avail} {mc} (~{usdt_equiv:.2f} USDT)"))
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
            # Affordability uses the REAL venue margin (converted to USDT-equiv).
            affordable = usdt_equiv * self.cfg.max_leverage
            ok = price > 0 and float(spec.min_notional) <= affordable
            detail = (f"avail~{usdt_equiv:.2f} USDT, affordable_notional={affordable:.2f}, "
                      f"min_notional={spec.min_notional}, min_qty_notional={min_qty_notional:.2f}")
            if not ok and usdt_equiv <= 0:
                detail += f"  [futures wallet not funded with {mc} margin]"
            checks.append(Check("min_notional", ok, detail))
        except Exception as e:
            checks.append(Check("instrument", False, str(e)))

        # Boot reconciliation healthy
        try:
            from .execution.reconciler import Reconciler
            self.reconciler = Reconciler(self.wallet, self.execution_engine, self.risk,
                                         bus=self.bus, strict_cancel=self.cfg.reconcile_strict_cancel)
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

    # ── CoinDCX private/user stream (F5, optional) ──────────────────────────
    def _maybe_start_user_stream(self):
        if not self.cfg.coindcx_user_stream_enabled:
            return
        from .exchanges.coindcx_user_stream import CoinDCXUserStream
        self.user_stream = CoinDCXUserStream(
            api_key=self.cfg.coindcx_api_key,
            api_secret=self.cfg.coindcx_api_secret,
            bus=self.bus,
            url=self.cfg.coindcx_stream_url,
            channel=self.cfg.coindcx_stream_channel,
            on_fill=self._on_stream_fill,
            on_reconnect=self._safe_reconcile,
        )
        started = self.user_stream.start()
        logger.info("CoinDCX user stream: %s", "started" if started else "unavailable (REST fallback)")

    def _on_stream_fill(self, fill: dict):
        """Book an exit instantly when a resting venue protective order fills.

        Only acts on fills that match a tracked protective order id; entries are
        already booked at placement time. ``close_position`` is idempotent, so a
        later REST reconcile that sees the same exit is a no-op.
        """
        sym = fill.get("symbol")
        oid = str(fill.get("exchange_order_id") or "")
        if not sym or not oid:
            return
        pos = self.wallet.get_open_position(sym)
        if not pos:
            return
        protective_ids = {str(v) for v in (pos.protective_orders or {}).values() if v}
        if oid in protective_ids:
            self.wallet.apply_external_fill(
                sym, pos.side, fill.get("fill_price", 0), fill.get("fill_quantity", 0),
                order_id=oid, reduce_only=True,
            )

    def _safe_reconcile(self):
        if self.reconciler is not None:
            try:
                self.reconciler.reconcile(self.cfg.symbol)
            except Exception as e:
                logger.warning("post-reconnect reconcile failed: %s", e)

    # ── run ───────────────────────────────────────────────────────────────
    def start(self, signal_interval_seconds: int = 300, max_iterations: Optional[int] = None):
        logger.info("Starting LiveTradingSystem | %s", self.cfg.redacted())
        self.startup_self_test()

        if self.cfg.is_live:
            self.wallet.attach_execution_engine(self.execution_engine, live=True)
            self._maybe_start_user_stream()
        # Optional Postgres read-model (projection) derived from the event stream.
        if self.cfg.projection_enabled and self.cfg.database_url:
            try:
                from .storage.projection import PostgresProjection
                self.projection = PostgresProjection(self.cfg.database_url, mode=self.cfg.mode.value)
                logger.info("Postgres projection (read-model) enabled")
            except Exception as e:
                logger.error("projection init failed (continuing without it): %s", e)
                self.projection = None

        # Realtime UI relay: publish each event to Redis pub/sub for the dashboard.
        ui_publisher = None
        if self.cfg.ui_events_enabled and self.cfg.redis_url:
            try:
                from .api.events_bridge import RedisEventPublisher
                ui_publisher = RedisEventPublisher(self.cfg.redis_url, self.cfg.ui_events_channel)
                logger.info("Realtime UI event relay enabled (channel=%s)", self.cfg.ui_events_channel)
            except Exception as e:
                logger.error("UI event relay init failed (continuing without it): %s", e)

        if self.event_store is not None or self.projection is not None or ui_publisher is not None:
            prev = self.wallet.event_hook
            def _sink(ev):
                if self.event_store is not None:
                    try:
                        self.event_store.append(ev)
                    except Exception as e:  # never let journaling kill trading
                        logger.error("event store append failed: %s", e)
                if self.projection is not None:
                    try:
                        self.projection.apply(ev)
                    except Exception as e:  # projection is derived; never fatal
                        logger.error("projection apply failed: %s", e)
                if ui_publisher is not None:
                    ui_publisher(ev)  # already swallows its own errors
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
            cfg=self.cfg,
        )
        engine.risk_manager = self.risk  # share kill switch with reconciler
        # AUTO failover for the signal tick: Binance primary, CoinDCX fallback
        # (klines + mark price + funding) so the loop survives a Binance geo-block.
        from .exchanges.resilient_data_feed import ResilientDataFeed
        engine.data_feed = ResilientDataFeed.from_config(self.cfg)
        logger.info("Signal-tick data feed: resilient (%s primary)", self.cfg.data_source.value)
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
