-- Pair-trade basket strategy metrics (PostgreSQL schema: pair_trade)
--
-- Fresh apply (after DROP SCHEMA pair_trade CASCADE):
--   python scripts/init_mm_metrics_db.py
--   (or pair_trade only via apply_pair_trade_schema in db.py)
--
-- All columns live in CREATE TABLE definitions (no ALTER migrations).

CREATE SCHEMA IF NOT EXISTS pair_trade;

CREATE TABLE IF NOT EXISTS pair_trade.strategy (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    strategy_type   TEXT NOT NULL DEFAULT 'pair_trade_basket',
    config_file     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pair_trade.session (
    id              SERIAL PRIMARY KEY,
    strategy_id     INTEGER NOT NULL REFERENCES pair_trade.strategy(id),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    quote_currency  TEXT NOT NULL DEFAULT 'IRR',
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_session_strategy_started
    ON pair_trade.session (strategy_id, started_at DESC);

CREATE TABLE IF NOT EXISTS pair_trade.session_balance (
    id              SERIAL PRIMARY KEY,
    session_id      INTEGER NOT NULL REFERENCES pair_trade.session(id) ON DELETE CASCADE,
    exchange        TEXT NOT NULL,
    asset           TEXT NOT NULL,
    amount          NUMERIC(36, 18) NOT NULL,
    UNIQUE (session_id, exchange, asset)
);

CREATE TABLE IF NOT EXISTS pair_trade.balance_snapshot (
    ts              TIMESTAMPTZ NOT NULL,
    session_id      INTEGER NOT NULL REFERENCES pair_trade.session(id) ON DELETE CASCADE,
    exchange        TEXT NOT NULL,
    asset           TEXT NOT NULL,
    total           NUMERIC(36, 18) NOT NULL,
    available       NUMERIC(36, 18),
    PRIMARY KEY (session_id, ts, exchange, asset)
);

CREATE INDEX IF NOT EXISTS idx_balance_snapshot_ts
    ON pair_trade.balance_snapshot (session_id, ts DESC);

CREATE TABLE IF NOT EXISTS pair_trade.portfolio_snapshot (
    ts                  TIMESTAMPTZ NOT NULL,
    session_id          INTEGER NOT NULL REFERENCES pair_trade.session(id) ON DELETE CASCADE,
    portfolio_value     NUMERIC(36, 18) NOT NULL,
    pnl                 NUMERIC(36, 18),
    -- unused for long-only ETF baskets (prefer basket_snapshot.symbols + balance_snapshot)
    net_exposure        JSONB NOT NULL DEFAULT '{}'::jsonb,
    marks               JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (session_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_portfolio_snapshot_ts
    ON pair_trade.portfolio_snapshot (session_id, ts DESC);

CREATE TABLE IF NOT EXISTS pair_trade.signal_snapshot (
    -- One row per (session, freq bin, symbol). Same series used to build rotation edges.
    session_id      INTEGER NOT NULL REFERENCES pair_trade.session(id) ON DELETE CASCADE,
    bin_ts          TIMESTAMPTZ NOT NULL,  -- completed return-frequency bucket start
    symbol          TEXT NOT NULL,         -- strategy symbol (ETF code)
    ret             NUMERIC(36, 18),       -- log(mid) - EWM(log(mid)); mid return signal
    ba_spread       NUMERIC(36, 18),       -- mean((ask-bid)/mid) over the bin; relative BA cost
    mid_price       NUMERIC(36, 18),       -- resampled mid for the bin
    ret_ask         NUMERIC(36, 18),       -- log(best_ask) - EWM; buy-side signal at log time
    ret_bid         NUMERIC(36, 18),       -- log(best_bid) - EWM; sell-side signal at log time
    ask_price       NUMERIC(36, 18),       -- best ask at signal log time
    bid_price       NUMERIC(36, 18),       -- best bid at signal log time
    ask_volume      NUMERIC(36, 18),       -- best-ask size
    bid_volume      NUMERIC(36, 18),       -- best-bid size
    PRIMARY KEY (session_id, bin_ts, symbol)
);

CREATE INDEX IF NOT EXISTS idx_signal_snapshot_session_bin
    ON pair_trade.signal_snapshot (session_id, bin_ts DESC);

CREATE TABLE IF NOT EXISTS pair_trade.basket_snapshot (
    session_id           INTEGER NOT NULL REFERENCES pair_trade.session(id) ON DELETE CASCADE,
    ts                   TIMESTAMPTZ NOT NULL,
    symbols              JSONB NOT NULL DEFAULT '[]'::jsonb,
    quote_balance        NUMERIC(36, 18),
    -- legacy columns kept nullable; P&L charts use portfolio_snapshot only
    portfolio_value      NUMERIC(36, 18),
    pnl                  NUMERIC(36, 18),
    pending_order_count  INTEGER NOT NULL DEFAULT 0,
    lowest_ret_symbol    TEXT,
    PRIMARY KEY (session_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_basket_snapshot_session_ts
    ON pair_trade.basket_snapshot (session_id, ts DESC);

-- Sell→buy rotation (or cash_collector). prev_* = sell leg; new_* = buy leg.
-- expected_edge = transition_score = ret_prev - ret_new - ba_spread_prev - ba_spread_new
-- last_edge     = ret_bid[prev] - ret_ask[new] at order time (live book)
-- realized_edge = ret_prev - ret_new - log(buy_vwap/mid_new) + log(sell_vwap/mid_prev)
-- realized_edge_net = same with sell_vwap_net / buy_vwap_net (fees in quote)
CREATE TABLE IF NOT EXISTS pair_trade.rotation (
    id                  BIGSERIAL PRIMARY KEY,
    session_id          INTEGER NOT NULL REFERENCES pair_trade.session(id) ON DELETE CASCADE,
    ts                  TIMESTAMPTZ NOT NULL,   -- logical time when sell (rotation) started
    prev_symbol         TEXT NOT NULL,          -- symbol sold (s_prev)
    new_symbol          TEXT NOT NULL,          -- symbol bought (s_new)
    transition_score    NUMERIC(36, 18),        -- expected_edge (see header)
    transition_value    NUMERIC(36, 18),        -- max quote notional to rotate at decision
    sell_pair           TEXT,                   -- HB pair for sell, e.g. XXXX-IRR
    buy_pair            TEXT,                   -- HB pair for buy
    sell_order_id       TEXT,                   -- client sell id, or cash_collector:{buy_id}
    status              TEXT NOT NULL,          -- started|sell_complete|buy_partial|buy_complete|cash_collector|...
    quote_earned        NUMERIC(36, 18) NOT NULL DEFAULT 0,  -- accumulated sell quote notional
    quote_spent         NUMERIC(36, 18) NOT NULL DEFAULT 0,  -- accumulated buy quote notional
    last_edge           NUMERIC(36, 18),        -- execution-time edge from live ret_bid/ret_ask
    realized_edge       NUMERIC(36, 18),        -- edge using fill VWAPs vs signal mids (see header)
    intended_sell_price NUMERIC(36, 18),        -- limit sell price at send
    intended_buy_price  NUMERIC(36, 18),        -- limit buy price at send
    sell_vwap           NUMERIC(36, 18),        -- fill VWAP sell leg
    buy_vwap            NUMERIC(36, 18),        -- fill VWAP buy leg
    sell_vwap_net       NUMERIC(36, 18),        -- (sell_notional - sell_fees) / sell_qty
    buy_vwap_net        NUMERIC(36, 18),        -- (buy_notional + buy_fees) / buy_qty
    total_fee_quote     NUMERIC(36, 18),        -- sum fee_quote over rotation fills
    realized_edge_net   NUMERIC(36, 18),        -- realized_edge using *_vwap_net
    sell_slippage       NUMERIC(36, 18),        -- log(sell_vwap/intended_sell); neg = worse
    buy_slippage        NUMERIC(36, 18),        -- log(buy_vwap/intended_buy); pos = worse
    latency_sec         NUMERIC(36, 18),        -- seconds from start to buy_complete
    signal_mid_prev     NUMERIC(36, 18),        -- freq-bin mid for prev (= signal_snapshot.mid_price)
    signal_mid_new      NUMERIC(36, 18),        -- freq-bin mid for new
    ret_prev            NUMERIC(36, 18),        -- freq-bin ret for prev (= signal_snapshot.ret)
    ret_new             NUMERIC(36, 18),        -- freq-bin ret for new
    ba_spread_prev      NUMERIC(36, 18),        -- freq-bin ba_spread for prev (= signal_snapshot.ba_spread)
    ba_spread_new       NUMERIC(36, 18),        -- freq-bin ba_spread for new
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_rotation_session_sell_order
    ON pair_trade.rotation (session_id, sell_order_id)
    WHERE sell_order_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_rotation_session_ts
    ON pair_trade.rotation (session_id, ts DESC);

CREATE TABLE IF NOT EXISTS pair_trade.fill (
    id                  BIGSERIAL PRIMARY KEY,
    session_id          INTEGER NOT NULL REFERENCES pair_trade.session(id) ON DELETE CASCADE,
    ts                  TIMESTAMPTZ NOT NULL,
    rotation_id         BIGINT REFERENCES pair_trade.rotation(id),
    order_id            TEXT NOT NULL,
    trading_pair        TEXT NOT NULL,
    side                TEXT NOT NULL,
    amount              NUMERIC(36, 18) NOT NULL,
    price               NUMERIC(36, 18) NOT NULL,
    fee_quote           NUMERIC(36, 18) NOT NULL DEFAULT 0,
    exchange_trade_id   TEXT,
    fill_key            TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_fill_session_ts
    ON pair_trade.fill (session_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_fill_rotation
    ON pair_trade.fill (rotation_id);

CREATE TABLE IF NOT EXISTS pair_trade.event_log (
    id              BIGSERIAL PRIMARY KEY,
    session_id      INTEGER NOT NULL REFERENCES pair_trade.session(id) ON DELETE CASCADE,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    level           TEXT NOT NULL DEFAULT 'info',
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_event_log_session_ts
    ON pair_trade.event_log (session_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_event_log_type
    ON pair_trade.event_log (event_type, ts DESC);
