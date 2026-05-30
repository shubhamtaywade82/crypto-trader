"""crypto_trader.backtest — historical replay of the live strategy logic.

Answers the only question that matters before risking capital: does the strategy
have positive expectancy after fees? Reuses the SAME PlaybookMeanReversion
entry/exit code the live bot runs, so backtest results reflect the real bot — no
re-implementation, no logic drift.
"""
