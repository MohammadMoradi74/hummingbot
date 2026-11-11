#!/usr/bin/env python3
"""Generate hummingbot-pair-trade-mobin-v2.json Grafana dashboard."""
import json
from pathlib import Path

DS = "cfs3bqgnrvawwa"
GROUP = "grafana-postgresql-datasource"
VER = "13.1.0"
OUT = Path(__file__).with_name("hummingbot-pair-trade-mobin-v2.json")


def pq(raw_sql, fmt="time_series", ref="A"):
    return {
        "kind": "PanelQuery",
        "spec": {
            "refId": ref,
            "hidden": False,
            "query": {
                "kind": "DataQuery",
                "group": GROUP,
                "version": "v0",
                "datasource": {"name": DS},
                "spec": {"format": fmt, "rawQuery": True, "rawSql": raw_sql},
            },
        },
    }


def qg(queries):
    return {"kind": "QueryGroup", "spec": {"queries": queries, "transformations": [], "queryOptions": {}}}


def _stat_opts(graph_mode="area"):
    return {
        "colorMode": "value",
        "graphMode": graph_mode,
        "justifyMode": "auto",
        "orientation": "auto",
        "percentChangeColorMode": "standard",
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        "showPercentChange": False,
        "textMode": "auto",
        "wideLayout": True,
    }


def stat_viz(decimals=2, unit=None, thresholds=None, minv=None, maxv=None, graph_mode="area"):
    defaults = {
        "decimals": decimals,
        "thresholds": {"mode": "absolute", "steps": thresholds or [{"value": None, "color": "blue"}]},
        "color": {"mode": "thresholds"},
    }
    if unit:
        defaults["unit"] = unit
    if minv is not None:
        defaults["min"] = minv
    if maxv is not None:
        defaults["max"] = maxv
    return {
        "kind": "VizConfig",
        "group": "stat",
        "version": VER,
        "spec": {"options": _stat_opts(graph_mode), "fieldConfig": {"defaults": defaults, "overrides": []}},
    }


def ts_viz(decimals=2, unit=None, draw="line", fill=10, stacking="none", overrides=None, tooltip_mode="single"):
    defaults = {
        "decimals": decimals,
        "color": {"mode": "palette-classic"},
        "custom": {
            "drawStyle": draw,
            "fillOpacity": fill,
            "lineWidth": 2 if draw == "line" else 1,
            "showPoints": "never" if draw == "line" else "auto",
            "spanNulls": True if draw == "line" else False,
            "stacking": {"group": "A", "mode": stacking},
        },
    }
    if unit:
        defaults["unit"] = unit
    if stacking == "normal":
        defaults["min"] = 0
        defaults["max"] = 100
    return {
        "kind": "VizConfig",
        "group": "timeseries",
        "version": VER,
        "spec": {
            "options": {
                "legend": {"calcs": [], "displayMode": "list", "placement": "bottom", "showLegend": True},
                "tooltip": {"mode": tooltip_mode, "sort": "none" if tooltip_mode == "single" else "desc"},
            },
            "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        },
    }


def table_viz(sort_by=None):
    opts = {"cellHeight": "sm", "showHeader": True}
    if sort_by:
        opts["sortBy"] = sort_by
    return {
        "kind": "VizConfig",
        "group": "table",
        "version": VER,
        "spec": {
            "options": opts,
            "fieldConfig": {
                "defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False}},
                "overrides": [],
            },
        },
    }


def pie_viz(unit=None):
    defaults = {
        "color": {"mode": "palette-classic"},
        "custom": {"hideFrom": {"legend": False, "tooltip": False, "viz": False}},
    }
    if unit:
        defaults["unit"] = unit
        defaults["decimals"] = 2
    return {
        "kind": "VizConfig",
        "group": "piechart",
        "version": VER,
        "spec": {
            "options": {
                "legend": {"displayMode": "table", "placement": "right", "showLegend": True, "values": ["value"]},
                "pieType": "pie",
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "sort": "desc",
                "tooltip": {"mode": "single", "sort": "none"},
            },
            "fieldConfig": {"defaults": defaults, "overrides": []},
        },
    }


def panel(pid, title, description, data, viz):
    return {
        "kind": "Panel",
        "spec": {"id": pid, "title": title, "description": description, "links": [], "data": data, "vizConfig": viz},
    }


def gird(x, y, w, h, name):
    return {
        "kind": "GridLayoutItem",
        "spec": {
            "x": x,
            "y": y,
            "width": w,
            "height": h,
            "element": {"kind": "ElementReference", "name": name},
        },
    }


def main():
    red_green = [{"value": None, "color": "red"}, {"value": 0, "color": "green"}]
    inc_th = [{"value": None, "color": "green"}, {"value": 1, "color": "yellow"}, {"value": 3, "color": "red"}]
    comp_th = [{"value": None, "color": "red"}, {"value": 50, "color": "yellow"}, {"value": 80, "color": "green"}]

    weights_sql = (
        "SELECT b.ts AS time, "
        "100.0 * CASE WHEN b.asset = 'IRR' THEN b.total ELSE b.total * COALESCE(("
        "SELECT kv.value::numeric FROM jsonb_each_text(p.marks) AS kv(key, value) "
        "WHERE kv.key LIKE b.asset || '-%' LIMIT 1), 0) END / NULLIF(p.portfolio_value, 0) AS value, "
        "b.asset AS metric "
        "FROM pair_trade.balance_snapshot b "
        "JOIN pair_trade.portfolio_snapshot p ON p.session_id = b.session_id AND p.ts = b.ts "
        "WHERE b.session_id = $session AND $__timeFilter(b.ts) ORDER BY b.ts;"
    )
    pie_sql = (
        "WITH latest AS (SELECT MAX(ts) AS ts FROM pair_trade.portfolio_snapshot WHERE session_id = $session), "
        "p AS (SELECT portfolio_value, marks FROM pair_trade.portfolio_snapshot WHERE session_id = $session AND ts = (SELECT ts FROM latest)), "
        "b AS (SELECT asset, total FROM pair_trade.balance_snapshot WHERE session_id = $session AND ts = (SELECT ts FROM latest)) "
        "SELECT b.asset AS metric, 100.0 * CASE WHEN b.asset = 'IRR' THEN b.total ELSE b.total * COALESCE(("
        "SELECT kv.value::numeric FROM jsonb_each_text(p.marks) AS kv(key, value) WHERE kv.key LIKE b.asset || '-%' LIMIT 1), 0) END "
        "/ NULLIF(p.portfolio_value, 0) AS value "
        "FROM b CROSS JOIN p "
        "WHERE CASE WHEN b.asset = 'IRR' THEN b.total ELSE b.total * COALESCE(("
        "SELECT kv.value::numeric FROM jsonb_each_text(p.marks) AS kv(key, value) WHERE kv.key LIKE b.asset || '-%' LIMIT 1), 0) END > 0 "
        "ORDER BY value DESC;"
    )
    edges_sql = (
        "SELECT ts AS time, transition_score * 10000 AS value, 'expected' AS metric FROM pair_trade.rotation "
        "WHERE session_id = $session AND prev_symbol <> 'CASH' AND transition_score IS NOT NULL AND $__timeFilter(ts) "
        "UNION ALL SELECT ts, last_edge * 10000, 'last' FROM pair_trade.rotation "
        "WHERE session_id = $session AND prev_symbol <> 'CASH' AND last_edge IS NOT NULL AND $__timeFilter(ts) "
        "UNION ALL SELECT ts, realized_edge * 10000, 'realized' FROM pair_trade.rotation "
        "WHERE session_id = $session AND prev_symbol <> 'CASH' AND realized_edge IS NOT NULL AND $__timeFilter(ts) "
        "UNION ALL SELECT ts, realized_edge_net * 10000, 'realized_net' FROM pair_trade.rotation "
        "WHERE session_id = $session AND prev_symbol <> 'CASH' AND realized_edge_net IS NOT NULL AND $__timeFilter(ts) ORDER BY 1;"
    )
    slip_sql = (
        "SELECT ts AS time, sell_slippage * 10000 AS value, 'sell_slippage_bps' AS metric FROM pair_trade.rotation "
        "WHERE session_id = $session AND prev_symbol <> 'CASH' AND sell_slippage IS NOT NULL AND $__timeFilter(ts) "
        "UNION ALL SELECT ts, buy_slippage * 10000, 'buy_slippage_bps' FROM pair_trade.rotation "
        "WHERE session_id = $session AND prev_symbol <> 'CASH' AND buy_slippage IS NOT NULL AND $__timeFilter(ts) ORDER BY 1;"
    )

    elements = {
        "panel-1": panel(
            1,
            "P&L (M IRT)",
            "Latest session mark-to-mid P&L from portfolio_snapshot, in million IRT (IRR / 1e7). Green if ≥ 0.",
            qg([pq("SELECT pnl / 1e7 AS pnl_m_irt FROM pair_trade.portfolio_snapshot WHERE session_id = $session ORDER BY ts DESC LIMIT 1;", "table")]),
            stat_viz(2, thresholds=red_green),
        ),
        "panel-1b": panel(
            101,
            "P&L (%)",
            "Latest P&L as % of session start NAV: 100 * pnl / start_value (first portfolio_snapshot of the session).",
            qg([pq(
                "WITH start_nav AS (SELECT portfolio_value AS v FROM pair_trade.portfolio_snapshot WHERE session_id = $session ORDER BY ts ASC LIMIT 1), "
                "latest AS (SELECT pnl FROM pair_trade.portfolio_snapshot WHERE session_id = $session ORDER BY ts DESC LIMIT 1) "
                "SELECT 100.0 * latest.pnl / NULLIF(start_nav.v, 0) AS pnl_pct FROM latest, start_nav;",
                "table",
            )]),
            stat_viz(3, unit="percent", thresholds=red_green),
        ),
        "panel-2": panel(
            2,
            "Portfolio value (M IRT)",
            "Latest mark-to-mid NAV from portfolio_snapshot only (IRR / 1e7).",
            qg([pq("SELECT portfolio_value / 1e7 AS portfolio_m_irt FROM pair_trade.portfolio_snapshot WHERE session_id = $session ORDER BY ts DESC LIMIT 1;", "table")]),
            stat_viz(2),
        ),
        "panel-3": panel(
            3,
            "Incomplete rotations",
            "Count of rotation rows with status ≠ buy_complete (in-flight started/sell_complete/buy_partial, failed/cancelled, or unfinished cash_collector). High means stuck or unfinished legs.",
            qg([pq("SELECT COUNT(*) AS incomplete FROM pair_trade.rotation WHERE session_id = $session AND status <> 'buy_complete';", "table")]),
            stat_viz(0, thresholds=inc_th, graph_mode="none"),
        ),
        "panel-4": panel(
            4,
            "Rotation complete %",
            "Among sell→buy rotations only (excludes cash_collector / prev_symbol=CASH): 100 × count(status=buy_complete) / count(*). Measures how often both legs finish.",
            qg([pq(
                "SELECT CASE WHEN COUNT(*) = 0 THEN 0 ELSE 100.0 * SUM(CASE WHEN status = 'buy_complete' THEN 1 ELSE 0 END) / COUNT(*) END AS complete_rate "
                "FROM pair_trade.rotation WHERE session_id = $session AND prev_symbol <> 'CASH';",
                "table",
            )]),
            stat_viz(1, unit="percent", thresholds=comp_th, minv=0, maxv=100, graph_mode="none"),
        ),
        "panel-5": panel(
            5,
            "Net rotation cash (M IRT)",
            "Sum(quote_earned − quote_spent) for sell→buy only; cash_collector excluded. Cash flow through rotations, not true mark-to-mid P&L.",
            qg([pq("SELECT COALESCE(SUM(quote_earned - quote_spent), 0) / 1e7 AS net_cash_m_irt FROM pair_trade.rotation WHERE session_id = $session AND prev_symbol <> 'CASH';", "table")]),
            stat_viz(2, thresholds=red_green, graph_mode="none"),
        ),
        "panel-6": panel(
            6,
            "Fills",
            "Total fill events this session (rotations + cash-collector), linked via rotation_id when applicable.",
            qg([pq("SELECT COUNT(*) AS fills FROM pair_trade.fill WHERE session_id = $session;", "table")]),
            stat_viz(0, thresholds=[{"value": None, "color": "purple"}], graph_mode="none"),
        ),
        "panel-10": panel(
            10,
            "Portfolio P&L vs session start (M IRT)",
            "Mark-to-mid P&L over time in million IRT (portfolio_snapshot.pnl / 1e7).",
            qg([pq("SELECT ts AS time, pnl / 1e7 AS \"P&L M IRT\" FROM pair_trade.portfolio_snapshot WHERE session_id = $session AND $__timeFilter(ts) ORDER BY ts;")]),
            ts_viz(2, fill=15),
        ),
        "panel-10b": panel(
            110,
            "Portfolio P&L vs session start (%)",
            "100 * pnl / session_start_NAV over time. Start NAV = first portfolio_snapshot.portfolio_value for the session.",
            qg([pq(
                "WITH start_nav AS (SELECT portfolio_value AS v FROM pair_trade.portfolio_snapshot WHERE session_id = $session ORDER BY ts ASC LIMIT 1) "
                "SELECT p.ts AS time, 100.0 * p.pnl / NULLIF(s.v, 0) AS \"P&L %\" FROM pair_trade.portfolio_snapshot p "
                "CROSS JOIN start_nav s WHERE p.session_id = $session AND $__timeFilter(p.ts) ORDER BY p.ts;"
            )]),
            ts_viz(3, unit="percent", fill=15),
        ),
        "panel-11": panel(
            11,
            "Portfolio value (M IRT)",
            "Mark-to-mid NAV over time from portfolio_snapshot (IRR / 1e7).",
            qg([pq("SELECT ts AS time, portfolio_value / 1e7 AS \"Portfolio M IRT\" FROM pair_trade.portfolio_snapshot WHERE session_id = $session AND $__timeFilter(ts) ORDER BY ts;")]),
            ts_viz(2, fill=10),
        ),
        "panel-12": panel(
            12,
            "Portfolio weights by asset (%)",
            "Share of mark-to-mid NAV per asset over time. IRR = cash; ETF bases valued as units × mid from portfolio_snapshot.marks. Stacked area ≈ 100% composition.",
            qg([pq(weights_sql)]),
            ts_viz(2, unit="percent", fill=40, stacking="normal", tooltip_mode="multi"),
        ),
        "panel-12b": panel(
            112,
            "Latest portfolio composition (%)",
            "Pie of latest asset weights by mark-to-mid value (same valuation as weights chart). Cash = IRR.",
            qg([pq(pie_sql, "table")]),
            pie_viz(unit="percent"),
        ),
        "panel-13": panel(
            13,
            "Quote balance & pending orders",
            "Free IRR cash from basket_snapshot (M IRT) and open strategy order count.",
            qg([
                pq("SELECT ts AS time, quote_balance / 1e7 AS \"Cash M IRT\" FROM pair_trade.basket_snapshot WHERE session_id = $session AND $__timeFilter(ts) ORDER BY ts;", ref="A"),
                pq("SELECT ts AS time, pending_order_count AS \"Pending orders\" FROM pair_trade.basket_snapshot WHERE session_id = $session AND $__timeFilter(ts) ORDER BY ts;", ref="B"),
            ]),
            ts_viz(2, fill=0, overrides=[
                {"matcher": {"id": "byName", "options": "Pending orders"}, "properties": [{"id": "custom.axisPlacement", "value": "right"}, {"id": "decimals", "value": 0}]},
                {"matcher": {"id": "byName", "options": "Cash M IRT"}, "properties": [{"id": "decimals", "value": 2}]},
            ]),
        ),
        "panel-14": panel(
            14,
            "Held ETF count",
            "Number of symbols with positive position in basket_snapshot.symbols (jsonb_array_length).",
            qg([pq("SELECT ts AS time, jsonb_array_length(symbols) AS \"Held ETFs\" FROM pair_trade.basket_snapshot WHERE session_id = $session AND $__timeFilter(ts) ORDER BY ts;")]),
            ts_viz(0, fill=0),
        ),
        "panel-15": panel(
            15,
            "Lowest-ret symbol (cash target)",
            "Cash-collector target: argmin(ret + ba_spread), refreshed on snapshots/ticks.",
            qg([pq("SELECT ts, lowest_ret_symbol FROM pair_trade.basket_snapshot WHERE session_id = $session AND lowest_ret_symbol IS NOT NULL AND $__timeFilter(ts) ORDER BY ts DESC LIMIT 100;", "table")]),
            table_viz(),
        ),
        "panel-20": panel(
            20,
            "Return signal by symbol",
            "Primary signal: EWM log-mid residual (signal_snapshot.ret). Positive = rich vs own EWM; negative = cheap. Rotations sell high ret, buy low ret.",
            qg([pq("SELECT bin_ts AS time, ret AS value, symbol AS metric FROM pair_trade.signal_snapshot WHERE session_id = $session AND $__timeFilter(bin_ts) ORDER BY bin_ts;")]),
            ts_viz(4, fill=0),
        ),
        "panel-20b": panel(
            120,
            "Return signal demeaned (cross-section)",
            "For each bin_ts: ret − mean(ret across symbols). Highlights relative richness/cheapness vs the basket average that tick.",
            qg([pq("SELECT bin_ts AS time, ret - AVG(ret) OVER (PARTITION BY bin_ts) AS value, symbol AS metric FROM pair_trade.signal_snapshot WHERE session_id = $session AND ret IS NOT NULL AND $__timeFilter(bin_ts) ORDER BY bin_ts;")]),
            ts_viz(4, fill=0),
        ),
        "panel-21": panel(
            21,
            "Bid-ask spread % by symbol",
            "Relative BA cost: mean((ask−bid)/mid) over the freq bin, displayed as percent (×100).",
            qg([pq("SELECT bin_ts AS time, ba_spread * 100 AS value, symbol AS metric FROM pair_trade.signal_snapshot WHERE session_id = $session AND $__timeFilter(bin_ts) ORDER BY bin_ts;")]),
            ts_viz(3, unit="percent", fill=0),
        ),
        "panel-22": panel(
            22,
            "Latest signals (all symbols)",
            "Latest freq bin. Ordered by ret+ba_spread ASC (cash-collector target at top). Prices in M IRT.",
            qg([pq(
                "SELECT s.symbol, s.ret, s.ba_spread, s.mid_price / 1e7 AS mid_m_irt, s.ret_ask, s.ret_bid, "
                "s.ask_price / 1e7 AS ask_m_irt, s.bid_price / 1e7 AS bid_m_irt, (s.ret + s.ba_spread) AS ret_plus_spread, s.bin_ts "
                "FROM pair_trade.signal_snapshot s WHERE s.session_id = $session AND s.bin_ts = ("
                "SELECT MAX(bin_ts) FROM pair_trade.signal_snapshot WHERE session_id = $session) "
                "ORDER BY s.ret + s.ba_spread ASC NULLS LAST;",
                "table",
            )]),
            table_viz([{"desc": False, "displayName": "ret_plus_spread"}]),
        ),
        "panel-41": panel(
            41,
            "Rotation edges (bps)",
            "Sell→buy only. expected=transition_score; last=ret_bid[prev]−ret_ask[new]; "
            "realized=fill VWAPs (pre-fee); realized_net=fee-adjusted VWAPs. Log units ×10000 ≈ bps. Negatives OK.",
            qg([pq(edges_sql)]),
            ts_viz(1, draw="bars", fill=70, tooltip_mode="multi"),
        ),
        "panel-42": panel(
            42,
            "Buy / sell slippage (bps)",
            "Sell→buy only. sell_slippage=log(sell_vwap/intended_sell); buy_slippage=log(buy_vwap/intended_buy). ×10000 ≈ bps. Sell neg=worse; buy pos=worse.",
            qg([pq(slip_sql)]),
            ts_viz(1, draw="bars", fill=70, tooltip_mode="multi"),
        ),
        "panel-43": panel(
            43,
            "Rotation latency (sec)",
            "Seconds from rotation start (sell placed) to buy_complete for sell→buy rotations.",
            qg([pq("SELECT ts AS time, latency_sec AS \"latency_sec\", prev_symbol || '→' || new_symbol AS metric FROM pair_trade.rotation WHERE session_id = $session AND prev_symbol <> 'CASH' AND latency_sec IS NOT NULL AND $__timeFilter(ts) ORDER BY ts;")]),
            ts_viz(1, unit="s", draw="bars", fill=70),
        ),
        "panel-44": panel(
            44,
            "Cumulative realized edge (bps)",
            "Running sum of realized_edge (pre-fee) and realized_edge_net (post-fee) for sell→buy "
            "(×10000 ≈ bps). Gap between series ≈ cumulative fee drag in edge space.",
            qg([
                pq(
                    "SELECT ts AS time, SUM(realized_edge) OVER (ORDER BY ts) * 10000 AS \"cum realized bps\" "
                    "FROM pair_trade.rotation WHERE session_id = $session AND prev_symbol <> 'CASH' "
                    "AND realized_edge IS NOT NULL AND $__timeFilter(ts) ORDER BY ts;",
                    ref="A",
                ),
                pq(
                    "SELECT ts AS time, SUM(realized_edge_net) OVER (ORDER BY ts) * 10000 AS \"cum realized_net bps\" "
                    "FROM pair_trade.rotation WHERE session_id = $session AND prev_symbol <> 'CASH' "
                    "AND realized_edge_net IS NOT NULL AND $__timeFilter(ts) ORDER BY ts;",
                    ref="B",
                ),
            ]),
            ts_viz(1, fill=15, tooltip_mode="multi"),
        ),
        "panel-30": panel(
            30,
            "Rotation status",
            "Distribution of rotation.status including cash_collector.",
            qg([pq("SELECT status, COUNT(*) AS value FROM pair_trade.rotation WHERE session_id = $session GROUP BY status ORDER BY value DESC;", "table")]),
            pie_viz(),
        ),
        "panel-31": panel(
            31,
            "Expected edge (transition_score) ×100",
            "Sell→buy expected_edge = ret_prev−ret_new−ba_prev−ba_new, shown ×100.",
            qg([pq("SELECT ts AS time, transition_score * 100 AS \"Score %\", prev_symbol || '->' || new_symbol AS metric FROM pair_trade.rotation WHERE session_id = $session AND prev_symbol <> 'CASH' AND transition_score IS NOT NULL AND $__timeFilter(ts) ORDER BY ts;")]),
            ts_viz(2, unit="percent", draw="bars", fill=80),
        ),
        "panel-32": panel(
            32,
            "Cumulative rotation cash (M IRT)",
            "Cumulative quote_earned / quote_spent for sell→buy only (cash_collector excluded).",
            qg([
                pq("SELECT ts AS time, SUM(quote_earned) OVER (ORDER BY ts) / 1e7 AS \"Earned M IRT\" FROM pair_trade.rotation WHERE session_id = $session AND prev_symbol <> 'CASH' AND $__timeFilter(ts) ORDER BY ts;", ref="A"),
                pq("SELECT ts AS time, SUM(quote_spent) OVER (ORDER BY ts) / 1e7 AS \"Spent M IRT\" FROM pair_trade.rotation WHERE session_id = $session AND prev_symbol <> 'CASH' AND $__timeFilter(ts) ORDER BY ts;", ref="B"),
            ]),
            ts_viz(2, fill=0),
        ),
        "panel-33": panel(
            33,
            "Recent rotations",
            "Includes cash_collector (prev_symbol=CASH). Edges (incl. realized_edge_net) and fees_m_irt for debugging.",
            qg([pq(
                "SELECT ts, CASE WHEN prev_symbol = 'CASH' THEN 'cash_collector' ELSE 'rotation' END AS kind, "
                "prev_symbol, new_symbol, status, transition_score, last_edge, realized_edge, realized_edge_net, "
                "sell_slippage, buy_slippage, latency_sec, "
                "transition_value / 1e7 AS transition_value_m_irt, quote_earned / 1e7 AS earned_m_irt, "
                "quote_spent / 1e7 AS spent_m_irt, total_fee_quote / 1e7 AS fees_m_irt, sell_order_id "
                "FROM pair_trade.rotation WHERE session_id = $session ORDER BY ts DESC LIMIT 50;",
                "table",
            )]),
            table_viz([{"desc": True, "displayName": "ts"}]),
        ),
        "panel-34": panel(
            34,
            "Fill volume by side",
            "Aggregated fills by side/pair. fee_quote includes percent fees. Notional/fees in M IRT.",
            qg([pq(
                "SELECT side, trading_pair, COUNT(*) AS fills, ROUND(SUM(amount)::numeric, 6) AS base_volume, "
                "ROUND((SUM(amount * price) / 1e7)::numeric, 2) AS notional_m_irt, ROUND((SUM(fee_quote) / 1e7)::numeric, 4) AS fees_m_irt, "
                "COUNT(*) FILTER (WHERE rotation_id IS NULL) AS unlinked_fills "
                "FROM pair_trade.fill WHERE session_id = $session GROUP BY side, trading_pair ORDER BY notional_m_irt DESC;",
                "table",
            )]),
            table_viz(),
        ),
        "panel-35": panel(
            35,
            "Cumulative fill notional (M IRT)",
            "Running sum of amount×price by side (M IRT).",
            qg([pq("SELECT ts AS time, SUM(amount * price) OVER (PARTITION BY side ORDER BY ts) / 1e7 AS value, side || ' M IRT' AS metric FROM pair_trade.fill WHERE session_id = $session AND $__timeFilter(ts) ORDER BY ts;")]),
            ts_viz(2, fill=0),
        ),
        "panel-36": panel(
            36,
            "Event types (health)",
            "Counts of event_log types (timeouts, skips, checkpoints, …).",
            qg([pq("SELECT event_type, COUNT(*) AS value FROM pair_trade.event_log WHERE session_id = $session GROUP BY event_type ORDER BY value DESC;", "table")]),
            pie_viz(),
        ),
        "panel-37": panel(
            37,
            "Recent events",
            "Latest event_log rows for ops debugging.",
            qg([pq("SELECT ts, level, event_type, payload FROM pair_trade.event_log WHERE session_id = $session ORDER BY ts DESC LIMIT 50;", "table")]),
            table_viz(),
        ),
        "panel-38": panel(
            38,
            "Latest balances",
            "Latest total/available per asset. Prefer weight/composition panels for value view; this keeps raw units.",
            qg([pq(
                "SELECT DISTINCT ON (exchange, asset) exchange, asset, "
                "CASE WHEN asset = 'IRR' THEN total / 1e7 ELSE total END AS total, "
                "CASE WHEN asset = 'IRR' THEN available / 1e7 ELSE available END AS available, "
                "CASE WHEN asset = 'IRR' THEN 'M IRT' ELSE 'units' END AS unit, ts "
                "FROM pair_trade.balance_snapshot WHERE session_id = $session ORDER BY exchange, asset, ts DESC;",
                "table",
            )]),
            table_viz(),
        ),
        "panel-39": panel(
            39,
            "Cash-collector spend (M IRT)",
            "Cumulative quote_spent for cash_collector rotations (prev_symbol=CASH).",
            qg([pq("SELECT ts AS time, SUM(quote_spent) OVER (ORDER BY ts) / 1e7 AS \"CC spent M IRT\" FROM pair_trade.rotation WHERE session_id = $session AND prev_symbol = 'CASH' AND $__timeFilter(ts) ORDER BY ts;")]),
            ts_viz(2, fill=10),
        ),
        "panel-40": panel(
            40,
            "Cumulative fees (M IRT)",
            "Running sum of fills.fee_quote (percent + flat) in M IRT. Includes rotation fills and "
            "cash-collector / unlinked fills — full session fee spend from the exchange.",
            qg([pq("SELECT ts AS time, SUM(fee_quote) OVER (ORDER BY ts) / 1e7 AS \"Fees M IRT\" FROM pair_trade.fills WHERE session_id = $session AND $__timeFilter(ts) ORDER BY ts;")]),
            ts_viz(4, fill=10),
        ),
    }

    layout_items = [
        gird(0, 0, 3, 4, "panel-1"),
        gird(3, 0, 3, 4, "panel-1b"),
        gird(6, 0, 3, 4, "panel-2"),
        gird(9, 0, 3, 4, "panel-3"),
        gird(12, 0, 4, 4, "panel-4"),
        gird(16, 0, 4, 4, "panel-5"),
        gird(20, 0, 4, 4, "panel-6"),
        gird(0, 4, 8, 8, "panel-10"),
        gird(8, 4, 8, 8, "panel-10b"),
        gird(16, 4, 8, 8, "panel-11"),
        gird(0, 12, 16, 8, "panel-12"),
        gird(16, 12, 8, 8, "panel-12b"),
        gird(0, 20, 12, 8, "panel-13"),
        gird(12, 20, 12, 8, "panel-14"),
        gird(0, 28, 24, 6, "panel-15"),
        gird(0, 34, 12, 8, "panel-20"),
        gird(12, 34, 12, 8, "panel-20b"),
        gird(0, 42, 12, 8, "panel-21"),
        gird(12, 42, 12, 8, "panel-22"),
        gird(0, 50, 12, 8, "panel-41"),
        gird(12, 50, 12, 8, "panel-42"),
        gird(0, 58, 12, 7, "panel-43"),
        gird(12, 58, 12, 7, "panel-44"),
        gird(0, 65, 8, 8, "panel-30"),
        gird(8, 65, 8, 8, "panel-31"),
        gird(16, 65, 8, 8, "panel-32"),
        gird(0, 73, 12, 8, "panel-33"),
        gird(12, 73, 12, 8, "panel-34"),
        gird(0, 81, 8, 7, "panel-35"),
        gird(8, 81, 8, 7, "panel-39"),
        gird(16, 81, 8, 7, "panel-40"),
        gird(0, 88, 6, 7, "panel-36"),
        gird(6, 88, 6, 7, "panel-37"),
        gird(12, 88, 12, 8, "panel-38"),
    ]

    dashboard = {
        "apiVersion": "dashboard.grafana.app/v2",
        "kind": "Dashboard",
        "metadata": {
            "name": "hummingbot-pair-trade-mobin-v2",
            "namespace": "default",
            "uid": "hb-pair-trade-mobin-v2",
        },
        "spec": {
            "title": "Hummingbot Pair Trade (Mobin Gold) v2",
            "description": (
                "Gold ETF basket: mark-to-mid P&L (% + M IRT), portfolio weights, signal edges, "
                "rotation execution quality (expected/last/realized/realized_net edge, fees, slippage, latency)."
            ),
            "tags": ["hummingbot", "pair-trade", "mobin", "gold-etf", "v2"],
            "editable": True,
            "cursorSync": "Crosshair",
            "liveNow": False,
            "preload": False,
            "links": [],
            "annotations": [{
                "kind": "AnnotationQuery",
                "spec": {
                    "name": "Annotations & Alerts",
                    "enable": True,
                    "hide": True,
                    "iconColor": "rgba(0, 211, 255, 1)",
                    "builtIn": True,
                    "query": {
                        "kind": "DataQuery",
                        "group": "grafana",
                        "version": "v0",
                        "datasource": {"name": "-- Grafana --"},
                        "spec": {},
                    },
                },
            }],
            "timeSettings": {
                "timezone": "browser",
                "from": "now-6h",
                "to": "now",
                "autoRefresh": "30s",
                "autoRefreshIntervals": ["5s", "10s", "30s", "1m", "5m", "15m", "30m", "1h", "2h", "1d"],
                "hideTimepicker": False,
                "fiscalYearStartMonth": 0,
            },
            "variables": [
                {
                    "kind": "QueryVariable",
                    "spec": {
                        "name": "strategy",
                        "label": "Strategy",
                        "hide": "dontHide",
                        "refresh": "onDashboardLoad",
                        "skipUrlSync": False,
                        "multi": False,
                        "includeAll": False,
                        "allowCustomValue": True,
                        "sort": "alphabeticalAsc",
                        "regex": "",
                        "regexApplyTo": "value",
                        "options": [],
                        "current": {"text": "", "value": ""},
                        "query": {
                            "kind": "DataQuery",
                            "group": GROUP,
                            "version": "v0",
                            "datasource": {"name": DS},
                            "spec": {"__legacyStringValue": "SELECT name AS __text, name AS __value FROM pair_trade.strategy ORDER BY name;"},
                        },
                    },
                },
                {
                    "kind": "QueryVariable",
                    "spec": {
                        "name": "session",
                        "label": "Session",
                        "hide": "dontHide",
                        "refresh": "onDashboardLoad",
                        "skipUrlSync": False,
                        "multi": False,
                        "includeAll": False,
                        "allowCustomValue": True,
                        "sort": "disabled",
                        "regex": "",
                        "regexApplyTo": "value",
                        "options": [],
                        "current": {"text": "", "value": ""},
                        "query": {
                            "kind": "DataQuery",
                            "group": GROUP,
                            "version": "v0",
                            "datasource": {"name": DS},
                            "spec": {
                                "__legacyStringValue": (
                                    "SELECT s.id::text || ' @ ' || to_char(s.started_at, 'YYYY-MM-DD HH24:MI') AS __text, s.id AS __value "
                                    "FROM pair_trade.session s JOIN pair_trade.strategy st ON st.id = s.strategy_id "
                                    "WHERE st.name = '$strategy' ORDER BY s.started_at DESC;"
                                )
                            },
                        },
                    },
                },
            ],
            "elements": elements,
            "layout": {"kind": "GridLayout", "spec": {"items": layout_items}},
        },
    }

    OUT.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
    json.loads(OUT.read_text(encoding="utf-8"))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {len(elements)} panels)")


if __name__ == "__main__":
    main()
