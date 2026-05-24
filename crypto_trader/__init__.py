"""
crypto_trader — Production-Grade Binance USD-M Futures Trading System
=======================================================================
A modular, testable, and extensible algorithmic trading framework.

Modules:
    data_feed     — Binance REST API client (klines, mark price, funding, OI)
    wallet        — Position tracking, PnL, margin, persistence
    risk          — Daily limits, consecutive loss circuit breaker
    playbooks     — Intraday Snap + Swing entry logic
    regime        — Multi-timeframe trend classification
    llm_advisor   — Ollama integration (advisory only, weighted fusion)
    engine        — Orchestrator: data → regime → signal → LLM → risk → execute

Usage:
    from crypto_trader.engine import TradingEngine
    engine = TradingEngine(symbol="SOLUSDT", use_llm=True)
    engine.run_once()
"""
from __future__ import annotations

# ── Load Environment Variables from .env ──
def _load_dotenv():
    import os
    from pathlib import Path
    
    # Check CWD first, then the parent directory of this package
    cwd_env = Path.cwd() / ".env"
    pkg_parent_env = Path(__file__).resolve().parent.parent / ".env"
    
    dotenv_path = None
    if cwd_env.is_file():
        dotenv_path = cwd_env
    elif pkg_parent_env.is_file():
        dotenv_path = pkg_parent_env
        
    if dotenv_path:
        try:
            with open(dotenv_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip()
                        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                            val = val[1:-1]
                        if key not in os.environ:
                            os.environ[key] = val
        except Exception:
            pass

_load_dotenv()

__version__ = "4.0.0"

__all__ = [
    "TradingEngine",
    "BinanceDataFeed",
    "EnhancedFuturesWallet",
    "RiskManager",
    "PlaybookA",
    "PlaybookB",
    "MarketRegimeAnalyzer",
    "OllamaAdvisor",
    "LLMAdvice",
    "TradeJournal",
    "BinanceWebSocketFeed",
    "WebSocketPositionManager",
    "WebSocketTradingEngine",
]

# Lazy imports to avoid sys.modules collision when running submodules as __main__
# (e.g. `python -m crypto_trader.engine`)
_lazy_map = {
    "TradingEngine": ("engine", "TradingEngine"),
    "BinanceDataFeed": ("data_feed", "BinanceDataFeed"),
    "EnhancedFuturesWallet": ("wallet", "EnhancedFuturesWallet"),
    "RiskManager": ("risk", "RiskManager"),
    "PlaybookA": ("playbooks", "PlaybookA"),
    "PlaybookB": ("playbooks", "PlaybookB"),
    "MarketRegimeAnalyzer": ("regime", "MarketRegimeAnalyzer"),
    "OllamaAdvisor": ("llm_advisor", "OllamaAdvisor"),
    "LLMAdvice": ("llm_advisor", "LLMAdvice"),
    "TradeJournal": ("journal", "TradeJournal"),
    "BinanceWebSocketFeed": ("ws_client", "BinanceWebSocketFeed"),
    "WebSocketPositionManager": ("ws_client", "WebSocketPositionManager"),
    "WebSocketTradingEngine": ("engine_ws", "WebSocketTradingEngine"),
}


def __getattr__(name: str):
    if name in _lazy_map:
        module_name, attr = _lazy_map[name]
        import importlib
        mod = importlib.import_module(f".{module_name}", package=__name__)
        return getattr(mod, attr)
    raise AttributeError(f"module 'crypto_trader' has no attribute {name!r}")

