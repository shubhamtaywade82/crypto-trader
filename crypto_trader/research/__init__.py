"""
crypto_trader.research — Capital-aware strategy research & ranking engine
========================================================================
Fetches live Binance data, runs all 7 strategies through backtest engines,
simulates ₹10k capital at 10x leverage, and ranks strategies by
expectancy, profit factor, and win rate.
"""
from .engine import ResearchEngine, CapitalSimulator, RankedStrategy

__all__ = ["ResearchEngine", "CapitalSimulator", "RankedStrategy"]
