import argparse
import time
import threading
import os
import sys
from typing import List, Optional
import logging
import fcntl

from crypto_trader.engine_ws import WebSocketTradingEngine
from crypto_trader.wallet import EnhancedFuturesWallet
from crypto_trader.exchanges.coindcx_execution import CoinDCXExecutionEngine
from crypto_trader.logger_config import configure_colored_logging
from crypto_trader.events import bus
from crypto_trader.telegram_bot import TelegramService
from crypto_trader.risk import RiskManager
from crypto_trader import safe_mode

# Env variables are loaded by crypto_trader.__init__ (_load_dotenv):
#   .env        → non-sensitive config  (no-override)
#   .env.secrets → API keys / tokens    (always overrides .env)

configure_colored_logging()
logger = logging.getLogger("multi_engine")

def acquire_lock():
    """Acquire a file lock to prevent multiple instances."""
    lock_file = "/tmp/crypto_trader_multi_engine.lock"
    
    # Try to open/create the file
    mode = "r+" if os.path.exists(lock_file) else "w+"
    lock_fd = open(lock_file, mode)

    try:
        fcntl.lockf(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Got the lock! Write current PID.
        lock_fd.seek(0)
        lock_fd.truncate()
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        return lock_fd
    except IOError:
        # Failed to get lock. Try to read PID from file.
        lock_fd.seek(0)
        existing_pid = lock_fd.read().strip()
        print(f"\nERROR: Another instance of multi_engine is already running.")
        if existing_pid:
            print(f"Existing PID: {existing_pid} (check lock file {lock_file})")
            print(f"Please stop it with: kill {existing_pid}")
        else:
            print(f"Check lock file {lock_file}")
        sys.exit(1)

from pathlib import Path

def parse_args():
    parser = argparse.ArgumentParser(description="Multi-Symbol WebSocket Trading Engine")
    parser.add_argument("--symbols", type=str, default=None, help="Comma-separated list of symbols (e.g., BTCUSDT,ETHUSDT)")
    # Default is the sentinel ``None`` (NOT an env-derived literal) so an explicit
    # ``--leverage`` on the CLI is distinguishable from "unset". resolve_leverage()
    # then applies precedence: explicit CLI override > cfg.leverage. Leaving this
    # env-derived would silently disagree with cfg.leverage (which falls back to the
    # active profile cap, e.g. 3 for moderate, not the literal 5 used here).
    parser.add_argument("--leverage", type=int, default=None,
                        help="Leverage multiplier (overrides cfg/profile leverage when set)")
    parser.add_argument("--no-llm", action="store_true",
                        default=os.getenv("USE_LLM", "true").lower() in ("false", "0", "no"),
                        help="Disable LLM advisor (also: USE_LLM=false in .env)")
    parser.add_argument("--llm-host", type=str, default=None, help="Ollama host URL")
    parser.add_argument("--llm-model", type=str, default=None, help="Ollama model name")
    parser.add_argument("--log-responses", action="store_true", help="Log all API/WS responses")
    parser.add_argument("--log-rest", action="store_true", help="Log REST API responses")
    parser.add_argument("--log-ws", action="store_true", help="Log WebSocket messages")
    parser.add_argument("--log-llm", action="store_true", help="Log LLM prompts and raw responses")
    return parser.parse_args()

def load_watchlist(symbols_arg: Optional[str] = None) -> List[str]:
    # 1. Command-line argument
    if symbols_arg:
        return [s.strip().upper() for s in symbols_arg.split(",") if s.strip()]

    # 2. watchlist.yaml
    yaml_path = Path("watchlist.yaml")
    if yaml_path.exists():
        try:
            import yaml
            with open(yaml_path, "r") as f:
                data = yaml.safe_load(f)
                if isinstance(data, dict) and "watchlist" in data:
                    symbols = data["watchlist"]
                elif isinstance(data, list):
                    symbols = data
                else:
                    symbols = []
                if symbols:
                    return [str(s).strip().upper() for s in symbols if s]
        except Exception as e:
            logger.warning(f"Failed to read watchlist.yaml: {e}")

    # 3. WATCHLIST env var from .env
    watchlist_env = os.getenv("WATCHLIST")
    if watchlist_env:
        return [s.strip().upper() for s in watchlist_env.split(",") if s.strip()]

    # 4. TRADE_SYMBOL env var from .env
    trade_symbol = os.getenv("TRADE_SYMBOL")
    if trade_symbol:
        return [trade_symbol.strip().upper()]

    return []

def resolve_leverage(cli_leverage: Optional[int], cfg) -> int:
    """Reconcile the leverage source between the engine constructor and cfg.

    Precedence (intentional, documented): an explicit CLI ``--leverage`` override
    WINS over cfg; otherwise cfg.leverage (sourced from MAX_LEVERAGE/LEVERAGE env or
    the active trading profile cap) is used.

    Why mutate cfg on override: the position-sizing path reads ``self.cfg.leverage``
    directly (engine_ws.py:1538) while the constructor receives a separate
    ``leverage=`` argument. If the CLI override only flowed to the constructor, the
    two would silently disagree (constructor sees the override, sizing/liq-distance
    sees the un-overridden cfg.leverage). To keep a single source of truth we push
    the override back into ``cfg.max_leverage`` so both reads agree. When no CLI
    override is supplied, cfg is left untouched and cfg.leverage governs both.
    """
    if cli_leverage is not None:
        cfg.max_leverage = int(cli_leverage)
        return int(cli_leverage)
    return int(cfg.leverage)


def run_preflight_checks(
    symbols: List[str],
    wallet: EnhancedFuturesWallet,
    execution_engine: CoinDCXExecutionEngine,
    risk: RiskManager,
    bus,
    cfg=None,
) -> bool:
    """Delegate all preflight logic to the canonical ``run_venue_preflight`` in engine_live."""
    from .engine_live import run_venue_preflight
    if cfg is None:
        from .config import load_config
        cfg = load_config()
    return run_venue_preflight(symbols, wallet, execution_engine, risk, bus, cfg)

def run_engine(engine: WebSocketTradingEngine):
    try:
        engine.run_loop()
    except Exception as e:
        logger.error(f"Engine loop crashed for {engine.symbol}: {e}")
    finally:
        engine.stop()

def main():
    lock_fd = acquire_lock()
    args = parse_args()
    
    symbols = load_watchlist(args.symbols)
    if not symbols:
        logger.error("No valid symbols found. Provide --symbols, a watchlist.yaml file, or WATCHLIST/TRADE_SYMBOL in .env.")
        sys.exit(1)

    logger.info(f"Starting Multi-Engine for {len(symbols)} symbols: {', '.join(symbols)}")
    engines = []
    threads = []
    
    # Load config early so profile-driven risk parameters apply to the global risk
    # manager AND to each per-symbol engine. This single cfg object is reused for
    # the wallet, risk manager, and engines so config-gated behaviors (sizing,
    # strategy_mode, allowed_regimes, kill-zone, basis-guard, funding thresholds,
    # leverage, live-mode gating) are honored fleet-wide instead of each engine
    # falling back to hardcoded defaults (self.cfg=None).
    from crypto_trader.config import load_config as _load_cfg_early
    _early_cfg = _load_cfg_early()
    # Reconcile leverage source: explicit CLI --leverage wins over cfg, otherwise
    # cfg.leverage governs. resolve_leverage mutates cfg.max_leverage on override so
    # the constructor leverage and the sizing path's self.cfg.leverage never disagree.
    resolved_leverage = resolve_leverage(args.leverage, _early_cfg)
    total_balance = _early_cfg.initial_balance
    global_risk = RiskManager(
        max_daily_trades=_early_cfg.max_daily_trades,
        max_consecutive_losses=_early_cfg.max_consecutive_losses,
        max_drawdown_pct=_early_cfg.max_drawdown_pct,
        max_daily_drawdown_pct=_early_cfg.max_daily_drawdown_pct,
        cooldown_after_loss_minutes=_early_cfg.cooldown_after_loss_minutes,
        max_orders_per_minute=_early_cfg.max_orders_per_minute,
        max_margin_ratio=_early_cfg.max_margin_ratio,
        max_margin_utilization=_early_cfg.max_margin_utilization,
    )
    logger.info(
        "Trading profile: %s (threshold=%.2f, max_trades=%d, regimes=%s)",
        _early_cfg.trading_profile, _early_cfg.final_score_threshold,
        _early_cfg.max_daily_trades, _early_cfg.allowed_regimes,
    )

    # Initialize CoinDCX live execution engine if live trading is enabled
    live_enabled = os.getenv("LIVE_TRADING_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    live_ack = os.getenv("LIVE_TRADING_ACK", "") == "I_UNDERSTAND_REAL_MONEY_WILL_BE_LOST"
    
    execution_engine = None
    if live_enabled:
        api_key = os.getenv("COINDCX_API_KEY")
        api_secret = os.getenv("COINDCX_API_SECRET")
        if not api_key or not api_secret:
            logger.error("LIVE_TRADING_ENABLED is true, but COINDCX_API_KEY/SECRET is missing!")
            raise ValueError("Missing CoinDCX API Key/Secret for live trading.")
        
        execution_engine = CoinDCXExecutionEngine(
            api_key=api_key,
            api_secret=api_secret,
            leverage=resolved_leverage,
            margin_currency=_early_cfg.coindcx_margin_currency,
            i_understand_real_money=live_ack
        )
        logger.warning("⚠️ LIVE TRADING IS ENABLED! Real orders will be routed to CoinDCX.")
        
    # ── Database Projections & Event Journal for Dashboard UI ──
    # Reuse the SAME cfg object loaded above (carries any CLI leverage override) so
    # the wallet, projections, and per-symbol engines all share one config.
    cfg = _early_cfg

    # Create one shared wallet for all engines
    global_wallet = EnhancedFuturesWallet(
        symbol="GLOBAL",
        initial_balance=total_balance,
        leverage=resolved_leverage,
        database_url=cfg.database_url,
        event_bus=bus,
    )
    
    event_store = None
    projection = None
    ui_publisher = None
    
    try:
        from pathlib import Path as _Path
        from crypto_trader.storage import get_event_store
        _sqlite_fallback = str(_Path.home() / ".crypto_trader" / "event_journal.db")
        event_store = get_event_store(cfg.database_url, sqlite_path=_sqlite_fallback)
        logger.info("Event store initialized for multi-engine (%s)",
                    "Postgres" if cfg.database_url else "SQLite fallback")
    except Exception as e:
        logger.error("Failed to initialize event store: %s", e)
            
    if cfg.projection_enabled and cfg.database_url:
        try:
            from crypto_trader.storage.projection import PostgresProjection
            projection = PostgresProjection(cfg.database_url, mode=cfg.mode.value)
            logger.info("Postgres projection read-model enabled for multi-engine")
            try:
                projection.align_with_wallet(global_wallet)
            except Exception as ex:
                logger.error("Failed to align projection with wallet on boot: %s", ex)
        except Exception as e:
            logger.error("Failed to initialize Postgres projection: %s", e)
            
    if cfg.ui_events_enabled and cfg.redis_url:
        try:
            from crypto_trader.api.events_bridge import RedisEventPublisher
            ui_publisher = RedisEventPublisher(cfg.redis_url, cfg.ui_events_channel)
            logger.info("Realtime UI event relay enabled for multi-engine (channel=%s)", cfg.ui_events_channel)
        except Exception as e:
            logger.error("Failed to initialize UI event relay: %s", e)
            
    from crypto_trader.infra.event_routing import build_event_sink
    _sink = build_event_sink(
        event_store=event_store,
        projection=projection,
        ui_publisher=ui_publisher,
        mode=cfg.mode.value,
        prev_hook=global_wallet.event_hook,
    )
    if _sink is not None:
        global_wallet.event_hook = _sink
    if execution_engine is not None:
        global_wallet.attach_execution_engine(execution_engine, live=True)
        # Sync per-symbol fee rates from venue instrument specs so the wallet
        # uses real CoinDCX maker/taker rates instead of hardcoded defaults.
        for sym in symbols:
            global_wallet.sync_fee_rates_from_spec(sym)
        try:
            live_bal = execution_engine.sync_balance()
            if live_bal is not None:
                mc = cfg.coindcx_margin_currency.upper()
                if mc != "USDT":
                    conv_raw = execution_engine.get_usdt_conversion()
                    conv = float(conv_raw) if conv_raw else 0.0
                    if conv <= 0:
                        raise RuntimeError(
                            f"USDT/{mc} conversion rate unavailable or invalid ({conv_raw!r}). "
                            f"Cannot safely convert balance for {mc}-margined account."
                        )
                    live_bal = live_bal / conv
                global_wallet.wallet_balance = global_wallet._to_decimal(live_bal)
                total_balance = live_bal
                # Sync peak balance in RiskManager to prevent false drawdown trip (1000 -> 0)
                global_risk.peak_balance = live_bal
                global_risk._save_state()
                logger.info("[LIVE BALANCE SYNC] Synchronized wallet balance with exchange: %.4f USDT", live_bal)
        except Exception as e:
            logger.error("[LIVE BALANCE SYNC] Failed to sync wallet balance with exchange: %s", e)

    # Run pre-flight checks and state reconciliation in live mode
    if live_enabled and execution_engine is not None:
        if not run_preflight_checks(symbols, global_wallet, execution_engine, global_risk, bus, cfg):
            logger.critical("Multi-engine startup aborted due to pre-flight self-test or reconciliation failure.")
            sys.exit(1)
    
    # Initialize Telegram Service if token is available
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if telegram_token and telegram_chat_id:
        tg_service = TelegramService(telegram_token, telegram_chat_id, bus)
        tg_service.attach_wallet(global_wallet)
        tg_service.start()
        logger.info("Telegram notification service started")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing. Notifications disabled.")

    # Divide total balance equally among symbols to prevent overallocation
    per_symbol_balance = total_balance / len(symbols)

    # Shared wallet: bound each symbol's per-trade risk to 1/N of the budget so
    # N concurrent positions don't collectively risk N × cfg.risk_per_trade_pct of
    # the whole account. This composes with cfg.risk_per_trade_pct in sizing
    # (engine_ws.py:1510-1511): per-symbol per-trade risk = risk_per_trade_pct × 1/N.
    risk_fraction = 1.0 / max(1, len(symbols))

    for sym in symbols:
        engine = WebSocketTradingEngine(
            symbol=sym,
            wallet=global_wallet,
            initial_balance=per_symbol_balance,
            leverage=resolved_leverage,
            # Pass the loaded cfg so this engine honors the TradingConfig (sizing
            # risk_per_trade_pct, strategy_mode, allowed_regimes, kill-zone,
            # basis-guard, funding thresholds, leverage, live gating) instead of
            # silently falling back to hardcoded defaults (self.cfg=None).
            # resolved_leverage and cfg.leverage are kept consistent by
            # resolve_leverage() above, so the constructor and sizing path agree.
            cfg=cfg,
            use_llm=not args.no_llm,
            llm_host=args.llm_host,
            llm_model=args.llm_model,
            log_responses=args.log_responses,
            log_rest=args.log_rest,
            log_ws=args.log_ws,
            log_llm=args.log_llm,
            event_bus=bus,
            risk_budget_fraction=risk_fraction,
        )
        engine.risk_manager = global_risk  # Share the global risk manager
        engines.append(engine)
        
        # Stagger startup to avoid Binance rate limits (max 5 connections per second)
        time.sleep(1.5)

    try:
        for engine in engines:
            t = threading.Thread(target=run_engine, args=(engine,), daemon=True)
            threads.append(t)
            t.start()
            
        # Keep main thread alive
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("Multi-engine shutting down via KeyboardInterrupt...")
    finally:
        for engine in engines:
            engine.stop()
        try:
            lock_fd.close()
        except Exception:
            pass
        logger.info("All engines stopped.")

if __name__ == "__main__":
    main()
