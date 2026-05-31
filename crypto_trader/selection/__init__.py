"""crypto_trader.selection — offline per-symbol strategy champion selection.

A selector backtests every candidate strategy per symbol on an out-of-sample
slice, gates them (min trades / net PF / positive expectancy), and writes a
``strategy_champions.json`` the live bot consumes to auto-route each symbol to
its best strategy. Selection is OFFLINE and the map is reviewable — the live
loop never re-picks a strategy on noisy recent data.
"""
from .champions import load_champions, champion_for, CHAMPIONS_PATH  # noqa: F401
