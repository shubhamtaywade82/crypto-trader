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
    "BinanceWebSocketFeed": ("websocket", "BinanceWebSocketFeed"),
    "WebSocketPositionManager": ("websocket", "WebSocketPositionManager"),
    "WebSocketTradingEngine": ("engine_ws", "WebSocketTradingEngine"),
}


def __getattr__(name: str):
    if name in _lazy_map:
        module_name, attr = _lazy_map[name]
        import importlib
        mod = importlib.import_module(f".{module_name}", package=__name__)
        return getattr(mod, attr)
    raise AttributeError(f"module 'crypto_trader' has no attribute {name!r}")

