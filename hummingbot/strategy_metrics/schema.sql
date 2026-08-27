-- Hummingbot strategy metrics schema (PostgreSQL)
-- Database: hummingbot (or any Postgres DB you choose)
--
-- Apply once:
--   psql "$MM_METRICS_DATABASE_URL" -f hummingbot/strategy_metrics/schema.sql
--
-- Designed for multiple strategies, exchanges, and base assets.

CREATE TABLE IF NOT EXISTS mm_strategy (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    strategy_type   TEXT NOT NULL DEFAULT 'cross_exchange_mm',
    config_file     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mm_session (
    id              SERIAL PRIMARY KEY,
    strategy_id     INTEGER NOT NULL REFERENCES mm_strategy(id),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    quote_currency  TEXT NOT NULL DEFAULT 'USDT',
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_mm_session_strategy_started
    ON mm_session (strategy_id, started_at DESC);

-- Baseline balances captured when a session starts.
CREATE TABLE IF NOT EXISTS mm_session_balance (
    id              SERIAL PRIMARY KEY,
    session_id      INTEGER NOT NULL REFERENCES mm_session(id) ON DELETE CASCADE,
    exchange        TEXT NOT NULL,
    asset           TEXT NOT NULL,
    amount          NUMERIC(36, 18) NOT NULL,
    UNIQUE (session_id, exchange, asset)
);

-- Raw balance rows per snapshot (normalized; works for any asset/exchange).
CREATE TABLE IF NOT EXISTS mm_balance_snapshot (
    ts              TIMESTAMPTZ NOT NULL,
    session_id      INTEGER NOT NULL REFERENCES mm_session(id) ON DELETE CASCADE,
    exchange        TEXT NOT NULL,
    asset           TEXT NOT NULL,
    total           NUMERIC(36, 18) NOT NULL,
    available       NUMERIC(36, 18),
    PRIMARY KEY (session_id, ts, exchange, asset)
);

CREATE INDEX IF NOT EXISTS idx_mm_balance_snapshot_ts
    ON mm_balance_snapshot (session_id, ts DESC);

-- Denormalized portfolio summary for fast Grafana queries.
CREATE TABLE IF NOT EXISTS mm_portfolio_snapshot (
    ts                  TIMESTAMPTZ NOT NULL,
    session_id          INTEGER NOT NULL REFERENCES mm_session(id) ON DELETE CASCADE,
    portfolio_value     NUMERIC(36, 18) NOT NULL,
    pnl                 NUMERIC(36, 18),
    net_exposure        JSONB NOT NULL DEFAULT '{}'::jsonb,
    marks               JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (session_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_mm_portfolio_snapshot_ts
    ON mm_portfolio_snapshot (session_id, ts DESC);

-- Maker-leg fills (Bitpin BTC-IRT today; any exchange/pair tomorrow).
CREATE TABLE IF NOT EXISTS mm_maker_fill (
    id                  BIGSERIAL PRIMARY KEY,
    session_id          INTEGER NOT NULL REFERENCES mm_session(id) ON DELETE CASCADE,
    ts                  TIMESTAMPTZ NOT NULL,
    exchange            TEXT NOT NULL,
    trading_pair        TEXT NOT NULL,
    base_asset          TEXT NOT NULL,
    quote_asset         TEXT NOT NULL,
    side                TEXT NOT NULL,
    amount              NUMERIC(36, 18) NOT NULL,
    price               NUMERIC(36, 18) NOT NULL,
    price_quote         NUMERIC(36, 18),
    fx_rate             NUMERIC(36, 18),
    fair_price          NUMERIC(36, 18),
    fee_quote           NUMERIC(36, 18) NOT NULL DEFAULT 0,
    exchange_trade_id   TEXT,
    order_id            TEXT,
    maker_fill_key      TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_mm_maker_fill_session_ts
    ON mm_maker_fill (session_id, ts DESC);

-- Hedge attempts and outcomes.
CREATE TABLE IF NOT EXISTS mm_hedge_event (
    id                  BIGSERIAL PRIMARY KEY,
    session_id          INTEGER NOT NULL REFERENCES mm_session(id) ON DELETE CASCADE,
    maker_fill_id       BIGINT REFERENCES mm_maker_fill(id),
    ts                  TIMESTAMPTZ NOT NULL,
    exchange            TEXT NOT NULL,
    trading_pair        TEXT NOT NULL,
    base_asset          TEXT NOT NULL,
    side                TEXT NOT NULL,
    requested_amount    NUMERIC(36, 18) NOT NULL,
    filled_amount       NUMERIC(36, 18) NOT NULL DEFAULT 0,
    limit_price         NUMERIC(36, 18),
    fill_price          NUMERIC(36, 18),
    order_id            TEXT,
    status              TEXT NOT NULL,
    fail_reason         TEXT,
    latency_ms          INTEGER,
    order_id_key        TEXT UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_mm_hedge_event_session_ts
    ON mm_hedge_event (session_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_mm_hedge_event_maker_fill
    ON mm_hedge_event (maker_fill_id);

-- Matched round trips (realized spread lives here).
CREATE TABLE IF NOT EXISTS mm_round_trip (
    id                  BIGSERIAL PRIMARY KEY,
    session_id          INTEGER NOT NULL REFERENCES mm_session(id) ON DELETE CASCADE,
    ts                  TIMESTAMPTZ NOT NULL,
    base_asset          TEXT NOT NULL,
    maker_fill_id       BIGINT REFERENCES mm_maker_fill(id),
    hedge_event_id      BIGINT REFERENCES mm_hedge_event(id),
    amount              NUMERIC(36, 18) NOT NULL,
    maker_price_quote   NUMERIC(36, 18) NOT NULL,
    hedge_price_quote   NUMERIC(36, 18) NOT NULL,
    spread_bps          NUMERIC(36, 18),
    gross_pnl_quote     NUMERIC(36, 18),
    fees_quote          NUMERIC(36, 18) NOT NULL DEFAULT 0,
    net_pnl_quote       NUMERIC(36, 18),
    status              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mm_round_trip_session_ts
    ON mm_round_trip (session_id, ts DESC);

-- Generic event log for future metrics / debugging.
CREATE TABLE IF NOT EXISTS mm_event_log (
    id              BIGSERIAL PRIMARY KEY,
    session_id      INTEGER NOT NULL REFERENCES mm_session(id) ON DELETE CASCADE,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    level           TEXT NOT NULL DEFAULT 'info',
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_mm_event_log_session_ts
    ON mm_event_log (session_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_mm_event_log_type
    ON mm_event_log (event_type, ts DESC);
