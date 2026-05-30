# Plan: Foundation — Risk-limit correctness + Trade-outcome substrate

**Goal:** Close the two highest-leverage go-live blockers from the audit:
1. Hard risk limits are wrong / not config-driven (daily-loss, lockout, halt, risk-per-trade).
2. No unified trade-outcome data substrate or computed metrics — blocks all learning, metrics, and go-live measurability.

**Risk values (user-confirmed = spec):** daily max loss **3%**, consecutive-loss lockout **3**, halt duration **24h (1440m)**, risk-per-trade **0.5%**, leverage **3x default / 5x hard cap**.

**Branch:** dedicated feature branch off current HEAD. **TDD throughout.** Each task: implement → test → commit → self-review → spec review → quality review.

---

## Task 1 — Make hard risk limits config-driven with spec defaults

**Problem (from audit):**
- `risk.py` `RiskManager.__init__` hardcodes `max_daily_drawdown_pct=0.05` (spec: 0.03) and `cooldown_after_loss_minutes=30` (spec: 1440). Neither is exposed in `TradingConfig` or passed at instantiation, so they can never be set.
- `config.py` default `risk_per_trade_pct=0.02` (spec: 0.005); default `max_consecutive_losses` resolves to 2 in the default profile (spec: 3).
- `multi_engine.py` (~line 147) constructs `RiskManager` without passing daily-drawdown / cooldown / consecutive-loss values.

**Requirements:**
1. Add `TradingConfig` fields (with `from_env` wiring + env var names) for: `max_daily_drawdown_pct` (default `0.03`, env `DAILY_MAX_LOSS_PCT`), `cooldown_after_loss_minutes` (default `1440`, env `LOSS_COOLDOWN_MINUTES`), `max_consecutive_losses` (default `3`, env `MAX_CONSECUTIVE_LOSSES`), `risk_per_trade_pct` (default `0.005`, env `RISK_PER_TRADE_PCT`). If a field already exists, correct its default and ensure env wiring.
2. Thread all four values from config into `RiskManager` at every instantiation site (`multi_engine.py`, and any engine/test factory). No hardcoded risk constant may remain as the effective live value.
3. Leverage: confirm default `3x` and hard cap `5x` (margin_engine `hard_max_leverage=5`). If the default profile's `max_leverage` is not 3, set it to 3. Do not weaken the hard cap.
4. Update `.env.example` documenting the four new env vars with spec defaults.
5. Do NOT change the named `conservative`/`aggressive` profiles' explicit risk appetites — only the default values used when no profile override is supplied.

**Tests (TDD — write first):**
- Daily-loss: with `max_daily_drawdown_pct=0.03`, a simulated realized daily loss ≥3% of initial daily balance makes `can_trade()` / the risk gate return blocked; <3% does not.
- Consecutive-loss: 3 consecutive stop-loss outcomes activates the lockout; 2 does not.
- Cooldown: after a loss, cooldown is reported active for the configured 1440-minute window (test with a small injected value to avoid real-time waits, plus assert the default is 1440).
- Risk-per-trade: position sizing uses 0.5% of equity for the risk budget by default.
- Env override: `from_env` picks up each new env var.

**Files:** `crypto_trader/config.py`, `crypto_trader/risk.py`, `crypto_trader/multi_engine.py`, `.env.example`, tests under `tests/`.

---

## Task 2 — Unified trade-outcome record + persistence

**Problem:** No single record captures a trade's *features + outcome* together. `journal.py` (`TradeJournalEntry`) is the closest seed but is partial and JSONL-only. Learning/metrics need one complete, queryable record per closed trade.

**Requirements:**
1. Define a `TradeOutcomeRecord` (dataclass or pydantic model) capturing:
   - **identity:** trade_id, symbol, side, opened_at, closed_at
   - **features (at entry):** regime, regime_score, funding_rate, vol_regime, oi_delta, session, entry_reason/setup_type, llm_action, llm_confidence, llm_rationale (short), and the **params used**: entry_band, stop_loss_pct, risk_per_trade_pct, leverage
   - **outcome (at exit):** entry_price, exit_price, quantity, realized_pnl, pnl_r (PnL in R = realized / initial risk), mfe, mae, holding_time_s, exit_reason, slippage_bps
2. Persist each completed record to a single append-only sink. Extend the existing `TradeJournal` (preferred — reuse its file/path handling) rather than inventing a parallel store. Provide a `load_all()` / iterator to read records back for aggregation.
3. Keep it provider/strategy-agnostic (any strategy can emit one).

**Tests (TDD):** construct a record, persist, read back, assert round-trip equality incl. all fields; assert append semantics (N writes → N records); assert missing optional fields tolerated.

**Files:** `crypto_trader/journal.py` (extend) or a new focused module if journal.py is already large (report as concern if so), tests.

---

## Task 3 — Capture features at open, outcome at close

**Problem:** Records are only useful if the trade lifecycle populates them. Wire capture into open/close.

**Requirements:**
1. At position open (engine_ws/wallet open path): capture the entry-feature set (regime, scores, funding, vol, oi, session, setup/entry reason, LLM action/confidence/rationale, params used) and stash on the position (or a pending-record map keyed by trade_id/symbol).
2. At position close: complete the record with outcome fields (realized_pnl, pnl_r, mfe, mae, holding_time_s, exit_reason, slippage) and write one `TradeOutcomeRecord` via Task 2's sink. Reuse existing MFE/MAE/holding-time tracking if present (close event already carries some of these per audit).
3. One complete record per closed trade. No double-writes. Must not break existing trade flow or events.

**Tests (TDD):** simulate an open→close cycle through the wallet/engine (use existing test harness/fakes) and assert exactly one complete `TradeOutcomeRecord` is written with correct feature + outcome values.

**Files:** `crypto_trader/engine_ws.py`, `crypto_trader/wallet.py`, tests. Follow existing event/patterns.

---

## Task 4 — Metrics aggregation

**Problem:** No computed win-rate / expectancy / drawdown. Audit: metrics not computed.

**Requirements:**
1. Pure aggregation module: input = iterable of `TradeOutcomeRecord`, output = metrics object with: total_trades, win_rate, avg_win_r, avg_loss_r, expectancy_r, profit_factor, max_drawdown (from cumulative realized PnL), avg_holding_time_s, and a per-regime breakdown (win_rate + expectancy_r per regime).
2. Pure/deterministic — no I/O, no globals. Reads records, returns numbers.

**Tests (TDD):** hand-crafted record sets with known outcomes → assert each metric exactly (incl. zero-trade edge case returning safe zeros, and profit_factor when no losses).

**Files:** new `crypto_trader/metrics.py` (focused), tests.

---

## Task 5 — Expose metrics + equity curve via API

**Problem:** Audit: dashboard missing `/api/metrics`, `/api/equity`.

**Requirements:**
1. `GET /api/metrics`: returns Task 4's aggregated metrics computed from the persisted trade-outcome records. Honor the existing API auth pattern (bearer token) used by other endpoints.
2. `GET /api/equity`: returns an equity curve — time series of cumulative realized PnL (or wallet balance) from wallet snapshots / outcome records, suitable for plotting.
3. Match the existing `api/app.py` + `api/repo.py` style; degrade gracefully (empty series / zeroed metrics) when no data.

**Tests:** API-client tests asserting both endpoints return correct shape + values for a seeded dataset, and respect auth.

**Files:** `crypto_trader/api/app.py`, `crypto_trader/api/repo.py`, tests.

---

## Sequencing / dependencies
- Task 1 independent.
- Task 2 → 3 → (4) → 5. (4 depends on 2; 5 depends on 4.)
- Execute sequentially in this session, fresh implementer per task, two-stage review each.

## Out of scope (later phases)
Persistence unification (Postgres-mandatory, single event journal), adaptive/auto-tuning calibration loop, provider-agnostic LLM adapters + per-provider breaker, multi-symbol portfolio engine, gap detection, G1 per-entry clock-skew gate.
