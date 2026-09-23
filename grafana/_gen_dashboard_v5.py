#!/usr/bin/env python3
"""Generate hummingbot-pair-trade-mobin-v5.json Grafana dashboard.

v5 changes vs v4:
  1. Gapless axis maps samples into the Grafana time window [$__from, $__to] so the
     full selected-session range is always visible (fixes clipped beginnings).
     Uses $__unixEpochFrom/To (seconds) — no /1000 division.
  2. Timeline keys use (session_id, real_ts) ordered by session.started_at.
  3. Benchmark index panels read signal_snapshot directly (dense mids; no bin×symbol grid).
  4. Benchmark panels use one SQL query each (All+Top5 via FILTER aggregate).
  5. Portfolio-on-bin alignment uses O(n) fill_rn forward-fill (correct last snapshot, not MAX/LATERAL).
"""
import json
from pathlib import Path

DS = "afz4am1a4lywwf"
GROUP = "grafana-postgresql-datasource"
VER = "13.1.0"
OUT = Path(__file__).with_name("hummingbot-pair-trade-mobin-v5.json")

TOP5_SYMBOLS = ("عیار", "طلا", "کهربا", "گنج", "مثقال")
TOP5_IN = ", ".join(f"'{s}'" for s in TOP5_SYMBOLS)

SESS = "session_id IN ($session)"

# Map distinct (session_id, real_ts) into the active Grafana window (gapless, 1 step/point).
# $__unixEpochFrom/To return Unix SECONDS (not ms) — do not divide by 1000.
_TMAP_CTE = """
tmap AS (
  SELECT d.session_id,
         d.real_ts,
         to_timestamp($__unixEpochFrom())
           + (ROW_NUMBER() OVER (ORDER BY s.started_at, d.real_ts) - 1)
           * (($__unixEpochTo() - $__unixEpochFrom())
              / GREATEST(COUNT(*) OVER (), 1))
           * INTERVAL '1 second' AS time
  FROM (SELECT DISTINCT session_id, real_ts FROM src) d
  JOIN pair_trade.session s ON s.id = d.session_id
)"""

_FILLED_MIDS_CTE = f"""
raw AS (
  SELECT ss.session_id, ss.bin_ts, ss.symbol, ss.mid_price
  FROM pair_trade.signal_snapshot ss
  WHERE {SESS}
),
sym_start AS (
  SELECT symbol,
         (array_agg(r.mid_price ORDER BY s.started_at, r.bin_ts))[1] AS m0
  FROM raw r
  JOIN pair_trade.session s ON s.id = r.session_id
  WHERE r.mid_price IS NOT NULL
  GROUP BY symbol
),
idx AS (
  SELECT r.session_id,
         r.bin_ts AS real_ts,
         AVG(100.0 * (r.mid_price / NULLIF(ss.m0, 0) - 1)) AS all_pct,
         AVG(100.0 * (r.mid_price / NULLIF(ss.m0, 0) - 1))
           FILTER (WHERE r.symbol IN ({TOP5_IN})) AS top5_pct
  FROM raw r
  JOIN sym_start ss ON ss.symbol = r.symbol
  WHERE r.mid_price IS NOT NULL
  GROUP BY r.session_id, r.bin_ts
)"""


def _benchmark_base_cte():
    """Shared CTEs for benchmark/excess panels: raw mids, index %, port as-of bins."""
    return (
        f"{_FILLED_MIDS_CTE}, "
        f"{_port_cte()}, "
        f"bins AS ("
        f"SELECT DISTINCT session_id, bin_ts AS real_ts FROM raw"
        f"), "
        f"{_port_ffill_on_bins_cte()}"
    )


def _port_cte():
    return (
        f"start_nav AS ("
        f"SELECT portfolio_value AS v FROM pair_trade.portfolio_snapshot "
        f"WHERE {SESS} ORDER BY ts ASC LIMIT 1"
        f"), "
        f"port AS ("
        f"SELECT p.session_id, p.ts AS real_ts, "
        f"100.0 * (p.portfolio_value / NULLIF(sn.v, 0) - 1) AS port_pct "
        f"FROM pair_trade.portfolio_snapshot p "
        f"CROSS JOIN start_nav sn "
        f"WHERE p.session_id IN ($session)"
        f")"
    )


def _port_ffill_on_bins_cte():
    """Align portfolio % to signal bins via ordered union + O(n) last-value forward-fill."""
    return """
port_events AS (
  SELECT s.started_at, b.session_id, b.real_ts, 0 AS is_port, NULL::numeric AS port_pct
  FROM bins b
  JOIN pair_trade.session s ON s.id = b.session_id
  UNION ALL
  SELECT s.started_at, p.session_id, p.real_ts, 1 AS is_port, p.port_pct
  FROM port p
  JOIN pair_trade.session s ON s.id = p.session_id
),
port_seq AS (
  SELECT *,
         ROW_NUMBER() OVER (ORDER BY started_at, real_ts, is_port DESC) AS rn
  FROM port_events
),
with_fill_rn AS (
  SELECT *,
         MAX(CASE WHEN port_pct IS NOT NULL THEN rn END)
           OVER (ORDER BY rn ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS fill_rn
  FROM port_seq
),
bin_port AS (
  SELECT w.session_id, w.real_ts, p.port_pct
  FROM with_fill_rn w
  JOIN port_seq p ON p.rn = w.fill_rn
  WHERE w.is_port = 0
)"""


def _benchmark_final_select(value_sql):
    """Join bin_port+idx once, then map gapless time (avoids planner re-scan of idx)."""
    return (
        f"result AS ("
        f"SELECT bp.session_id, bp.real_ts, bp.port_pct, i.all_pct, i.top5_pct "
        f"FROM bin_port bp "
        f"JOIN idx i ON i.session_id = bp.session_id AND i.real_ts = bp.real_ts"
        f"), "
        f"src AS (SELECT session_id, real_ts FROM result), "
        f"{_TMAP_CTE} "
        f"SELECT m.time, {value_sql} "
        f"FROM tmap m "
        f"JOIN result r ON r.session_id = m.session_id AND r.real_ts = m.real_ts;"
    )


def benchmark_vs_indexes_sql():
    return (
        f"WITH {_benchmark_base_cte()}, "
        f"{_benchmark_final_select('r.port_pct AS \"Portfolio %\", r.all_pct AS \"All index %\", r.top5_pct AS \"Top5 index %\"')}"
    )


def excess_vs_indexes_sql():
    return (
        f"WITH {_benchmark_base_cte()}, "
        f"{_benchmark_final_select('r.port_pct - r.all_pct AS \"Excess vs All %\", r.port_pct - r.top5_pct AS \"Excess vs Top5 %\"')}"
    )


def gapless_metric_sql(select_inner, value_expr="value", metric_expr="metric"):
    return (
        f"WITH src AS ({select_inner}), "
        f"{_TMAP_CTE} "
        f"SELECT m.time, s.{value_expr} AS value, s.{metric_expr} AS metric "
        f"FROM src s "
        f"JOIN tmap m ON m.session_id = s.session_id AND m.real_ts = s.real_ts "
        f"ORDER BY m.time;"
    )


def gapless_scalar_sql(select_inner, label, value_expr="v"):
    return (
        f"WITH src AS ({select_inner}), "
        f"{_TMAP_CTE} "
        f'SELECT m.time, s.{value_expr} AS "{label}" '
        f"FROM src s "
        f"JOIN tmap m ON m.session_id = s.session_id AND m.real_ts = s.real_ts "
        f"ORDER BY m.time;"
    )


COLOR_PORTFOLIO = "green"
COLOR_ALL_INDEX = "yellow"
COLOR_TOP5_INDEX = "blue"

GAPLESS_NOTE = (
    " X-axis is trading-time (overnight/weekend/between-session gaps removed); "
    "samples are spread across the dashboard time range. Hover clock is compressed, "
    "not wall-clock. Filter via Session multi-select."
)


def color_override(name, fixed_color):
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": fixed_color}}],
    }


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


def pie_viz(unit=None, all_values=False):
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
                "legend": {
                    "displayMode": "table",
                    "placement": "right",
                    "showLegend": True,
                    "values": ["value", "percent"],
                },
                "pieType": "pie",
                "reduceOptions": {
                    "calcs": ["lastNotNull"],
                    "fields": "",
                    "values": all_values,
                },
                "sort": "desc",
                "tooltip": {"mode": "single", "sort": "none"},
                "displayLabels": ["name", "percent"],
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

    port_pnl_m_sql = (
        f"WITH start_nav AS ("
        f"SELECT portfolio_value AS v FROM pair_trade.portfolio_snapshot "
        f"WHERE {SESS} ORDER BY ts ASC LIMIT 1"
        f"), "
        f"src AS ("
        f"SELECT p.session_id, p.ts AS real_ts, (p.portfolio_value - sn.v) / 1e7 AS pnl_m "
        f"FROM pair_trade.portfolio_snapshot p CROSS JOIN start_nav sn "
        f"WHERE p.session_id IN ($session)"
        f"), "
        f"{_TMAP_CTE} "
        f'SELECT m.time, s.pnl_m AS "P&L M IRT" FROM src s '
        f"JOIN tmap m ON m.session_id = s.session_id AND m.real_ts = s.real_ts "
        f"ORDER BY m.time;"
    )
    port_pnl_pct_sql = (
        f"WITH start_nav AS ("
        f"SELECT portfolio_value AS v FROM pair_trade.portfolio_snapshot "
        f"WHERE {SESS} ORDER BY ts ASC LIMIT 1"
        f"), "
        f"src AS ("
        f"SELECT p.session_id, p.ts AS real_ts, "
        f"100.0 * (p.portfolio_value / NULLIF(sn.v, 0) - 1) AS pnl_pct "
        f"FROM pair_trade.portfolio_snapshot p CROSS JOIN start_nav sn "
        f"WHERE p.session_id IN ($session)"
        f"), "
        f"{_TMAP_CTE} "
        f'SELECT m.time, s.pnl_pct AS "P&L %" FROM src s '
        f"JOIN tmap m ON m.session_id = s.session_id AND m.real_ts = s.real_ts "
        f"ORDER BY m.time;"
    )
    port_value_sql = (
        f"WITH src AS ("
        f"SELECT session_id, ts AS real_ts, portfolio_value / 1e7 AS v "
        f"FROM pair_trade.portfolio_snapshot WHERE {SESS}"
        f"), "
        f"{_TMAP_CTE} "
        f'SELECT m.time, s.v AS "Portfolio M IRT" FROM src s '
        f"JOIN tmap m ON m.session_id = s.session_id AND m.real_ts = s.real_ts "
        f"ORDER BY m.time;"
    )

    weights_sql = (
        f"WITH src AS ("
        f"SELECT b.session_id, b.ts AS real_ts, "
        f"100.0 * CASE WHEN b.asset = 'IRR' THEN b.total ELSE b.total * COALESCE(("
        f"SELECT kv.value::numeric FROM jsonb_each_text(p.marks) AS kv(key, value) "
        f"WHERE kv.key LIKE b.asset || '-%' LIMIT 1), 0) END / NULLIF(p.portfolio_value, 0) AS value, "
        f"b.asset AS metric "
        f"FROM pair_trade.balance_snapshot b "
        f"JOIN pair_trade.portfolio_snapshot p ON p.session_id = b.session_id AND p.ts = b.ts "
        f"WHERE b.session_id IN ($session)"
        f"), "
        f"{_TMAP_CTE} "
        f"SELECT m.time, s.value, s.metric FROM src s "
        f"JOIN tmap m ON m.session_id = s.session_id AND m.real_ts = s.real_ts "
        f"ORDER BY m.time;"
    )
    pie_sql = (
        f"WITH latest AS (SELECT MAX(ts) AS ts FROM pair_trade.portfolio_snapshot WHERE {SESS}), "
        f"p AS (SELECT ts, portfolio_value, marks FROM pair_trade.portfolio_snapshot "
        f"WHERE {SESS} AND ts = (SELECT ts FROM latest)), "
        f"b AS (SELECT asset, SUM(total) AS total FROM pair_trade.balance_snapshot "
        f"WHERE {SESS} AND ts = (SELECT ts FROM latest) GROUP BY asset), "
        f"valued AS ("
        f"SELECT p.ts AS time, b.asset AS metric, "
        f"100.0 * CASE WHEN b.asset = 'IRR' THEN b.total ELSE b.total * COALESCE(("
        f"SELECT kv.value::numeric FROM jsonb_each_text(p.marks) AS kv(key, value) "
        f"WHERE kv.key = b.asset || '-IRR' OR kv.key LIKE b.asset || '-%' LIMIT 1), 0) END "
        f"/ NULLIF(p.portfolio_value, 0) AS value "
        f"FROM b CROSS JOIN p"
        f") "
        f"SELECT time, value, metric FROM valued WHERE value > 0 ORDER BY value DESC;"
    )
    edges_sql = (
        f"WITH src AS ("
        f"SELECT session_id, ts AS real_ts, transition_score * 10000 AS value, 'expected' AS metric "
        f"FROM pair_trade.rotation WHERE {SESS} AND prev_symbol <> 'CASH' AND transition_score IS NOT NULL "
        f"UNION ALL SELECT session_id, ts, last_edge * 10000, 'last' FROM pair_trade.rotation "
        f"WHERE {SESS} AND prev_symbol <> 'CASH' AND last_edge IS NOT NULL "
        f"UNION ALL SELECT session_id, ts, realized_edge * 10000, 'realized' FROM pair_trade.rotation "
        f"WHERE {SESS} AND prev_symbol <> 'CASH' AND realized_edge IS NOT NULL "
        f"UNION ALL SELECT session_id, ts, realized_edge_net * 10000, 'realized_net' FROM pair_trade.rotation "
        f"WHERE {SESS} AND prev_symbol <> 'CASH' AND realized_edge_net IS NOT NULL"
        f"), "
        f"{_TMAP_CTE} "
        f"SELECT m.time, s.value, s.metric FROM src s "
        f"JOIN tmap m ON m.session_id = s.session_id AND m.real_ts = s.real_ts "
        f"ORDER BY m.time;"
    )
    slip_sql = (
        f"WITH src AS ("
        f"SELECT session_id, ts AS real_ts, sell_slippage * 10000 AS value, 'sell_slippage_bps' AS metric "
        f"FROM pair_trade.rotation WHERE {SESS} AND prev_symbol <> 'CASH' AND sell_slippage IS NOT NULL "
        f"UNION ALL SELECT session_id, ts, buy_slippage * 10000, 'buy_slippage_bps' FROM pair_trade.rotation "
        f"WHERE {SESS} AND prev_symbol <> 'CASH' AND buy_slippage IS NOT NULL"
        f"), "
        f"{_TMAP_CTE} "
        f"SELECT m.time, s.value, s.metric FROM src s "
        f"JOIN tmap m ON m.session_id = s.session_id AND m.real_ts = s.real_ts "
        f"ORDER BY m.time;"
    )

    elements = {
        "panel-1": panel(
            1,
            "P&L (M IRT)",
            "Mark-to-mid P&L vs first portfolio_value across selected sessions (M IRT = IRR/1e7). Green if ≥ 0.",
            qg([pq(
                f"WITH start_nav AS ("
                f"SELECT portfolio_value AS v FROM pair_trade.portfolio_snapshot "
                f"WHERE {SESS} ORDER BY ts ASC LIMIT 1), "
                f"latest AS ("
                f"SELECT portfolio_value FROM pair_trade.portfolio_snapshot "
                f"WHERE {SESS} ORDER BY ts DESC LIMIT 1) "
                f"SELECT (latest.portfolio_value - start_nav.v) / 1e7 AS pnl_m_irt "
                f"FROM latest, start_nav;",
                "table",
            )]),
            stat_viz(2, thresholds=red_green),
        ),
        "panel-1b": panel(
            101,
            "P&L (%)",
            "100 * (latest_NAV / first_NAV − 1) across selected sessions (shared baseline).",
            qg([pq(
                f"WITH start_nav AS ("
                f"SELECT portfolio_value AS v FROM pair_trade.portfolio_snapshot "
                f"WHERE {SESS} ORDER BY ts ASC LIMIT 1), "
                f"latest AS ("
                f"SELECT portfolio_value FROM pair_trade.portfolio_snapshot "
                f"WHERE {SESS} ORDER BY ts DESC LIMIT 1) "
                f"SELECT 100.0 * (latest.portfolio_value / NULLIF(start_nav.v, 0) - 1) AS pnl_pct "
                f"FROM latest, start_nav;",
                "table",
            )]),
            stat_viz(3, unit="percent", thresholds=red_green),
        ),
        "panel-2": panel(
            2,
            "Portfolio value (M IRT)",
            "Latest mark-to-mid NAV from portfolio_snapshot across selected sessions (IRR / 1e7).",
            qg([pq(
                f"SELECT portfolio_value / 1e7 AS portfolio_m_irt FROM pair_trade.portfolio_snapshot "
                f"WHERE {SESS} ORDER BY ts DESC LIMIT 1;",
                "table",
            )]),
            stat_viz(2),
        ),
        "panel-3": panel(
            3,
            "Incomplete rotations",
            "Count of rotation rows with status ≠ buy_complete across selected sessions.",
            qg([pq(
                f"SELECT COUNT(*) AS incomplete FROM pair_trade.rotation "
                f"WHERE {SESS} AND status <> 'buy_complete';",
                "table",
            )]),
            stat_viz(0, thresholds=inc_th, graph_mode="none"),
        ),
        "panel-4": panel(
            4,
            "Rotation complete %",
            "Among sell→buy rotations (excludes cash_collector): 100 × buy_complete / count(*).",
            qg([pq(
                f"SELECT CASE WHEN COUNT(*) = 0 THEN 0 ELSE "
                f"100.0 * SUM(CASE WHEN status = 'buy_complete' THEN 1 ELSE 0 END) / COUNT(*) END AS complete_rate "
                f"FROM pair_trade.rotation WHERE {SESS} AND prev_symbol <> 'CASH';",
                "table",
            )]),
            stat_viz(1, unit="percent", thresholds=comp_th, minv=0, maxv=100, graph_mode="none"),
        ),
        "panel-5": panel(
            5,
            "Net rotation cash (M IRT)",
            "Sum(quote_earned − quote_spent) for sell→buy across selected sessions.",
            qg([pq(
                f"SELECT COALESCE(SUM(quote_earned - quote_spent), 0) / 1e7 AS net_cash_m_irt "
                f"FROM pair_trade.rotation WHERE {SESS} AND prev_symbol <> 'CASH';",
                "table",
            )]),
            stat_viz(2, thresholds=red_green, graph_mode="none"),
        ),
        "panel-6": panel(
            6,
            "Fills",
            "Total fill events across selected sessions.",
            qg([pq(
                f"SELECT COUNT(*) AS fills FROM pair_trade.fills WHERE {SESS};",
                "table",
            )]),
            stat_viz(0, thresholds=[{"value": None, "color": "purple"}], graph_mode="none"),
        ),
        "panel-10": panel(
            10,
            "Portfolio P&L vs shared start (M IRT)",
            "Mark-to-mid P&L vs first NAV of selected sessions (M IRT)." + GAPLESS_NOTE,
            qg([pq(port_pnl_m_sql)]),
            ts_viz(2, fill=15),
        ),
        "panel-10b": panel(
            110,
            "Portfolio P&L vs shared start (%)",
            "100 * (NAV / first_NAV − 1) across selected sessions." + GAPLESS_NOTE,
            qg([pq(port_pnl_pct_sql)]),
            ts_viz(3, unit="percent", fill=15),
        ),
        "panel-11": panel(
            11,
            "Portfolio value (M IRT)",
            "Mark-to-mid NAV over trading-time from portfolio_snapshot." + GAPLESS_NOTE,
            qg([pq(port_value_sql)]),
            ts_viz(2, fill=10),
        ),
        "panel-10c": panel(
            111,
            "Portfolio return vs benchmark indexes (%)",
            "Portfolio % (shared first-NAV baseline) vs equal-weight mid indexes. "
            "All = every symbol; Top5 = عیار، طلا، کهربا، گنج، مثقال. "
            "Index: 100*(mid / first_mid − 1) from signal_snapshot (shared baseline). "
            "Portfolio aligned via forward-fill on bins."
            + GAPLESS_NOTE,
            qg([pq(benchmark_vs_indexes_sql())]),
            ts_viz(
                3,
                unit="percent",
                fill=10,
                tooltip_mode="multi",
                overrides=[
                    color_override("Portfolio %", COLOR_PORTFOLIO),
                    color_override("All index %", COLOR_ALL_INDEX),
                    color_override("Top5 index %", COLOR_TOP5_INDEX),
                ],
            ),
        ),
        "panel-10d": panel(
            113,
            "Portfolio excess return vs indexes (%)",
            "Portfolio % minus each equal-weight mid index % (shared baselines). "
            "Positive = outperforming that benchmark."
            + GAPLESS_NOTE,
            qg([pq(excess_vs_indexes_sql())]),
            ts_viz(
                3,
                unit="percent",
                fill=15,
                tooltip_mode="multi",
                overrides=[
                    color_override("Excess vs All %", COLOR_ALL_INDEX),
                    color_override("Excess vs Top5 %", COLOR_TOP5_INDEX),
                ],
            ),
        ),
        "panel-12": panel(
            12,
            "Portfolio weights by asset (%)",
            "Share of mark-to-mid NAV per asset. Stacked area ≈ 100%." + GAPLESS_NOTE,
            qg([pq(weights_sql)]),
            ts_viz(2, unit="percent", fill=40, stacking="normal", tooltip_mode="multi"),
        ),
        "panel-12b": panel(
            112,
            "Latest portfolio composition (%)",
            "Pie of latest asset weights by mark-to-mid value across selected sessions. Cash = IRR.",
            qg([pq(pie_sql)]),
            pie_viz(unit="percent"),
        ),
        "panel-13": panel(
            13,
            "Quote balance & pending orders",
            "Free IRR cash (M IRT) and open strategy order count." + GAPLESS_NOTE,
            qg([
                pq(
                    gapless_scalar_sql(
                        f"SELECT session_id, ts AS real_ts, quote_balance / 1e7 AS v "
                        f"FROM pair_trade.basket_snapshot WHERE {SESS}",
                        "Cash M IRT",
                    ),
                    ref="A",
                ),
                pq(
                    gapless_scalar_sql(
                        f"SELECT session_id, ts AS real_ts, pending_order_count AS v "
                        f"FROM pair_trade.basket_snapshot WHERE {SESS}",
                        "Pending orders",
                    ),
                    ref="B",
                ),
            ]),
            ts_viz(2, fill=0, overrides=[
                {"matcher": {"id": "byName", "options": "Pending orders"}, "properties": [{"id": "custom.axisPlacement", "value": "right"}, {"id": "decimals", "value": 0}]},
                {"matcher": {"id": "byName", "options": "Cash M IRT"}, "properties": [{"id": "decimals", "value": 2}]},
            ]),
        ),
        "panel-14": panel(
            14,
            "Held ETF count",
            "jsonb_array_length(basket_snapshot.symbols)." + GAPLESS_NOTE,
            qg([pq(
                gapless_scalar_sql(
                    f"SELECT session_id, ts AS real_ts, jsonb_array_length(symbols) AS v "
                    f"FROM pair_trade.basket_snapshot WHERE {SESS}",
                    "Held ETFs",
                )
            )]),
            ts_viz(0, fill=0),
        ),
        "panel-15": panel(
            15,
            "Lowest-ret symbol (cash target)",
            "Cash-collector target across selected sessions (wall-clock ts).",
            qg([pq(
                f"SELECT ts, session_id, lowest_ret_symbol FROM pair_trade.basket_snapshot "
                f"WHERE {SESS} AND lowest_ret_symbol IS NOT NULL ORDER BY ts DESC LIMIT 100;",
                "table",
            )]),
            table_viz(),
        ),
        "panel-20": panel(
            20,
            "Return signal by symbol",
            "EWM log-mid residual (signal_snapshot.ret)." + GAPLESS_NOTE,
            qg([pq(gapless_metric_sql(
                f"SELECT session_id, bin_ts AS real_ts, ret AS value, symbol AS metric "
                f"FROM pair_trade.signal_snapshot WHERE {SESS} AND ret IS NOT NULL"
            ))]),
            ts_viz(4, fill=0),
        ),
        "panel-20b": panel(
            120,
            "Return signal demeaned (cross-section)",
            "ret − mean(ret) per bin_ts across symbols." + GAPLESS_NOTE,
            qg([pq(gapless_metric_sql(
                f"SELECT session_id, bin_ts AS real_ts, "
                f"ret - AVG(ret) OVER (PARTITION BY session_id, bin_ts) AS value, symbol AS metric "
                f"FROM pair_trade.signal_snapshot WHERE {SESS} AND ret IS NOT NULL"
            ))]),
            ts_viz(4, fill=0),
        ),
        "panel-21": panel(
            21,
            "Bid-ask spread % by symbol",
            "BA cost as percent (×100)." + GAPLESS_NOTE,
            qg([pq(gapless_metric_sql(
                f"SELECT session_id, bin_ts AS real_ts, ba_spread * 100 AS value, symbol AS metric "
                f"FROM pair_trade.signal_snapshot WHERE {SESS} AND ba_spread IS NOT NULL"
            ))]),
            ts_viz(3, unit="percent", fill=0),
        ),
        "panel-22": panel(
            22,
            "Latest signals (all symbols)",
            "Latest freq bin across selected sessions. Ordered by ret+ba_spread ASC.",
            qg([pq(
                f"SELECT s.symbol, s.ret, s.ba_spread, s.mid_price / 1e7 AS mid_m_irt, s.ret_ask, s.ret_bid, "
                f"s.ask_price / 1e7 AS ask_m_irt, s.bid_price / 1e7 AS bid_m_irt, "
                f"(s.ret + s.ba_spread) AS ret_plus_spread, s.bin_ts, s.session_id "
                f"FROM pair_trade.signal_snapshot s WHERE s.session_id IN ($session) AND s.bin_ts = ("
                f"SELECT MAX(bin_ts) FROM pair_trade.signal_snapshot WHERE {SESS}) "
                f"ORDER BY s.ret + s.ba_spread ASC NULLS LAST;",
                "table",
            )]),
            table_viz([{"desc": False, "displayName": "ret_plus_spread"}]),
        ),
        "panel-41": panel(
            41,
            "Rotation edges (bps)",
            "Sell→buy only. expected/last/realized/realized_net ×10000 ≈ bps." + GAPLESS_NOTE,
            qg([pq(edges_sql)]),
            ts_viz(1, draw="bars", fill=70, tooltip_mode="multi"),
        ),
        "panel-42": panel(
            42,
            "Buy / sell slippage (bps)",
            "Sell→buy slippage ×10000 ≈ bps." + GAPLESS_NOTE,
            qg([pq(slip_sql)]),
            ts_viz(1, draw="bars", fill=70, tooltip_mode="multi"),
        ),
        "panel-43": panel(
            43,
            "Rotation latency (sec)",
            "Seconds from sell placed to buy_complete." + GAPLESS_NOTE,
            qg([pq(gapless_metric_sql(
                f"SELECT session_id, ts AS real_ts, latency_sec AS value, "
                f"prev_symbol || '→' || new_symbol AS metric "
                f"FROM pair_trade.rotation WHERE {SESS} AND prev_symbol <> 'CASH' "
                f"AND latency_sec IS NOT NULL"
            ))]),
            ts_viz(1, unit="s", draw="bars", fill=70),
        ),
        "panel-44": panel(
            44,
            "Cumulative realized edge (bps)",
            "Running sum of realized_edge / realized_edge_net (×10000)." + GAPLESS_NOTE,
            qg([
                pq(
                    f"WITH base AS ("
                    f"SELECT session_id, ts AS real_ts, realized_edge, realized_edge_net "
                    f"FROM pair_trade.rotation WHERE {SESS} AND prev_symbol <> 'CASH' "
                    f"AND (realized_edge IS NOT NULL OR realized_edge_net IS NOT NULL)"
                    f"), "
                    f"src AS (SELECT session_id, real_ts FROM base), "
                    f"{_TMAP_CTE}, "
                    f"cum AS ("
                    f"SELECT session_id, real_ts, "
                    f"SUM(realized_edge) OVER (PARTITION BY session_id ORDER BY real_ts) * 10000 AS v "
                    f"FROM base"
                    f") "
                    f'SELECT m.time, c.v AS "cum realized bps" FROM tmap m '
                    f"JOIN cum c ON c.session_id = m.session_id AND c.real_ts = m.real_ts "
                    f"ORDER BY m.time;",
                    ref="A",
                ),
                pq(
                    f"WITH base AS ("
                    f"SELECT session_id, ts AS real_ts, realized_edge, realized_edge_net "
                    f"FROM pair_trade.rotation WHERE {SESS} AND prev_symbol <> 'CASH' "
                    f"AND (realized_edge IS NOT NULL OR realized_edge_net IS NOT NULL)"
                    f"), "
                    f"src AS (SELECT session_id, real_ts FROM base), "
                    f"{_TMAP_CTE}, "
                    f"cum AS ("
                    f"SELECT session_id, real_ts, "
                    f"SUM(realized_edge_net) OVER (PARTITION BY session_id ORDER BY real_ts) * 10000 AS v "
                    f"FROM base"
                    f") "
                    f'SELECT m.time, c.v AS "cum realized_net bps" FROM tmap m '
                    f"JOIN cum c ON c.session_id = m.session_id AND c.real_ts = m.real_ts "
                    f"ORDER BY m.time;",
                    ref="B",
                ),
            ]),
            ts_viz(1, fill=15, tooltip_mode="multi"),
        ),
        "panel-30": panel(
            30,
            "Rotation status",
            "Distribution of rotation.status across selected sessions.",
            qg([pq(
                f"SELECT NOW() AS time, COUNT(*)::float AS value, status AS metric "
                f"FROM pair_trade.rotation WHERE {SESS} GROUP BY status ORDER BY value DESC;"
            )]),
            pie_viz(),
        ),
        "panel-31": panel(
            31,
            "Expected edge (transition_score) ×100",
            "Sell→buy expected_edge ×100." + GAPLESS_NOTE,
            qg([pq(gapless_metric_sql(
                f"SELECT session_id, ts AS real_ts, transition_score * 100 AS value, "
                f"prev_symbol || '->' || new_symbol AS metric "
                f"FROM pair_trade.rotation WHERE {SESS} AND prev_symbol <> 'CASH' "
                f"AND transition_score IS NOT NULL"
            ))]),
            ts_viz(2, unit="percent", draw="bars", fill=80),
        ),
        "panel-32": panel(
            32,
            "Cumulative rotation cash (M IRT)",
            "Cumulative quote_earned / quote_spent for sell→buy." + GAPLESS_NOTE,
            qg([
                pq(
                    f"WITH src AS ("
                    f"SELECT session_id, ts AS real_ts FROM pair_trade.rotation "
                    f"WHERE {SESS} AND prev_symbol <> 'CASH'"
                    f"), {_TMAP_CTE}, "
                    f"cum AS ("
                    f"SELECT session_id, ts AS real_ts, SUM(quote_earned) OVER ("
                    f"PARTITION BY session_id ORDER BY ts) / 1e7 AS v "
                    f"FROM pair_trade.rotation WHERE {SESS} AND prev_symbol <> 'CASH'"
                    f") "
                    f'SELECT m.time, c.v AS "Earned M IRT" FROM tmap m '
                    f"JOIN cum c ON c.session_id = m.session_id AND c.real_ts = m.real_ts "
                    f"ORDER BY m.time;",
                    ref="A",
                ),
                pq(
                    f"WITH src AS ("
                    f"SELECT session_id, ts AS real_ts FROM pair_trade.rotation "
                    f"WHERE {SESS} AND prev_symbol <> 'CASH'"
                    f"), {_TMAP_CTE}, "
                    f"cum AS ("
                    f"SELECT session_id, ts AS real_ts, SUM(quote_spent) OVER ("
                    f"PARTITION BY session_id ORDER BY ts) / 1e7 AS v "
                    f"FROM pair_trade.rotation WHERE {SESS} AND prev_symbol <> 'CASH'"
                    f") "
                    f'SELECT m.time, c.v AS "Spent M IRT" FROM tmap m '
                    f"JOIN cum c ON c.session_id = m.session_id AND c.real_ts = m.real_ts "
                    f"ORDER BY m.time;",
                    ref="B",
                ),
            ]),
            ts_viz(2, fill=0),
        ),
        "panel-33": panel(
            33,
            "Recent rotations",
            "Includes cash_collector. Wall-clock ts across selected sessions.",
            qg([pq(
                f"SELECT ts, session_id, "
                f"CASE WHEN prev_symbol = 'CASH' THEN 'cash_collector' ELSE 'rotation' END AS kind, "
                f"prev_symbol, new_symbol, status, transition_score, last_edge, realized_edge, realized_edge_net, "
                f"sell_slippage, buy_slippage, latency_sec, "
                f"transition_value / 1e7 AS transition_value_m_irt, quote_earned / 1e7 AS earned_m_irt, "
                f"quote_spent / 1e7 AS spent_m_irt, total_fee_quote / 1e7 AS fees_m_irt, sell_order_id "
                f"FROM pair_trade.rotation WHERE {SESS} ORDER BY ts DESC LIMIT 50;",
                "table",
            )]),
            table_viz([{"desc": True, "displayName": "ts"}]),
        ),
        "panel-34": panel(
            34,
            "Fill volume by side",
            "Aggregated fills by side/pair across selected sessions.",
            qg([pq(
                f"SELECT side, trading_pair, COUNT(*) AS fills, ROUND(SUM(amount)::numeric, 6) AS base_volume, "
                f"ROUND((SUM(amount * price) / 1e7)::numeric, 2) AS notional_m_irt, "
                f"ROUND((SUM(fee_quote) / 1e7)::numeric, 4) AS fees_m_irt, "
                f"COUNT(*) FILTER (WHERE rotation_id IS NULL) AS unlinked_fills "
                f"FROM pair_trade.fills WHERE {SESS} GROUP BY side, trading_pair ORDER BY notional_m_irt DESC;",
                "table",
            )]),
            table_viz(),
        ),
        "panel-35": panel(
            35,
            "Cumulative fill notional (M IRT)",
            "Running sum of amount×price by side." + GAPLESS_NOTE,
            qg([pq(gapless_metric_sql(
                f"SELECT session_id, ts AS real_ts, "
                f"SUM(amount * price) OVER (PARTITION BY session_id, side ORDER BY ts) / 1e7 AS value, "
                f"side || ' M IRT' AS metric "
                f"FROM pair_trade.fills WHERE {SESS}"
            ))]),
            ts_viz(2, fill=0),
        ),
        "panel-36": panel(
            36,
            "Event types (health)",
            "Counts of event_log types across selected sessions.",
            qg([pq(
                f"SELECT NOW() AS time, COUNT(*)::float AS value, event_type AS metric "
                f"FROM pair_trade.event_log WHERE {SESS} GROUP BY event_type ORDER BY value DESC;"
            )]),
            pie_viz(),
        ),
        "panel-37": panel(
            37,
            "Recent events",
            "Latest event_log rows (wall-clock).",
            qg([pq(
                f"SELECT ts, session_id, level, event_type, payload FROM pair_trade.event_log "
                f"WHERE {SESS} ORDER BY ts DESC LIMIT 50;",
                "table",
            )]),
            table_viz(),
        ),
        "panel-38": panel(
            38,
            "Latest balances",
            "Latest total/available per asset across selected sessions.",
            qg([pq(
                f"SELECT DISTINCT ON (exchange, asset) exchange, asset, "
                f"CASE WHEN asset = 'IRR' THEN total / 1e7 ELSE total END AS total, "
                f"CASE WHEN asset = 'IRR' THEN available / 1e7 ELSE available END AS available, "
                f"CASE WHEN asset = 'IRR' THEN 'M IRT' ELSE 'units' END AS unit, ts, session_id "
                f"FROM pair_trade.balance_snapshot WHERE {SESS} "
                f"ORDER BY exchange, asset, ts DESC;",
                "table",
            )]),
            table_viz(),
        ),
        "panel-39": panel(
            39,
            "Cash-collector spend (M IRT)",
            "Cumulative quote_spent for cash_collector." + GAPLESS_NOTE,
            qg([pq(
                f"WITH src AS ("
                f"SELECT session_id, ts AS real_ts, "
                f"SUM(quote_spent) OVER (PARTITION BY session_id ORDER BY ts) / 1e7 AS v "
                f"FROM pair_trade.rotation WHERE {SESS} AND prev_symbol = 'CASH'"
                f"), {_TMAP_CTE} "
                f'SELECT m.time, s.v AS "CC spent M IRT" FROM src s '
                f"JOIN tmap m ON m.session_id = s.session_id AND m.real_ts = s.real_ts "
                f"ORDER BY m.time;"
            )]),
            ts_viz(2, fill=10),
        ),
        "panel-40": panel(
            40,
            "Cumulative fees (M IRT)",
            "Running sum of fills.fee_quote across selected sessions." + GAPLESS_NOTE,
            qg([pq(
                f"WITH src AS ("
                f"SELECT session_id, ts AS real_ts, "
                f"SUM(fee_quote) OVER (PARTITION BY session_id ORDER BY ts) / 1e7 AS v "
                f"FROM pair_trade.fills WHERE {SESS}"
                f"), {_TMAP_CTE} "
                f'SELECT m.time, s.v AS "Fees M IRT" FROM src s '
                f"JOIN tmap m ON m.session_id = s.session_id AND m.real_ts = s.real_ts "
                f"ORDER BY m.time;"
            )]),
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
        gird(0, 12, 12, 8, "panel-10c"),
        gird(12, 12, 12, 8, "panel-10d"),
        gird(0, 20, 16, 8, "panel-12"),
        gird(16, 20, 8, 8, "panel-12b"),
        gird(0, 28, 12, 8, "panel-13"),
        gird(12, 28, 12, 8, "panel-14"),
        gird(0, 36, 24, 6, "panel-15"),
        gird(0, 42, 12, 8, "panel-20"),
        gird(12, 42, 12, 8, "panel-20b"),
        gird(0, 50, 12, 8, "panel-21"),
        gird(12, 50, 12, 8, "panel-22"),
        gird(0, 58, 12, 8, "panel-41"),
        gird(12, 58, 12, 8, "panel-42"),
        gird(0, 66, 12, 7, "panel-43"),
        gird(12, 66, 12, 7, "panel-44"),
        gird(0, 73, 8, 8, "panel-30"),
        gird(8, 73, 8, 8, "panel-31"),
        gird(16, 73, 8, 8, "panel-32"),
        gird(0, 81, 12, 8, "panel-33"),
        gird(12, 81, 12, 8, "panel-34"),
        gird(0, 89, 8, 7, "panel-35"),
        gird(8, 89, 8, 7, "panel-39"),
        gird(16, 89, 8, 7, "panel-40"),
        gird(0, 96, 6, 7, "panel-36"),
        gird(6, 96, 6, 7, "panel-37"),
        gird(12, 96, 12, 8, "panel-38"),
    ]

    dashboard = {
        "apiVersion": "dashboard.grafana.app/v2",
        "kind": "Dashboard",
        "metadata": {
            "name": "hummingbot-pair-trade-mobin-v5",
            "namespace": "default",
            "uid": "hb-pair-trade-mobin-v5",
        },
        "spec": {
            "title": "Hummingbot Pair Trade (Mobin Gold) v5",
            "description": (
                "v5: multi-session select, shared cumulative baseline across sessions, "
                "gapless trading-time axis mapped to the dashboard time range (overnight/weekend removed). "
                "Gold ETF basket: mark-to-mid P&L, equal-weight mid indexes (All / Top5), "
                "excess vs benchmarks, weights, signals, rotation quality."
            ),
            "tags": ["hummingbot", "pair-trade", "mobin", "gold-etf", "v5"],
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
                "from": "now-7d",
                "to": "now",
                "autoRefresh": "1m",
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
                        "multi": True,
                        "includeAll": True,
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
