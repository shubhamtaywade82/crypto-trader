"""
crypto_trader.execution.adapters — Mode-specific execution adapters.

The entire trading stack above this layer (Signal → Risk → Portfolio →
Wallet) is identical for all modes.  Only the adapter changes:

    PAPER   → PaperExecutor   (local DB, simulated fills)
    SANDBOX → SandboxExecutor (exchange testnet, real API, fake money)
    LIVE    → LiveExecutor    (real exchange, real money)

Use ``build_executor`` from ``factory`` to get the right one.
"""
from .base import ExecutorProtocol
from .paper import PaperExecutor
from .sandbox import SandboxExecutor
from .live import LiveExecutor
from .factory import build_executor

__all__ = [
    "ExecutorProtocol",
    "PaperExecutor",
    "SandboxExecutor",
    "LiveExecutor",
    "build_executor",
]
