import argparse
import time
import threading
import os
import sys
from typing import List
import logging
import fcntl
from dotenv import load_dotenv

from crypto_trader.engine_ws import WebSocketTradingEngine
from crypto_trader.wallet import EnhancedFuturesWallet
from crypto_trader.exchanges.coindcx_execution import CoinDCXExecutionEngine
from crypto_trader.logger_config import configure_colored_logging
from crypto_trader.events import bus
from crypto_trader.telegram_bot import TelegramService

# Load environment variables for Telegram
load_dotenv()

configure_colored_logging()
logger = logging.getLogger("multi_engine")

def acquire_lock():
    """Acquire a file lock to prevent multiple instances."""
    lock_file = "/tmp/crypto_trader_multi_engine.lock"
    lock_fd = open(lock_file, "w")
    try:
        fcntl.lockf(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Write PID to lock file
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        return lock_fd
    except IOError:
        print(f"\nERROR: Another instance of multi_engine is already running (check {lock_file}).")
        print("Please stop the existing instance before starting a new one.")
        sys.exit(1)

def parse_args():
    parser = argparse.ArgumentParser(description="Multi-Symbol WebSocket Trading Engine")
    parser.add_argument("--symbols", type=str, required=True, help="Comma-separated list of symbols (e.g., BTCUSDT,ETHUSDT)")
    parser.add_argument("--leverage", type=int, default=10, help="Leverage multiplier")
    parser.add_argument("--testnet", action="store_true", help="Use Binance Testnet")
    parser.add_argument("--no-llm", action="store_true", help="Disable LLM advisor")
    parser.add_argument("--llm-host", type=str, default=None, help="Ollama host URL")
    parser.add_argument("--llm-model", type=str, default=None, help="Ollama model name")
    parser.add_argument("--log-responses", action="store_true", help="Log all API/WS responses")
    parser.add_argument("--log-rest", action="store_true", help="Log REST API responses")
    parser.add_argument("--log-ws", action="store_true", help="Log WebSocket messages")
    parser.add_argument("--log-llm", action="store_true", help="Log LLM prompts and raw responses")
    return parser.parse_args()

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
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        logger.error("No valid symbols provided.")
        return

    logger.info(f"Starting Multi-Engine for {len(symbols)} symbols: {', '.join(symbols)}")
    engines = []
    threads = []
    
    total_balance = 1000.0
    
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
            leverage=args.leverage,
            i_understand_real_money=live_ack
        )
        logger.warning("⚠️ LIVE TRADING IS ENABLED! Real orders will be routed to CoinDCX.")
        
    # Create one shared wallet for all engines
    global_wallet = EnhancedFuturesWallet(
        symbol="GLOBAL", 
        initial_balance=total_balance, 
        leverage=args.leverage,
    )
    if execution_engine is not None:
        global_wallet.attach_execution_engine(execution_engine, live=True)

    
    # Initialize Telegram Service if token is available
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if telegram_token and telegram_chat_id:
        tg_service = TelegramService(telegram_token, telegram_chat_id, bus)
        tg_service.start()
        logger.info("Telegram notification service started")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing. Notifications disabled.")

    # Divide total balance equally among symbols to prevent overallocation
    per_symbol_balance = total_balance / len(symbols)

    for sym in symbols:
        engine = WebSocketTradingEngine(
            symbol=sym,
            wallet=global_wallet,
            initial_balance=per_symbol_balance,
            leverage=args.leverage,
            testnet=args.testnet,
            use_llm=not args.no_llm,
            llm_host=args.llm_host,
            llm_model=args.llm_model,
            log_responses=args.log_responses,
            log_rest=args.log_rest,
            log_ws=args.log_ws,
            log_llm=args.log_llm,
            event_bus=bus,
        )
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
        logger.info("All engines stopped.")

if __name__ == "__main__":
    main()
