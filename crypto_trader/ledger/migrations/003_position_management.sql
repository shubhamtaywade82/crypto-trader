-- ============================================================
-- Migration 003: AI Position Management System
-- ============================================================
-- Adds tables for:
--   1. positions_managed     — canonical managed position registry
--   2. position_snapshots    — time-series snapshots per position
--   3. position_actions      — every action taken or proposed
--   4. portfolio_snapshots   — portfolio-level state over time
--   5. ai_assessments        — full AI request/response audit
--   6. position_management_audit — condensed audit per cycle
-- ============================================================

-- ── 1. Managed positions ─────────────────────────────────────
-- One row per open (or recently closed) position, regardless of source.
CREATE TABLE IF NOT EXISTS positions_managed (
    id                   BIGSERIAL PRIMARY KEY,
    exchange_position_id TEXT          NOT NULL,
    local_id             TEXT          NOT NULL UNIQUE,
    symbol               TEXT          NOT NULL,
    side                 TEXT          NOT NULL CHECK (side IN ('LONG','SHORT')),
    quantity             NUMERIC(28,8) NOT NULL,
    entry_price          NUMERIC(28,8) NOT NULL,
    leverage             INTEGER       NOT NULL DEFAULT 1,
    source               TEXT          NOT NULL DEFAULT 'manual'
                           CHECK (source IN ('bot','manual','imported')),
    sync_state           TEXT          NOT NULL DEFAULT 'synced'
                           CHECK (sync_state IN (
                             'new','synced','protected','actively_managed',
                             'reducing','exiting','closed','stale','reconcile_required'
                           )),
    sl_price             NUMERIC(28,8),
    tp_price             NUMERIC(28,8),
    trailing_mode        TEXT,                -- 'atr' | 'structure' | 'vwap' | null
    trailing_active      BOOLEAN       NOT NULL DEFAULT FALSE,
    margin_used          NUMERIC(28,8),
    unrealized_pnl       NUMERIC(28,8),
    realized_pnl         NUMERIC(28,8) NOT NULL DEFAULT 0,
    opened_externally    BOOLEAN       NOT NULL DEFAULT FALSE,
    opened_at            BIGINT,
    closed_at            BIGINT,
    last_synced_at       BIGINT        NOT NULL
                           DEFAULT (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT,
    metadata             JSONB         NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_pm_exchange_id ON positions_managed (exchange_position_id);
CREATE INDEX IF NOT EXISTS idx_pm_symbol      ON positions_managed (symbol, sync_state);
CREATE INDEX IF NOT EXISTS idx_pm_source      ON positions_managed (source);
CREATE INDEX IF NOT EXISTS idx_pm_state       ON positions_managed (sync_state);


-- ── 2. Position snapshots ────────────────────────────────────
-- Time-series LTP / PnL / market context per position.
CREATE TABLE IF NOT EXISTS position_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    position_id     BIGINT        NOT NULL REFERENCES positions_managed(id) ON DELETE CASCADE,
    ltp             NUMERIC(28,8) NOT NULL,
    unrealized_pnl  NUMERIC(28,8),
    margin_used     NUMERIC(28,8),
    r_multiple      NUMERIC(10,4),
    wallet_balance  NUMERIC(28,8),
    market_regime   TEXT,
    market_bias     TEXT,
    risk_score      NUMERIC(6,2),
    trend_direction TEXT,
    volatility_state TEXT,
    momentum_state  TEXT,
    atr_pct         NUMERIC(10,6),
    vwap_dist_pct   NUMERIC(10,6),
    snapshot_at     BIGINT        NOT NULL
                      DEFAULT (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
);

CREATE INDEX IF NOT EXISTS idx_ps_position ON position_snapshots (position_id, snapshot_at DESC);
CREATE INDEX IF NOT EXISTS idx_ps_time     ON position_snapshots (snapshot_at DESC);


-- ── 3. Position actions ──────────────────────────────────────
-- Every action proposed, approved/rejected, and its outcome.
CREATE TABLE IF NOT EXISTS position_actions (
    id                 BIGSERIAL PRIMARY KEY,
    position_id        BIGINT        NOT NULL REFERENCES positions_managed(id) ON DELETE CASCADE,
    cycle_id           TEXT          NOT NULL,
    trigger            TEXT          NOT NULL,  -- 'tick'|'candle'|'fill'|'shock'|'scheduled'
    action_type        TEXT          NOT NULL,  -- PositionActionType value
    original_action    TEXT,                    -- before policy override
    approved           BOOLEAN       NOT NULL,
    confidence         NUMERIC(5,4),
    score              NUMERIC(6,2),
    reason             TEXT,
    rejection_reason   TEXT,
    hard_rule_triggered TEXT,
    requested_payload  JSONB         NOT NULL DEFAULT '{}',
    exchange_response  JSONB,
    execution_outcome  TEXT,
    idempotency_key    TEXT          UNIQUE,
    created_at         BIGINT        NOT NULL
                         DEFAULT (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
);

CREATE INDEX IF NOT EXISTS idx_pa_position   ON position_actions (position_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pa_action     ON position_actions (action_type);
CREATE INDEX IF NOT EXISTS idx_pa_approved   ON position_actions (approved);


-- ── 4. Portfolio snapshots ───────────────────────────────────
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id                BIGSERIAL PRIMARY KEY,
    equity            NUMERIC(28,8) NOT NULL,
    free_balance      NUMERIC(28,8) NOT NULL,
    blocked_capital   NUMERIC(28,8) NOT NULL,
    margin_used       NUMERIC(28,8) NOT NULL,
    unrealized_pnl    NUMERIC(28,8),
    total_open_risk   NUMERIC(28,8),
    open_positions    INTEGER,
    correlation_risk  TEXT,
    drawdown_pct      NUMERIC(8,4),
    snapshot_at       BIGINT        NOT NULL
                        DEFAULT (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
);

CREATE INDEX IF NOT EXISTS idx_portfolio_ts ON portfolio_snapshots (snapshot_at DESC);


-- ── 5. AI assessments ────────────────────────────────────────
-- Full prompt + response for every LLM call.
CREATE TABLE IF NOT EXISTS ai_assessments (
    id            BIGSERIAL PRIMARY KEY,
    cycle_id      TEXT      NOT NULL,
    position_id   TEXT      NOT NULL,  -- local_id reference
    symbol        TEXT      NOT NULL,
    prompt_hash   TEXT,                -- SHA-256 of system+user prompts
    system_prompt TEXT,
    user_prompt   TEXT,
    raw_response  TEXT,
    decision      JSONB     NOT NULL DEFAULT '{}',
    confidence    NUMERIC(5,4),
    action        TEXT,
    provider      TEXT,                -- 'local' | 'cloud_escalated' | 'cloud' | 'deterministic'
    latency_ms    INTEGER,
    created_at    BIGINT    NOT NULL
                    DEFAULT (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
);

CREATE INDEX IF NOT EXISTS idx_ai_cycle    ON ai_assessments (cycle_id);
CREATE INDEX IF NOT EXISTS idx_ai_position ON ai_assessments (position_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_symbol   ON ai_assessments (symbol, created_at DESC);


-- ── 6. Position management audit (condensed) ─────────────────
-- Quick-access summary per evaluation cycle.
CREATE TABLE IF NOT EXISTS position_management_audit (
    id                BIGSERIAL PRIMARY KEY,
    cycle_id          TEXT       NOT NULL,
    position_id       TEXT       NOT NULL,
    symbol            TEXT       NOT NULL,
    trigger           TEXT       NOT NULL,
    action            TEXT       NOT NULL,
    ai_action         TEXT       NOT NULL,
    approved          BOOLEAN    NOT NULL,
    confidence        NUMERIC(5,4),
    score             NUMERIC(6,2),
    reason            TEXT,
    rejection_reason  TEXT,
    execution_outcome TEXT,
    latency_ms        INTEGER,
    full_record       JSONB      NOT NULL DEFAULT '{}',
    created_at        BIGINT     NOT NULL
                        DEFAULT (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
);

CREATE INDEX IF NOT EXISTS idx_pma_position ON position_management_audit (position_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pma_symbol   ON position_management_audit (symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pma_action   ON position_management_audit (action);
CREATE INDEX IF NOT EXISTS idx_pma_cycle    ON position_management_audit (cycle_id);
