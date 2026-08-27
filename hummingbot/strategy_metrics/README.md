# Strategy metrics (PostgreSQL)

Performance logging for Hummingbot script strategies (cross-exchange market making and future strategies).

## What gets logged

| Table | Purpose |
|-------|---------|
| `mm_strategy` | One row per strategy name (`bitpin_mexc_mm`, future ETH strategy, etc.) |
| `mm_session` | One row per bot run |
| `mm_session_balance` | Starting balances per exchange/asset (P&L baseline) |
| `mm_balance_snapshot` | Balance time series per exchange/asset |
| `mm_portfolio_snapshot` | Total portfolio value + P&L in quote currency (USDT) |
| `mm_maker_fill` | Maker-leg fills |
| `mm_hedge_event` | Hedge attempts, fills, failures |
| `mm_round_trip` | Matched/partial/unhedged round trips + realized spread |
| `mm_event_log` | Generic events for future dashboards |

### Pair-trade schema (`pair_trade`)

All pair-trade metrics live in the PostgreSQL schema **`pair_trade`** (separate from `mm_*` market-making tables).

| Table | Purpose |
|-------|---------|
| `pair_trade.strategy` | One row per strategy name |
| `pair_trade.session` | One row per bot run |
| `pair_trade.session_balance` | Starting balances (P&L baseline) |
| `pair_trade.balance_snapshot` | Balance time series |
| `pair_trade.portfolio_snapshot` | Portfolio value + P&L in quote (IRR). Chart P&L here only. |
| `pair_trade.signal_snapshot` | Per-symbol ret / spread / mid every freq bin |
| `pair_trade.basket_snapshot` | Basket symbols, quote balance, pending orders (no P&L) |
| `pair_trade.rotation` | Sell→buy lifecycle; expected/last/realized(+net) edge, VWAP(+net), fees, slippage, latency; `ret_*` / `ba_spread_*` / `signal_mid_*` from same bin as signal_snapshot |
| `pair_trade.fill` | All fills linked to rotations when applicable |
| `pair_trade.event_log` | Health / ops events |

### Rotation column dictionary (`pair_trade.rotation`)

| Column | Meaning |
|--------|---------|
| `prev_symbol` / `new_symbol` | Sell / buy strategy symbols |
| `transition_score` | **expected_edge** = `ret_prev − ret_new − ba_spread_prev − ba_spread_new` |
| `transition_value` | Max quote notional to rotate at decision |
| `last_edge` | `ret_bid[prev] − ret_ask[new]` at order time (live book) |
| `realized_edge` | `ret_prev − ret_new − log(buy_vwap/mid_new) + log(sell_vwap/mid_prev)` after fills (pre-fee) |
| `realized_edge_net` | Same formula with `sell_vwap_net` / `buy_vwap_net` (post-fee) |
| `intended_sell_price` / `intended_buy_price` | Limit prices at send |
| `sell_vwap` / `buy_vwap` | Fill VWAPs (gross) |
| `sell_vwap_net` / `buy_vwap_net` | Fee-adjusted VWAPs (sell − fee, buy + fee) |
| `total_fee_quote` | Sum of rotation fill fees in quote |
| `sell_slippage` / `buy_slippage` | `log(vwap/intended)`; sell neg=worse, buy pos=worse |
| `latency_sec` | Seconds from rotation start → buy_complete |
| `signal_mid_prev` / `signal_mid_new` | Freq-bin mid (= `signal_snapshot.mid_price`) |
| `ret_prev` / `ret_new` | Freq-bin mid return (= `signal_snapshot.ret`) |
| `ba_spread_prev` / `ba_spread_new` | Freq-bin relative BA spread (= `signal_snapshot.ba_spread`), mean `(ask−bid)/mid` |
| `quote_earned` / `quote_spent` | Accumulated sell/buy quote notional |
| `status` | `started` / `sell_complete` / `buy_partial` / `buy_complete` / `cash_collector` / … |

### Signal snapshot columns (`pair_trade.signal_snapshot`)

| Column | Meaning |
|--------|---------|
| `ret` | `log(mid) − EWM(log(mid))` |
| `ba_spread` | Mean `(ask−bid)/mid` over the freq bin |
| `mid_price` | Resampled mid for the bin |
| `ret_bid` / `ret_ask` | Same EWM return evaluated at best bid / ask |

Use a unique `metrics_strategy_name` per bot instance. Grafana queries should prefix tables with `pair_trade.`:

```sql
SELECT ts, portfolio_value, pnl
FROM pair_trade.portfolio_snapshot
WHERE session_id = $session_id
ORDER BY ts;
```

## Step 1 — Install Postgres driver

Inside your Hummingbot conda env:

```bash
pip install psycopg[binary]
```

(`sqlalchemy` is already in Hummingbot dependencies.)

## Step 2 — Set database URL

Use your existing database named `hummingbot`:

```bash
export MM_METRICS_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@localhost:5432/hummingbot'
```

Add that line to your shell profile or a `.env` file you source before starting the bot.

## Step 3 — Create tables

From the repo root:

```bash
python scripts/init_mm_metrics_db.py
```

Or with psql directly:

```bash
psql "$MM_METRICS_DATABASE_URL" -f hummingbot/strategy_metrics/schema.sql
```

## Step 4 — Enable metrics in strategy config

In `conf/scripts/conf_bitpin_mexc_mm.yml` add (or keep defaults):

```yaml
metrics_enabled: true
metrics_strategy_name: bitpin_mexc_mm
metrics_snapshot_interval_sec: 60
```

## Step 5 — Start the strategy

```bash
# ensure MM_METRICS_DATABASE_URL is exported
start --script conf_bitpin_mexc_mm.yml
```

On start the bot will:

1. Register/update `mm_strategy`
2. Open a new `mm_session`
3. Snapshot starting balances into `mm_session_balance`
4. Every 60s write balance + portfolio snapshots
5. On maker fill / hedge placed / hedge fill / hedge failure → write metrics rows

## Step 6 — Verify data

```sql
SELECT * FROM mm_strategy;
SELECT id, started_at, quote_currency FROM mm_session ORDER BY id DESC LIMIT 5;

SELECT ts, portfolio_value, pnl
FROM mm_portfolio_snapshot
ORDER BY ts DESC LIMIT 10;

SELECT ts, side, amount, price_quote, status
FROM mm_round_trip
ORDER BY ts DESC LIMIT 20;

SELECT exchange, asset, total
FROM mm_balance_snapshot
WHERE ts = (SELECT MAX(ts) FROM mm_balance_snapshot);
```

## Extending to ETH or another exchange

Only config changes are needed — the schema is generic:

```yaml
maker_trading_pair: ETH-IRT
hedge_trading_pair: ETH-USDT
metrics_strategy_name: bitpin_mexc_eth_mm   # unique name per strategy instance
```

Use a different `metrics_strategy_name` when running multiple strategies so sessions stay separate in Grafana.

## Grafana (next step)

Point Grafana at the same PostgreSQL database. Useful starter queries:

**Portfolio P&L**

```sql
SELECT ts AS time, pnl AS "P&L USDT"
FROM mm_portfolio_snapshot
WHERE session_id = $session_id
ORDER BY ts;
```

**Balances by exchange**

```sql
SELECT ts AS time, total AS value, exchange || ' ' || asset AS metric
FROM mm_balance_snapshot
WHERE session_id = $session_id
ORDER BY ts;
```

**Cumulative realized spread**

```sql
SELECT ts AS time,
       SUM(net_pnl_quote) OVER (ORDER BY ts) AS "Realized spread USDT"
FROM mm_round_trip
WHERE session_id = $session_id AND status IN ('matched', 'partial')
ORDER BY ts;
```

**Hedge success rate**

```sql
SELECT status, COUNT(*)
FROM mm_hedge_event
WHERE session_id = $session_id
GROUP BY status;
```

## Files

| File | Role |
|------|------|
| `hummingbot/strategy_metrics/schema.sql` | Postgres DDL |
| `hummingbot/strategy_metrics/config.py` | Strategy-agnostic config |
| `hummingbot/strategy_metrics/db.py` | DB connection + schema apply |
| `hummingbot/strategy_metrics/repository.py` | SQL inserts |
| `hummingbot/strategy_metrics/tracker.py` | Background writer used by strategies |
| `scripts/init_mm_metrics_db.py` | One-time schema setup |
