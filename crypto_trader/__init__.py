"""crypto_trader package.

Submodules are imported lazily to avoid sys.modules collisions when running
individual modules as __main__ (e.g. ``python -m crypto_trader.engine``).
"""
from __future__ import annotations

__all__ = ["TradingEngine", "TradingEngineWS", "RealtimeTicker", "BinanceFuturesWebSocket"]


def __getattr__(name: str):
    if name == "TradingEngine":
        from .engine import TradingEngine
        return TradingEngine
    if name == "TradingEngineWS":
        from .engine_ws import TradingEngineWS
        return TradingEngineWS
    if name == "RealtimeTicker":
        from .websocket_feed import RealtimeTicker
        return RealtimeTicker
    if name == "BinanceFuturesWebSocket":
        from .websocket import BinanceFuturesWebSocket
        return BinanceFuturesWebSocket
    raise AttributeError(f"module 'crypto_trader' has no attribute {name!r}")
