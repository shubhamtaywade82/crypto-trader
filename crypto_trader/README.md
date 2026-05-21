# crypto_trader v4

Production-grade Binance USD-M Futures trading system with LLM-enhanced risk management.

## Architecture

```
crypto_trader/
├── __init__.py         # Package exports
├── data_feed.py        # Binance REST client (klines, funding, OI)
├── wallet.py           # Position tracking, PnL, partial closes
├── risk.py             # Daily limits, consecutive loss circuit breaker
├── playbooks.py        # Intraday Snap + Swing entry logic
├── regime.py           # Multi-timeframe trend classification (ADX + EMA)
├── llm_advisor.py      # Ollama integration (weighted fusion, not veto)
├── journal.py          # Append-only trade journal for analytics
└── engine.py           # Orchestrator
```

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start Ollama (optional, for LLM layer)
ollama pull qwen3.5:4b
ollama run qwen3.5:4b

# 3. Run
python -m crypto_trader.engine --symbol SOLUSDT --loop --tick 300

# 4. Without LLM (technical-only)
python -m crypto_trader.engine --symbol SOLUSDT --no-llm --loop
```

## Key Design Decisions

| Feature | Implementation |
|---|---|
| **LLM Role** | Advisory only — modifies confidence, never vetoes strong trends |
| **Weighted Fusion** | `final_score = tech_score * 0.8 + llm_confidence * 0.2` |
| **Trend Bypass** | LLM weight = 0 when `regime_score >= 0.85` |
| **Stale Advice** | Discarded if >20s old or inference >3s |
| **Circuit Breaker** | Auto-disables LLM after 5 failures |
| **Time Stops** | Use candle timestamps, not wall clock |
| **PnL Accounting** | Partials credited immediately; full close only adds remainder |
| **Journal** | JSONL per day with full context (regime, LLM, funding, OI) |

## Environment Variables

```bash
export OLLAMA_HOST="http://localhost:11434"
export OLLAMA_MODEL="qwen3.5:4b"
export OLLAMA_TIMEOUT="320"
export LLM_WEIGHT="0.20"
export LLM_MAX_LATENCY="3000"
export LLM_MAX_AGE="20"
```

## State Files

All state stored in `~/.crypto_trader/`:

- `wallet_{SYMBOL}.json` — positions and balance
- `risk_state.json` — daily counters
- `llm_cache/` — LLM response cache
- `journal/YYYY-MM-DD.jsonl` — trade history

## WebSocket Edition

For real-time position management and precise entry/exit execution:

```bash
python -m crypto_trader.engine_ws --symbol SOLUSDT --loop --tick 300
```

### WebSocket Streams Used

| Stream | Purpose | Frequency |
|---|---|---|
| `@markPrice@1s` | Mark price + funding rate | Every 1s |
| `@kline_1h` | 1H candle updates | Every hour |
| `@kline_4h` | 4H candle updates | Every 4 hours |
| `@bookTicker` | Best bid/ask (LTP) | Real-time |
| `@aggTrade` | Recent trades | Real-time |
| `@ticker` | 24h stats | Real-time |

### Architecture

```
REST (5-min tick)  →  Signal Generation  →  Regime  →  LLM  →  Entry Decision
WebSocket (1-sec)  →  Position Monitor   →  SL/TP/Trail/Catastrophic
```

**Entry execution**: Uses WebSocket mid-price (average of best bid/ask) for fairer fills.
**Exit monitoring**: Checks every 1 second against real-time LTP.
**Wick protection**: Uses mid-price for SL/TP to avoid getting hunted by single-tick wicks.
**Trailing stops**: Use LTP for faster reaction to momentum shifts.

### Slippage Tracking

Every entry logs:

- Intended price (from signal)
- Actual execution price (WebSocket mid)
- Spread at entry
- Slippage %
