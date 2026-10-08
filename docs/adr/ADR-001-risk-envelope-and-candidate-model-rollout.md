# ADR-001: Risk Envelope and Candidate-Model Rollout for the AI Trading Kernel

- **Status:** Accepted
- **Date:** 2026-10-08
- **Deciders:** Repository owner (decisions confirmed interactively); implementation by agent session
- **Scope:** `crypto-trader` (Python, v4.0.0) execution and AI subsystems; standalone TypeScript reference pipeline
- **Related:** `docs/ROADMAP.md` (Phase 9.2B funnel gate), `AGENTS.md` leverage rule (prior-session repo), `crypto_trader/config/_settings.py`, `crypto_trader/margin_engine.py`, `crypto_trader/exchanges/instrument_mapper.py`, `crypto_trader/ai/router.py`, `crypto_trader/ai/validators/decision_schema.py`, `crypto_trader/ai/shadow.py`

---

## 1. Context and Scope

This record captures four decisions that govern how a small, locally served LLM (the MiniCPM-class candidate) is allowed to participate in a live crypto perpetual-futures pipeline, and how much leverage the system may use while that participation is still unproven. The decisions were confirmed by the repository owner on 2026-10-08 in response to a gap analysis of the prior session's TypeScript `crypto-agent` design. That prior design targeted a kernel with analyst/strategist/challenger roles and a `candidateId` hard gate; this repository implements the same concepts differently, so each decision had to be mapped onto the actual Python architecture before it could be implemented.

Three structural facts about this codebase shaped every decision. First, the LLM advisor participates through score fusion and veto only: `_decision_to_advice()` in `crypto_trader/llm_advisor.py` discards the model's `entry_zone`, `stop_loss`, and `targets`, and the technical playbooks own all price levels, so "the model cannot set levels" is already enforced structurally. Second, leverage is computed through a stack of clamps: the per-profile `max_leverage` (5/10/20), the dynamic band `[dynamic_leverage_min, dynamic_leverage_max]`, the venue instrument spec (clamped by `_MAX_USABLE_LEVERAGE`), and the `LeverageEngine.hard_max_leverage` tier backstop. Third, model output reaches trading only through `DecisionValidator`, which parses, schema-validates, and applies hard safety rules, with a JSON-repair heuristic for truncated output on the local-provider path.

The four decisions, as approved: (1) the system-wide leverage ceiling is 10x; (2) actionable model output that required JSON repair is rejected fail-closed (the codebase analogue of the prior session's "candidateId required" decision); (3) the candidate model enters shadow-only, evaluated beside the live decision path, with promotion gated on measured agreement and latency; and (4) model tags stay environment-driven — no tag is hardcoded anywhere, and the exact candidate tag is supplied by the operator at rollout time. All changes ship uncommitted per the agreed git policy, with the full test suite green (625 passed).

## 2. Decision 1 — System-Wide Leverage Ceiling of 10x

### 2.1 Context

The prior analysis found three conflicting leverage values across project documents: 2x (the `AGENTS.md` futures rule from the crypto-agent repo), 10x (the project overview), and 15x (a risk-config default). In this repository the conflict manifests differently: trading profiles default `max_leverage` to 5/10/20, the dynamic band tops out at `dynamic_leverage_max = 20`, `LeverageEngine.hard_max_leverage` defaulted to 20, and the venue-spec clamp `_MAX_USABLE_LEVERAGE` was 20. Because sizing multiplies through whichever value survives the `min()` chain, the effective ceiling was 20x on an aggressive profile with favourable regime conditions.

### 2.2 Decision

The system-wide hard ceiling is **10x**. It is enforced at three independent layers so that no single misconfiguration can lift the effective per-symbol leverage above it: `LeverageEngine.hard_max_leverage` defaults to 10 (overridable only via `RISK_HARD_MAX_LEVERAGE`, which is itself a documented risk decision), `dynamic_leverage_max` defaults to 10 in both the dataclass and `from_env()` (an explicit `DYNAMIC_LEVERAGE_MAX=20` raises the band but cannot raise the effective ceiling), and `instrument_mapper._MAX_USABLE_LEVERAGE` is lowered to 10 as the last line of defence clamping every venue instrument spec. The dynamic operating band becomes 5x–10x, preserving the existing volatility/drawdown/margin/regime scaling behaviour inside a narrower range.

### 2.3 Rationale

A 10x ceiling keeps liquidation distance compatible with the advisor's validated stop distances while an unproven 2B classifier sits in the loop. At 10x, a 10% adverse move wipes the margin; the safety engines (`min_liquidation_distance_pct`, `check_liquidation_distance`, isolated-margin requirement) were designed for exactly this order of magnitude, and the dynamic engine's "favourable conditions climb toward the ceiling" behaviour stays meaningful at 10 rather than collapsing to a constant. Choosing 10x rather than the more conservative 2x keeps the throughput profile the project overview documents, while the venue clamp guarantees that even an operator error in `.env` cannot reproduce the old 20x behaviour on the live path.

### 2.4 Consequences

Tests that pinned the 20x ceiling were updated rather than skipped: the band-math tests in `tests/test_dynamic_leverage.py` now assert a 5x–10x band with a 10x top, the instrument-spec clamp tests in `tests/test_coindcx_execution.py` assert clamping at 10, and `tests/test_risk_config_driven.py` asserts the new hard-cap default. A new defence-in-depth test (`test_venue_clamp_blocks_env_raised_band`) proves that raising `DYNAMIC_LEVERAGE_MAX` via env cannot lift effective per-symbol leverage past the instrument clamp. Operators who genuinely need a different ceiling must change `RISK_HARD_MAX_LEVERAGE` and re-run the suite with a written risk decision; the ADR reference comments in all three source files make that expectation visible at the point of change.

## 3. Decision 2 — Fail-Closed Truncation Gate (the candidateId analogue)

### 3.1 Context

The prior session found that `resolveCandidate` fell back to `setups[0]` when the strategist emitted `EXECUTE` without a `candidateId`, meaning a weak model could trade a ranking it never made. The approved fix was "schema makes candidateId required; omission = schema failure, no trade." In this repository the direct concept does not exist — there is no candidate list to reference. The structural equivalents were audited instead: every `LLMDecision` field is a required pydantic field (omission fails validation into the safe fallback), and `_decision_to_advice()` discards model price levels entirely, so no model-fabricated level can reach sizing.

### 3.2 The remaining gap

One genuine fail-open path remained: `DecisionValidator._repair_truncated()` reconstructs parseable JSON from truncated model output. A payload cut off mid-string could be silently closed and — if the surviving fields passed pydantic — produce an actionable decision whose numbers came out of a salvage heuristic rather than from the model. The repair exists because small local models hit `num_predict` mid-object, so removing it outright would discard the benign NO_TRADE case along with the dangerous one.

### 3.3 Decision

`DecisionValidator` now records whether a payload required truncation repair, and **actionable decisions (LONG/SHORT) that required repair are rejected fail-closed**; NO_TRADE still passes because it is the conservative direction. The gate is controlled by `LLM_STRICT_TRUNCATION` (default on) so the previous salvaging behaviour can be restored explicitly for debugging. Complete JSON wrapped in markdown fences is deliberately not treated as repair — fence stripping is formatting cleanup, not salvage.

### 3.4 Consequences

Truncation-driven rejections surface as `RiskGate: actionable decision required JSON repair` validation errors, which flow into the existing telemetry as failures and, in the router path, escalate to cloud or fall back to technical-only — the pre-existing fail-safe routes. Operators running small models with tight `num_predict` budgets will see more schema-failure events; the correct response is to raise `OLLAMA_NUM_PREDICT` or shorten the prompt, not to disable the gate in production. Eight new tests pin the gate's behaviour, including the exact "numbers present, reasoning truncated" case that motivated it.

## 4. Decision 3 — Shadow-First Rollout for Candidate Models

### 4.1 Context

The candidate strategist model (MiniCPM-class, ~2B parameters) is unproven as a forecaster, and `docs/ROADMAP.md` defers LLM ranking changes until the Phase 9.2B funnel analysis explains zero-trade activation. The owner nevertheless chose "shadow now": the candidate runs beside the live decision path from the start, but cannot influence trading until it earns promotion through measured performance. Shadow evaluation is the standard resolution of this tension — measurement without authority.

### 4.2 Decision

A new `crypto_trader/ai/shadow.py` module provides `ShadowEvaluator` and `ShadowMetrics`. When `SHADOW_OLLAMA_MODEL` is set, the router fires a fire-and-forget shadow call on a daemon thread after each *fresh* primary decision (cache hits skip it — no inference happened, so there is nothing to compare). The shadow call receives the identical prompts, is validated by the identical `DecisionValidator` a promoted candidate would face, and can never mutate the returned decision; every failure mode is swallowed and counted. Metrics recorded per evaluation: action-level agreement with the primary, schema-failure rate, call-failure rate, latency (p50/p95), and confidence. Raw records append to `~/.crypto_trader/llm_telemetry/shadow_YYYY-MM-DD.jsonl` with a `snapshot()` aggregate for dashboards.

### 4.3 Promotion gate

The candidate earns a role change only when, over a representative window agreed with the owner: action-agreement rate is at or above the agreed threshold, schema-failure rate is low enough that promotion would not multiply fallbacks, and shadow latency p95 fits inside the data-freshness budget *in addition to* the primary call. Beyond the online window, the candidate must beat the no-LLM and incumbent-LLM baselines on the existing replay tooling (`walk-forward`, `oos-evaluation`, `funnel-replay`) using expectancy in R with a bootstrap confidence interval — accuracy alone is not the metric. This mirrors the prior session's replay-gate requirement without blocking the measurement phase on Phase 9.2B.

### 4.4 Consequences

Shadow evaluation costs one extra local inference per fresh decision — acceptable on a local 2B model, and zero when the env var is unset, which is the default and the pre-rollout state. Because the shadow thread is daemonized and every exception path is counted rather than raised, the trading loop's worst case is unchanged: one log line. The `SHADOW_OLLAMA_HOSTS` variable exists because a candidate tag may only be pulled on some hosts; shadow hosts default to the local host list when unset.

## 5. Decision 4 — Model Tags Stay Environment-Driven

### 5.1 Decision

No model tag is hardcoded for any role. `build_router()` resolves each role from its own environment variable when the argument is omitted: `OLLAMA_MODEL` for the local triage role, `CLOUD_OLLAMA_MODEL` for the cloud conviction role, and `SHADOW_OLLAMA_MODEL` for the shadow role, with `SHADOW_OLLAMA_HOSTS` optional. Explicit arguments still win over env, and with every variable unset the router behaves exactly as before the change — this is what makes the diff safe to land uncommitted. The candidate's exact tag (the owner will paste it from `ollama list`) is therefore a rollout-time input, not a code constant.

### 5.2 Rationale and consequences

The prior session could not confirm that `minicpm5-2b` exists as a published tag, and guessing tags into source code is how unreachable-model bugs ship. Env-driven selection also means the same image runs triage-only today and shadow-augmented tomorrow without a rebuild. The cost is a startup-verification obligation: operators should confirm the tag with `ollama show <tag>` and a one-shot `/api/chat` probe before enabling the shadow role, and the router's pre-flight health check will warn if the local endpoint is unreachable. A `build_router()` call site that previously ignored env (`multi_engine.py`) now inherits the same variables, closing a silent divergence between the two router construction paths.

## 6. Implementation Record

All changes are uncommitted in the working tree per the git policy. The source changes are deliberately small and concentrated; every behavioural change has new or updated tests.

| File | Change | Purpose |
| --- | --- | --- |
| `crypto_trader/ai/router.py` | Env-resolved per-role models in `build_router()`; optional `shadow_evaluator` wiring; `_launch_shadow()` daemon-thread helper | Decisions 3, 4 |
| `crypto_trader/ai/shadow.py` | New module: `ShadowEvaluator`, `ShadowMetrics`, `ShadowResult`, JSONL audit log | Decision 3 |
| `crypto_trader/ai/validators/decision_schema.py` | `_parse_json` returns `(parsed, repaired)`; fail-closed truncation gate behind `LLM_STRICT_TRUNCATION` | Decision 2 |
| `crypto_trader/margin_engine.py` | `LeverageEngine` hard cap → `RISK_HARD_MAX_LEVERAGE` (default 10); `DynamicLeverageManager` fallbacks 20 → 10 | Decision 1 |
| `crypto_trader/config/_settings.py` | `dynamic_leverage_max` default 20 → 10 (dataclass and `from_env`) | Decision 1 |
| `crypto_trader/exchanges/instrument_mapper.py` | `_MAX_USABLE_LEVERAGE` 20 → 10 | Decision 1 |
| `tests/test_dynamic_leverage.py` | Band tests re-pinned to 5x–10x; new hard-cap, env-override, and venue-clamp tests | Decision 1 |
| `tests/test_coindcx_execution.py` | Spec clamp tests re-pinned to 10x | Decision 1 |
| `tests/test_risk_config_driven.py` | Hard-cap default test re-pinned to 10x | Decision 1 |
| `tests/test_ai_shadow.py` | New: 19 tests for evaluator metrics, router wiring, env role selection | Decisions 3, 4 |
| `tests/test_decision_strict_truncation.py` | New: 8 tests for the fail-closed gate | Decision 2 |

## 7. Verification and Operator Runbook

The full suite (excluding three files with pre-existing collection errors unrelated to this ADR — `test_api.py`, `test_ares_bot_toolkit.py`, `test_calibration.py`, all verified failing on the pristine checkout) passes: **625 passed, 0 failed**. Targeted runs: `python3 -m pytest tests/test_ai_shadow.py tests/test_decision_strict_truncation.py tests/test_dynamic_leverage.py tests/test_llm_routing.py -q` completes in about two seconds and covers every new behaviour.

Operator rollout sequence for the shadow phase: (1) confirm the candidate tag locally with `ollama list` and `ollama show <tag>`; (2) set `SHADOW_OLLAMA_MODEL=<tag>` (and `SHADOW_OLLAMA_HOSTS` if the tag is not on every host) in `.env`; (3) restart the engine and confirm the log line `Shadow evaluation enabled (model=<tag>)`; (4) let it run through at least one full session and inspect `~/.crypto_trader/llm_telemetry/shadow_<date>.jsonl` plus `ShadowMetrics.snapshot()` output; (5) bring the snapshot and replay results to the promotion review — do not promote on intuition. Rollback is a single env unset; no code path changes.

## 8. Risks and Open Questions

Three risks remain open by design. First, the three pre-existing test-collection errors were left untouched because fixing them is outside this ADR's scope; they should be tracked separately since they mask real coverage (`config_store.DATA_DIR` import breakage affects two files). Second, shadow metrics measure agreement with the incumbent, not correctness — a candidate that disagrees with a wrong incumbent looks bad and a sycophantic candidate looks good; the replay gate with expectancy-in-R exists precisely to counteract this, and it must not be waived at promotion time. Third, the roadmap interaction is unresolved: whether shadow-phase data counts toward the Phase 9.2B zero-trade analysis is the owner's call, and the safest reading is that it can only help, since shadow traffic does not change funnel behaviour. Finally, the kill-switch flatten path remains out of scope here; the venue posture for this change is paper-only, and no live execution path was modified.

## Appendix A — Standalone Reference Pipeline

A standalone TypeScript reference implementation (`standalone-advisor/`: advisor, FSM, risk gate, engine, 30 tests, strict-mode typecheck clean) accompanies this ADR. It encodes the same invariants in a venue-agnostic form: JSON-schema-constrained advisor output where malformed output maps to no-trade, sizing derived solely from equity × risk fraction ÷ stop distance (confidence never changes size), a total transition table where any unlisted event throws, and a staleness re-check after LLM latency. Its role is definitional, not operational: when this repository's Python pipeline and the reference disagree about what "fail-closed" or "the model cannot size the position" means, the reference is the tiebreaker to read against, and its per-invariant tests document the intent in executable form.
