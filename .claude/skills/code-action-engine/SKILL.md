---
name: code-action-engine
description: Execute Python code in a sandboxed IPython environment for testing algorithms, running backtests, and validating technical analysis indicators. Use when validating logic before deploying to the trading system.
---

# Code Action Engine

## Instructions
1. **Validation**: Test trading logic (RSI, SMA, Supertrend, BB) using pandas/numpy mock data first.
2. **IPython Sandbox**: Run scripts in a clean environment to ensure no side effects.
3. **Mocking**: Mock exchange calls to test the risk manager rules (like consecutive losses or daily trade caps) safely.
