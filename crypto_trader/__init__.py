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

# ── Load Environment Variables (.env → config, .env.secrets → secrets) ──
#
# Loading order and override rules:
#   1. .env          — Non-sensitive configuration. Loaded first; does NOT
#                      overwrite variables already set in the OS environment.
#   2. .env.secrets  — API keys, tokens, passwords, and live-trading gates.
#                      Loaded second and ALWAYS WINS (overrides .env values
#                      and OS env). This lets secrets stay out of git while
#                      still taking precedence at runtime.
#
# Both files are searched in CWD first, then the project root (parent of
# this package directory), mirroring normal dotenv lookup behaviour.
def _parse_env_file(path, override: bool) -> None:
    """Parse a .env-style file and set variables into os.environ.

    Args:
        path: Path object pointing to the env file.
        override: When True, the file's values overwrite any existing env var.
                  When False, existing env vars are preserved (no-overwrite).
    """
    import os

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                # Strip surrounding quotes
                if len(val) >= 2 and (
                    (val.startswith('"') and val.endswith('"'))
                    or (val.startswith("'") and val.endswith("'"))
                ):
                    val = val[1:-1]
                if override or key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


def _find_env_file(filename: str):
    """Locate an env file in CWD or the project root (package parent dir)."""
    from pathlib import Path

    candidates = [
        Path.cwd() / filename,
        Path(__file__).resolve().parent.parent / filename,
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def _load_dotenv() -> None:
    """Load .env (config) then .env.secrets (secrets, highest priority)."""
    # Step 1 — load .env; do NOT override OS env or later secrets
    config_path = _find_env_file(".env")
    if config_path:
        _parse_env_file(config_path, override=False)

    # Step 2 — load .env.secrets; ALWAYS overrides .env values
    secrets_path = _find_env_file(".env.secrets")
    if secrets_path:
        _parse_env_file(secrets_path, override=True)


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

