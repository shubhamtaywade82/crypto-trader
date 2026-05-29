-- ============================================================
-- crypto_trader  —  Ledger / Wallet / Position schema
-- PostgreSQL 14+
-- ============================================================
-- Run once:  psql $DATABASE_URL -f schema.sql
-- ============================================================

-- ── 0. Extensions ────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ── 1. Currency accounts ─────────────────────────────────────
-- One row per user × currency.  The ledger_balance is the
-- authoritative number derived by replaying ledger_entries.
CREATE TABLE IF NOT EXISTS currency_accounts (
    id               BIGSERIAL PRIMARY KEY,
    user_id          TEXT        NOT NULL DEFAULT 'default',
    currency         TEXT        NOT NULL,   -- 'INR', 'USDT', …
    ledger_balance   NUMERIC(28,8) NOT NULL DEFAULT 0,
    available_balance NUMERIC(28,8) NOT NULL DEFAULT 0,
    locked_balance   NUMERIC(28,8) NOT NULL DEFAULT 0,
    updated_at       BIGINT      NOT NULL
                       DEFAULT (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT,
    CONSTRAINT uq_account UNIQUE (user_id, currency)
);
CREATE INDEX IF NOT EXISTS idx_accounts_user ON currency_accounts (user_id);

-- ── 2. Double-entry ledger ────────────────────────────────────
-- Every rupee / USDT movement is a journal entry.
-- Invariant: SUM(credits) - SUM(debits) = ledger_balance.
CREATE TABLE IF NOT EXISTS ledger_entries (
    id               BIGSERIAL PRIMARY KEY,
    account_id       BIGINT      NOT NULL REFERENCES currency_accounts(id),
    currency         TEXT        NOT NULL,
    direction        TEXT        NOT NULL CHECK (direction IN ('debit','credit')),
    amount           NUMERIC(28,8) NOT NULL CHECK (amount > 0),
    running_balance  NUMERIC(28,8) NOT NULL,
    event_type       TEXT        NOT NULL,  -- 'DEPOSIT','WITHDRAWAL','CONVERSION_DEBIT',
                                            -- 'CONVERSION_CREDIT','MARGIN_RESERVE',
                                            -- 'MARGIN_RELEASE','FEE','FUNDING',
                                            -- 'REALIZED_PNL','RECON_ADJUST'
    reference_type   TEXT,                  -- 'order','position','conversion',…
    reference_id     TEXT,
    rate_snapshot_id BIGINT,
    metadata         JSONB       NOT NULL DEFAULT '{}',
    created_at       BIGINT      NOT NULL
                       DEFAULT (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT
);
CREATE INDEX IF NOT EXISTS idx_ledger_account ON ledger_entries (account_id);
CREATE INDEX IF NOT EXISTS idx_ledger_event   ON ledger_entries (event_type);
CREATE INDEX IF NOT EXISTS idx_ledger_ref     ON ledger_entries (reference_type, reference_id);
CREATE INDEX IF NOT EXISTS idx_ledger_ts      ON ledger_entries (created_at);

-- ── 3. FX rate snapshots ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS fx_rates (
    id            BIGSERIAL PRIMARY KEY,
    from_currency TEXT         NOT NULL,
    to_currency   TEXT         NOT NULL,
    bid           NUMERIC(28,8) NOT NULL,
    ask           NUMERIC(28,8) NOT NULL,
    mid           NUMERIC(28,8) NOT NULL,
    source        TEXT         NOT NULL,
    captured_at   BIGINT       NOT NULL
                    DEFAULT (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT
);
CREATE INDEX IF NOT EXISTS idx_fx_pair ON fx_rates (from_currency, to_currency, captured_at DESC);

-- ── 4. Wallet snapshots ───────────────────────────────────────
-- Point-in-time projection of the trading wallet per venue.
CREATE TABLE IF NOT EXISTS wallet_snapshots_v2 (
    id                  BIGSERIAL PRIMARY KEY,
    venue               TEXT         NOT NULL,
    account_id          TEXT         NOT NULL DEFAULT 'default',
    settlement_currency TEXT         NOT NULL DEFAULT 'USDT',
    wallet_balance      NUMERIC(28,8) NOT NULL DEFAULT 0,
    margin_balance      NUMERIC(28,8) NOT NULL DEFAULT 0,
    available_balance   NUMERIC(28,8) NOT NULL DEFAULT 0,
    locked_balance      NUMERIC(28,8) NOT NULL DEFAULT 0,
    unrealized_pnl      NUMERIC(28,8) NOT NULL DEFAULT 0,
    realized_pnl        NUMERIC(28,8) NOT NULL DEFAULT 0,
    funding_accrued     NUMERIC(28,8) NOT NULL DEFAULT 0,
    fees_accrued        NUMERIC(28,8) NOT NULL DEFAULT 0,
    snapshot_at         BIGINT        NOT NULL
                          DEFAULT (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT
);
CREATE INDEX IF NOT EXISTS idx_wsv2_venue ON wallet_snapshots_v2 (venue, snapshot_at DESC);

-- ── 5. Positions ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS positions_v2 (
    id                  BIGSERIAL   PRIMARY KEY,
    position_uid        TEXT        NOT NULL UNIQUE DEFAULT gen_random_uuid()::TEXT,
    user_id             TEXT        NOT NULL DEFAULT 'default',
    venue               TEXT        NOT NULL,
    symbol              TEXT        NOT NULL,
    side                TEXT        NOT NULL CHECK (side IN ('LONG','SHORT')),
    qty                 NUMERIC(28,8) NOT NULL,
    entry_price_avg     NUMERIC(28,8) NOT NULL,
    mark_price          NUMERIC(28,8),
    notional            NUMERIC(28,8),
    leverage            INT         NOT NULL DEFAULT 1,
    initial_margin      NUMERIC(28,8),
    maintenance_margin  NUMERIC(28,8),
    margin_used         NUMERIC(28,8),
    liquidation_price   NUMERIC(28,8),
    unrealized_pnl      NUMERIC(28,8) NOT NULL DEFAULT 0,
    realized_pnl        NUMERIC(28,8) NOT NULL DEFAULT 0,
    unrealized_pnl_pct  NUMERIC(14,6) NOT NULL DEFAULT 0,
    funding_accrued     NUMERIC(28,8) NOT NULL DEFAULT 0,
    fees_accrued        NUMERIC(28,8) NOT NULL DEFAULT 0,
    tp_price            NUMERIC(28,8),
    sl_price            NUMERIC(28,8),
    status              TEXT        NOT NULL DEFAULT 'open'
                          CHECK (status IN ('open','closing','closed')),
    playbook            TEXT,
    strategy            TEXT,
    opened_at           BIGINT      NOT NULL,
    closed_at           BIGINT,
    updated_at          BIGINT      NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pos_user_venue  ON positions_v2 (user_id, venue);
CREATE INDEX IF NOT EXISTS idx_pos_symbol      ON positions_v2 (symbol, status);
CREATE INDEX IF NOT EXISTS idx_pos_uid         ON positions_v2 (position_uid);

-- ── 6. Orders ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orders_v2 (
    id                BIGSERIAL   PRIMARY KEY,
    client_order_id   TEXT        NOT NULL UNIQUE,
    exchange_order_id TEXT,
    venue             TEXT        NOT NULL,
    symbol            TEXT        NOT NULL,
    side              TEXT        NOT NULL CHECK (side IN ('BUY','SELL')),
    order_type        TEXT        NOT NULL,   -- MARKET LIMIT STOP_MARKET TAKE_PROFIT
    intent            TEXT        NOT NULL DEFAULT 'entry',  -- entry sl tp reduce exit
    qty               NUMERIC(28,8) NOT NULL,
    price             NUMERIC(28,8),
    stop_price        NUMERIC(28,8),
    reduce_only       BOOLEAN     NOT NULL DEFAULT FALSE,
    post_only         BOOLEAN     NOT NULL DEFAULT FALSE,
    time_in_force     TEXT                 DEFAULT 'GTC',
    status            TEXT        NOT NULL DEFAULT 'pending',
    filled_qty        NUMERIC(28,8) NOT NULL DEFAULT 0,
    avg_fill_price    NUMERIC(28,8),
    remaining_qty     NUMERIC(28,8),
    fee               NUMERIC(28,8) NOT NULL DEFAULT 0,
    fee_currency      TEXT                 DEFAULT 'USDT',
    position_id       BIGINT      REFERENCES positions_v2(id),
    created_at        BIGINT      NOT NULL,
    updated_at        BIGINT      NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ord_symbol  ON orders_v2 (symbol, status);
CREATE INDEX IF NOT EXISTS idx_ord_coid    ON orders_v2 (client_order_id);
CREATE INDEX IF NOT EXISTS idx_ord_xoid    ON orders_v2 (exchange_order_id);
CREATE INDEX IF NOT EXISTS idx_ord_pos     ON orders_v2 (position_id);

-- ── 7. Fills (immutable) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS fills_v2 (
    id                BIGSERIAL   PRIMARY KEY,
    order_id          BIGINT      NOT NULL REFERENCES orders_v2(id),
    exchange_trade_id TEXT,
    venue             TEXT        NOT NULL,
    symbol            TEXT        NOT NULL,
    side              TEXT        NOT NULL,
    qty               NUMERIC(28,8) NOT NULL,
    price             NUMERIC(28,8) NOT NULL,
    fee               NUMERIC(28,8) NOT NULL DEFAULT 0,
    fee_currency      TEXT                   DEFAULT 'USDT',
    is_maker          BOOLEAN     NOT NULL DEFAULT FALSE,
    position_id       BIGINT      REFERENCES positions_v2(id),
    created_at        BIGINT      NOT NULL,
    CONSTRAINT uq_fill UNIQUE (order_id, exchange_trade_id)
);
CREATE INDEX IF NOT EXISTS idx_fill_order ON fills_v2 (order_id);
CREATE INDEX IF NOT EXISTS idx_fill_pos   ON fills_v2 (position_id);
CREATE INDEX IF NOT EXISTS idx_fill_ts    ON fills_v2 (created_at);

-- ── 8. Reconciliation log ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS reconciliation_logs (
    id             BIGSERIAL PRIMARY KEY,
    venue          TEXT    NOT NULL,
    check_type     TEXT    NOT NULL,  -- balance position order
    status         TEXT    NOT NULL,  -- ok drift fixed error
    local_value    JSONB,
    exchange_value JSONB,
    delta          JSONB,
    action_taken   TEXT,
    created_at     BIGINT  NOT NULL
                     DEFAULT (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT
);
CREATE INDEX IF NOT EXISTS idx_recon_venue ON reconciliation_logs (venue, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_recon_status ON reconciliation_logs (status);
