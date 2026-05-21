# Binance USD-M Futures Trading System (SOL 10× Snap)

A suite of production-grade automated trading scripts for Binance USD-Margined Futures.
Implements the **"SOL 10× Snap"** strategy — from a basic technical engine (v1) through to a full AI-enhanced filtering system (v3).

---

## 📂 File Overview

| File | Role | Run Directly? |
|------|------|---------------|
| `binance_futures_trading_system.py` | v1 — Basic EMA/RSI paper trader | ✅ Yes |
| `binance_futures_trading_system_v2.py` | v2 — Multi-timeframe + Risk Manager | ✅ Yes |
| `binance_futures_trading_system_v3.py` | v3 — v2 + Ollama LLM filter | ✅ Yes |
| `ollama_advisor.py` | AI advisory module (used by v3) | ✅ Yes (standalone test) |
| `crypto_trader/` | v4 — Modular package (`engine`, `wallet`, `risk`, etc.) | ✅ Yes (`python -m crypto_trader.engine`) |

---

## 🛠️ Prerequisites

### Python dependencies

```bash
pip3 install pandas numpy requests
```

### Ollama (required for `v3` and `ollama_advisor.py`)

Ollama must be running **before** starting v3. Two options:

**Option A — Docker (recommended, already set up):**

```bash
# Already running as a container:
# ollama-server  0.0.0.0:11434->11434/tcp

# Verify it's up:
curl -s http://localhost:11434/api/tags
```

**Option B — Native install:**

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3.5:4b
ollama run qwen3.5:4b   # starts the API server on :11434
```

---

## 🔬 Running `ollama_advisor.py` (Standalone Test)

`ollama_advisor.py` is both a library (imported by v3) **and** a runnable diagnostic script.
When executed directly it:

1. Checks whether Ollama is reachable at `http://localhost:11434`
2. Generates synthetic OHLCV candle data
3. Sends it to the LLM and prints the parsed `LLMAdvice` response

```bash
# Make sure Ollama is running first, then:
python3 ollama_advisor.py
```

**Expected output (Ollama running, model warm):**

```
Ollama available: True

LLM Advice:
{
  "timestamp": 1779312704,
  "symbol": "TESTUSDT",
  "bias": "bullish",
  "confidence": 0.72,
  "sentiment_score": 0.45,
  "risk_level": "low",
  "key_factors": ["Consistent uptrend", "Volume confirms move"],
  "recommended_bias": "long_only",
  "technical_alignment": 0.6,
  "veto": false,
  "veto_reason": null
}
```

**Expected output (Ollama not running):**

```
Ollama available: False
Ollama not running. Start it with: ollama run qwen3.5:4b
```

### Environment variable overrides

You can point the advisor at a different host or model without editing the file:

```bash
OLLAMA_HOST=http://192.168.1.100:11434 python3 ollama_advisor.py   # remote server
OLLAMA_MODEL=qwen3.5:4b              python3 ollama_advisor.py   # better reasoning
OLLAMA_TIMEOUT=320                   python3 ollama_advisor.py   # slower hardware
```

> **Default timeout is 90 s** — covers the ~37 s cold-start of `qwen3.5:4b` plus full-prompt inference (~20 s).
> Subsequent warm calls take ~15 s. `qwen3.5:4b` is faster (~2 s warm) but produces shallower analysis.

### Available models on this server

| Model | Size | Cold start | Warm call | Quality | Notes |
|-------|------|-----------|-----------|---------|-------|
| `qwen3.5:4b` ⭐ | 3.4 GB | ~37s | ~15s | ★★★★★ | **Recommended** — cites actual price levels |
| `qwen3.5:4b` | 2.0 GB | ~34s | ~2s | ★★★☆☆ | Default fallback — fast but shallow analysis |
| `qwen3.5:4b` | 5.2 GB | ~55s | ~25s | ★★★★★ | Best quality, use `OLLAMA_TIMEOUT=120` |
| `llama3.1:8b` | 4.9 GB | ~50s | ~20s | ★★★★☆ | Good alternative to qwen3.5:4b |
| `qwen2.5:0.5b` | 0.4 GB | ~5s | <1s | ★★☆☆☆ | Fastest, weakest JSON reliability |

---

## 📈 Running the Trading Scripts

All three trading scripts share the same CLI pattern.
Use `--help` on any of them for the full argument list:

```bash
python3 binance_futures_trading_system_v2.py --help
```

### v1 — Basic EMA/RSI engine

```bash
# Single tick (demo)
python3 binance_futures_trading_system.py --symbol SOLUSDT

# Live loop (every 60 s)
python3 binance_futures_trading_system.py --symbol SOLUSDT --loop --tick 60

# 30-day backtest
python3 binance_futures_trading_system.py --symbol SOLUSDT --backtest-days 30

# Testnet
python3 binance_futures_trading_system.py --symbol SOLUSDT --testnet --loop
```

### v2 — SOL 10× Snap (multi-timeframe, risk-managed)

```bash
# Single tick
python3 binance_futures_trading_system_v2.py --symbol SOLUSDT

# Live loop (5-min ticks — aligns well with 1H candle close)
python3 binance_futures_trading_system_v2.py --symbol SOLUSDT --loop --tick 300

# 14-day backtest
python3 binance_futures_trading_system_v2.py --symbol SOLUSDT --backtest-days 14

# Different symbol (see parameter tuning section)
python3 binance_futures_trading_system_v2.py --symbol ETHUSDT --loop --tick 300
```

### v3 — AI-Enhanced (v2 + Ollama LLM filter)

Ollama **must be running** before launching v3.

```bash
# Verify Ollama is up first:
curl -s http://localhost:11434/api/tags | python3 -m json.tool

# Single tick with LLM (default: qwen3.5:4b at localhost:11434)
python3 binance_futures_trading_system_v3.py --symbol SOLUSDT

# Live loop with LLM
python3 binance_futures_trading_system_v3.py --symbol SOLUSDT --loop --tick 300

# Technical-only fallback (disable LLM — behaves like v2)
python3 binance_futures_trading_system_v3.py --symbol SOLUSDT --no-llm --loop

# Remote Ollama server
python3 binance_futures_trading_system_v3.py \
    --symbol SOLUSDT \
    --llm-host http://192.168.1.100:11434 \
    --llm-model qwen2.5:7b \
    --loop
```

### v4 — Modular package runner (`crypto_trader.engine`)

```bash
# Install package deps
pip3 install -r crypto_trader/requirements.txt

# Single tick
python3 -m crypto_trader.engine --symbol SOLUSDT

# Live loop
python3 -m crypto_trader.engine --symbol SOLUSDT --loop --tick 300

# No LLM mode
python3 -m crypto_trader.engine --symbol SOLUSDT --no-llm --loop

# v4 always runs with websocket ticker (bookTicker mid-price as live LTP)
python3 -m crypto_trader.engine --symbol SOLUSDT --no-llm --loop --tick 5
```

### Binance WebSocket data you can use for realtime entries/exits

For USDⓈ-M futures, useful public streams include:

- `@bookTicker`: best bid/ask updates (good for LTP proxy + spread checks)
- `@markPrice`: mark price and funding-related timing context
- `@aggTrade` / `@trade`: trade flow and micro momentum
- `@kline_1m` (or other intervals): live candle building

In this repo, `crypto_trader.websocket_feed.RealtimeTicker` wires `@bookTicker`
and exposes `last_bid`, `last_ask`, and `last_price`. `TradingEngine` starts
the websocket feed by default, keeps reconnecting on disconnect, and uses realtime
price for entries (falling back to REST mark price only until first tick arrives).

---

## ⚙️ Configuration & Tuning

All key parameters sit at the top of each script file:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `LEVERAGE` | `10` | Position leverage multiplier |
| `EQUITY_UTILIZATION` | `0.50` | Fraction of available balance per trade |
| `A_SL_PCT` | `0.007` | Playbook A stop-loss (0.7% price move) |
| `A_TP_PCT` | `0.010` | Playbook A take-profit (1.0% price move) |
| `B_SL_PCT` | `0.012` | Playbook B stop-loss (1.2% price move) |
| `MAX_DAILY_TRADES` | `2` | Max trades per UTC day |
| `MAX_CONSEC_LOSS` | `2` | Halt after N consecutive losses |
| `OLLAMA_MODEL` | `qwen3.5:4b` | LLM model (`qwen3.5:4b` recommended) |
| `OLLAMA_TIMEOUT` | `90` | LLM request timeout in seconds |
| `CACHE_TTL_SECONDS` | `1800` | LLM cache duration (30 min) |
| `LLM_MIN_CONFIDENCE` | `0.65` | Ignore LLM if confidence is below this |
| `LLM_VETO_THRESHOLD` | `-0.70` | Block trade if LLM sentiment score is below this |

### Tuning for different pairs

The default parameters are calibrated for **SOLUSDT** (avg 1H range ~0.74%).
For other pairs, adjust `A_SL_PCT` and `A_TP_PCT` based on the pair's ATR:

| Pair | Suggested `A_SL_PCT` | Suggested `A_TP_PCT` |
|------|----------------------|----------------------|
| SOLUSDT (low vol) | `0.004` | `0.006` |
| ETHUSDT, SOLUSDT | `0.007` | `0.010` |
| DOGEUSDT, SHIB (high vol) | `0.012` | `0.018` |

---

## 💾 State Persistence

v2/v3 now save state under `~/.crypto_trader/` (not the current working directory).
The v4 package uses the same base directory and adds a journal subfolder.

| File | Contents |
|------|----------|
| `~/.crypto_trader/wallet_{SYMBOL}_v2.json` | v2 wallet balance, open positions, trade history |
| `~/.crypto_trader/risk_manager_state.json` | v2 daily trade count, consecutive loss streak |
| `~/.crypto_trader/llm_cache/` | v3 cached LLM responses (30-min TTL) |
| `~/.crypto_trader/wallet_{SYMBOL}.json` | v4 wallet state |
| `~/.crypto_trader/risk_state.json` | v4 risk state |
| `~/.crypto_trader/journal/YYYY-MM-DD.jsonl` | v4 append-only trade journal |

To **reset** and start fresh (new wallet):

```bash
rm -f ~/.crypto_trader/wallet_SOLUSDT_v2.json ~/.crypto_trader/risk_manager_state.json
rm -f ~/.crypto_trader/wallet_SOLUSDT.json ~/.crypto_trader/risk_state.json
rm -rf ~/.crypto_trader/llm_cache ~/.crypto_trader/journal
```

---

## 🏗️ Architecture

```
binance_futures_trading_system_v3.py  (TradingEngineV3)
│
├── binance_futures_trading_system_v2.py  (TradingEngine, EnhancedFuturesWallet,
│       RiskManager, MarketRegimeAnalyzer, PlaybookA, PlaybookB, BinanceDataFeed)
│
└── ollama_advisor.py  (OllamaAdvisor)
        ├── OllamaClient      — HTTP calls to /api/generate
        ├── LLMResponseParser — JSON extraction & validation
        ├── LLMCache          — 30-min disk cache (llm_cache/)
        └── LLMAdvice         — Structured result: bias, confidence, veto_reason
```

**v4 modular architecture:**

```
crypto_trader/
├── data_feed.py     (Binance REST + 418/429 handling)
├── wallet.py        (position lifecycle + partial close accounting)
├── risk.py          (daily limits + LLM circuit breaker)
├── regime.py        (regime classification)
├── playbooks.py     (entry setup logic)
├── llm_advisor.py   (LLM advice schema + latency metadata)
├── journal.py       (JSONL journaling)
└── engine.py        (orchestrator + CLI)
```

**Signal flow in v3:**

```
Fetch 4H + 1H data
    → Regime analysis (4H)
    → Start async LLM call (non-blocking)
    → Update open positions (TP / SL / trail / time-stop)
    → Evaluate Playbook A or B signal
    → LLM filter (veto? reduce size? allow?)
    → Risk Manager check (daily cap / loss streak)
    → Open position (EnhancedFuturesWallet)
```

---

## ⚠️ Disclaimer

This software is for **educational and paper-trading purposes only**.

- Cryptocurrency futures trading involves significant risk of loss.
- 10× leverage is extremely aggressive.
- Always test on testnet or in simulation before deploying real capital.
- The authors are not responsible for any financial losses.

### v4 WebSocket hybrid engine

```bash
python3 -m crypto_trader.engine_ws --symbol SOLUSDT --loop --tick 300
python3 -m crypto_trader.engine_ws --symbol SOLUSDT --no-llm --loop
python3 -m crypto_trader.engine_ws --symbol SOLUSDT --testnet --loop
```
