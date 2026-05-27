"""
crypto_trader.strategies — Strategy implementations
"""
from .supertrend2 import PlaybookSupertrend2, compute_supertrend2, adaptive_atr_length

__all__ = ["PlaybookSupertrend2", "compute_supertrend2", "adaptive_atr_length"]
