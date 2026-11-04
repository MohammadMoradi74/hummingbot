#!/usr/bin/env python3
"""Generate hummingbot-pair-trade-mobin-v7.json Grafana dashboard.

v7 vs v6:
  1. Shared cumulative baseline = prior-session last NAV / last mid (not first tick).
  2. Trimmed panel set; bid-ask + latest signals at the end.
  3. Weight/composition labels: static IR→Persian VALUES map (نقد = cash).
  4. Default session = latest (includeAll off, ORDER BY started_at DESC).
  5. Default time range = today 12:02 → now (Asia/Tehran).
  6. Top row: Portfolio value TS (12×8) + P&L% / Portfolio value KPIs on the right.
  7. Return + excess share one SQL via Dashboard datasource.
  8. Benchmark indexes from portfolio_snapshot.marks (1‑min), not signal_snapshot (5s).

Inherited: gapless axis, 12:00–18:00 Asia/Tehran, Top5 indexes.
"""
import json
from pathlib import Path

DS = "afz4am1a4lywwf"
GROUP = "grafana-postgresql-datasource"
VER = "13.1.0"
OUT = Path(__file__).with_name("hummingbot-pair-trade-mobin-v7.json")

# TSE gold ETF regular session (Asia/Tehran wall clock). Do not replace with fixed UTC hours:
# this dataset mixes +3:30 and +4:30 offsets, so 08:30–14:30 UTC ≠ 12:00–18:00 Tehran.
MARKET_TZ = "Asia/Tehran"
MARKET_OPEN = "12:00:00"
MARKET_CLOSE = "18:00:00"


def _in_market_hours(ts_col: str) -> str:
    """SQL predicate: timestamptz column's local time within regular session."""
    loc = f"({ts_col} AT TIME ZONE '{MARKET_TZ}')::time"
    return f"({loc} >= TIME '{MARKET_OPEN}' AND {loc} <= TIME '{MARKET_CLOSE}')"


def _is_weekday(ts_col: str) -> str:
    """Tehran DOW: exclude Thu(4) and Fri(5) — TSE closed."""
    return f"EXTRACT(DOW FROM ({ts_col} AT TIME ZONE '{MARKET_TZ}')) NOT IN (4, 5)"


TOP5_SYMBOLS = ("عیار", "طلا", "کهربا", "گنج", "مثقال")

SESS = "session_id IN ($session)"
# Last (newest started_at) among multi-selected $session — for heavy signal_snapshot TS panels.
LAST_SESS = (
    "session_id = ("
    "SELECT id FROM pair_trade.session WHERE id IN ($session) "
    "ORDER BY started_at DESC LIMIT 1)"
)

# Static IR base → Persian label (tickers.csv + extras seen in DB). Cheap VALUES, no scan.
# Holdings that never rotated still resolve; IRR cash labeled نقد.
_ASSET_LABELS = (
    ("IRTKALTN0001", "آلتون"),
    ("IRTKATSH0001", "آتش"),
    ("IRTKDORN0001", "درنا"),
    ("IRTKDRKS0001", "درخشان"),
    ("IRTKEMRL0001", "امرالد"),
    ("IRTKGANJ0001", "گنج"),
    ("IRTKGHIR0001", "قیراط"),
    ("IRTKGOLD0001", "گلدا"),
    ("IRTKGOLN0001", "گلدیس"),
    ("IRTKHAMY0001", "همیان"),
    ("IRTKJAMF0001", "جام طلا"),
    ("IRTKJAVA0001", "جواهر"),
    ("IRTKKIAN0001", "گوهر"),
    ("IRTKLIAN0001", "لیان"),
    ("IRTKLOTF0001", "طلا"),
    ("IRTKMIRA0001", "میراث"),
    ("IRTKMOFD0001", "عیار"),
    ("IRTKNAAB0001", "ناب"),
    ("IRTKNAFS0001", "نفیس"),
    ("IRTKNGIN0001", "نگین فارس"),
    ("IRTKRITO0001", "ریتون"),
    ("IRTKROBA0001", "کهربا"),
    ("IRTKROSE0001", "رز ترنج"),
    ("IRTKROZG0001", "رزگلد"),
    ("IRTKTABA0001", "تابش"),
    ("IRTKZARA0001", "مثقال"),
    ("IRTKZARF0001", "زر"),
    ("IRTKZARG0001", "زرگر"),
    ("IRTKZARV0001", "زروان"),
    ("IRTKZFAM0001", "زرفام"),
    ("IRTKZOMR0001", "زمرد"),
    ("IRR", "نقد"),
)

_LABEL_TO_CODE = {label: code for code, label in _ASSET_LABELS if code != "IRR"}
TOP5_CODES_IN = ", ".join(f"'{_LABEL_TO_CODE[s]}'" for s in TOP5_SYMBOLS)


def _sym_map_cte() -> str:
    values = ", ".join(f"('{code}', '{label}')" for code, label in _ASSET_LABELS)
    return f"sym_map AS (SELECT * FROM (VALUES {values}) AS t(asset, label))"


# Earliest selected session + previous same-strategy session (outside $session).
# Pin to one prior id so baseline lookups stay O(one session), not a history scan.
_PRIOR_SESSION_CTE = """
anchor AS (
  SELECT s.id AS session_id, s.started_at, s.strategy_id
  FROM pair_trade.session s
  WHERE s.id IN ($session)
  ORDER BY s.started_at
  LIMIT 1
),
prev_session AS (
  SELECT s.id AS session_id
  FROM pair_trade.session s
  JOIN anchor a ON s.strategy_id = a.strategy_id AND s.started_at < a.started_at
  ORDER BY s.started_at DESC
  LIMIT 1
)"""


def _start_nav_cte(mh_predicate: str) -> str:
    """Last NAV of prior session; else first in-window NAV of selected sessions."""
    return (
        f"{_PRIOR_SESSION_CTE}, "
        f"start_nav AS ("
        f"SELECT COALESCE("
        f"(SELECT p.portfolio_value FROM pair_trade.portfolio_snapshot p "
        f"WHERE p.session_id = (SELECT session_id FROM prev_session) "
        f"ORDER BY p.ts DESC LIMIT 1), "
        f"(SELECT p.portfolio_value FROM pair_trade.portfolio_snapshot p "
        f"WHERE p.session_id IN ($session) AND {mh_predicate} "
        f"ORDER BY p.ts ASC LIMIT 1)"
        f") AS v"
        f")"
    )


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

# Source panel id for Dashboard-datasource sharing (return chart → excess chart).
BENCHMARK_SOURCE_PANEL_ID = 111

SERIES_PORTFOLIO = "Portfolio %"
SERIES_ALL_INDEX = "All index %"
SERIES_TOP5_INDEX = "Top5 index %"
SERIES_EXCESS_ALL = "Excess vs All %"
SERIES_EXCESS_TOP5 = "Excess vs Top5 %"


def benchmark_shared_sql():
    """Portfolio + EW mid indexes from portfolio_snapshot.marks (1‑min), one Postgres hit.

    Gapless trading-time axis. Skips Tehran Thu/Fri (TSE closed) and inserts a null
    between sessions so overnight / weekend is not drawn as a flat connector.
    Baseline = prior-session last NAV / last marks (fallback: first in-window).
    """
    mh = _in_market_hours("p.ts")
    wd = _is_weekday("p.ts")
    cols = (
        f's.port_pct AS "{SERIES_PORTFOLIO}", '
        f's.all_pct AS "{SERIES_ALL_INDEX}", '
        f's.top5_pct AS "{SERIES_TOP5_INDEX}", '
        f's.port_pct - s.all_pct AS "{SERIES_EXCESS_ALL}", '
        f's.port_pct - s.top5_pct AS "{SERIES_EXCESS_TOP5}"'
    )
    # tmap reads FROM src — build plot rows (data + session breaks) as src.
    return f"""
WITH {_PRIOR_SESSION_CTE},
nav0 AS (
  SELECT COALESCE(
    (SELECT p.portfolio_value FROM pair_trade.portfolio_snapshot p
     WHERE p.session_id = (SELECT session_id FROM prev_session)
     ORDER BY p.ts DESC LIMIT 1),
    (SELECT p.portfolio_value FROM pair_trade.portfolio_snapshot p
     WHERE p.session_id IN ($session) AND {mh} AND {_is_weekday("p.ts")}
     ORDER BY p.ts ASC LIMIT 1)
  ) AS v
),
base_marks AS (
  SELECT COALESCE(
    (SELECT p.marks FROM pair_trade.portfolio_snapshot p
     WHERE p.session_id = (SELECT session_id FROM prev_session)
     ORDER BY p.ts DESC LIMIT 1),
    (SELECT p.marks FROM pair_trade.portfolio_snapshot p
     WHERE p.session_id IN ($session) AND {mh} AND {_is_weekday("p.ts")}
     ORDER BY p.ts ASC LIMIT 1)
  ) AS marks
),
m0 AS (
  SELECT split_part(kv.key, '-', 1) AS asset, kv.value::numeric AS m0
  FROM base_marks b
  CROSS JOIN LATERAL jsonb_each_text(b.marks) AS kv(key, value)
  WHERE kv.value::numeric > 0
),
sess_ord AS (
  SELECT s.id AS session_id,
         ROW_NUMBER() OVER (ORDER BY s.started_at) - 1 AS sess_ord
  FROM pair_trade.session s
  WHERE s.id IN ($session)
),
pts AS (
  SELECT p.session_id,
         p.ts AS real_ts,
         100.0 * (p.portfolio_value / NULLIF(n.v, 0) - 1) AS port_pct,
         AVG(100.0 * (kv.value::numeric / NULLIF(m.m0, 0) - 1)) AS all_pct,
         AVG(100.0 * (kv.value::numeric / NULLIF(m.m0, 0) - 1))
           FILTER (WHERE split_part(kv.key, '-', 1) IN ({TOP5_CODES_IN})) AS top5_pct
  FROM pair_trade.portfolio_snapshot p
  CROSS JOIN nav0 n
  CROSS JOIN LATERAL jsonb_each_text(p.marks) AS kv(key, value)
  JOIN m0 m ON m.asset = split_part(kv.key, '-', 1)
  WHERE p.session_id IN ($session)
    AND {mh}
    AND {wd}
    AND kv.value::numeric > 0
  GROUP BY p.session_id, p.ts, p.portfolio_value, n.v
),
src AS (
  SELECT session_id, real_ts, port_pct, all_pct, top5_pct FROM pts
  UNION ALL
  SELECT p.session_id, MAX(p.real_ts) + INTERVAL '1 second', NULL, NULL, NULL
  FROM pts p
  JOIN sess_ord so ON so.session_id = p.session_id
  WHERE so.sess_ord < (SELECT MAX(sess_ord) FROM sess_ord)
  GROUP BY p.session_id
),
{_TMAP_CTE}
SELECT m.time, {cols}
FROM src s
JOIN tmap m ON m.session_id = s.session_id AND m.real_ts = s.real_ts
ORDER BY m.time;
""".strip()


def benchmark_vs_indexes_sql():
    """Alias kept for benches; same body as the shared source query."""
    return benchmark_shared_sql()


# Panel id for cumulative (style-6) return → excess Dashboard-datasource share.
BENCHMARK_CUM_SOURCE_PANEL_ID = 114


def benchmark_cumulative_sql():
    """Multi-session additive cumulative on real wall-clock (style 6).

    Each session’s path is vs its own prior-session close, then shifted by the sum
    of earlier selected sessions’ end port/index/excess %. Overnight gaps remain.
    """
    mh = _in_market_hours("p.ts")
    return f"""
WITH sess_ord AS (
  SELECT s.id AS session_id, s.started_at, s.strategy_id,
         ROW_NUMBER() OVER (ORDER BY s.started_at) - 1 AS sess_ord
  FROM pair_trade.session s
  WHERE s.id IN ($session)
),
sess_prev AS (
  SELECT so.session_id, so.sess_ord,
         (
           SELECT s2.id FROM pair_trade.session s2
           WHERE s2.strategy_id = so.strategy_id AND s2.started_at < so.started_at
           ORDER BY s2.started_at DESC LIMIT 1
         ) AS prev_id
  FROM sess_ord so
),
sess_nav0 AS (
  SELECT sp.session_id,
         COALESCE(
           (SELECT p.portfolio_value FROM pair_trade.portfolio_snapshot p
            WHERE p.session_id = sp.prev_id ORDER BY p.ts DESC LIMIT 1),
           (SELECT p.portfolio_value FROM pair_trade.portfolio_snapshot p
            WHERE p.session_id = sp.session_id AND {mh}
            ORDER BY p.ts ASC LIMIT 1)
         ) AS v
  FROM sess_prev sp
),
sess_marks0 AS (
  SELECT sp.session_id,
         COALESCE(
           (SELECT p.marks FROM pair_trade.portfolio_snapshot p
            WHERE p.session_id = sp.prev_id ORDER BY p.ts DESC LIMIT 1),
           (SELECT p.marks FROM pair_trade.portfolio_snapshot p
            WHERE p.session_id = sp.session_id AND {mh}
            ORDER BY p.ts ASC LIMIT 1)
         ) AS marks
  FROM sess_prev sp
),
m0 AS (
  SELECT sm.session_id, split_part(kv.key, '-', 1) AS asset, kv.value::numeric AS m0
  FROM sess_marks0 sm
  CROSS JOIN LATERAL jsonb_each_text(sm.marks) AS kv(key, value)
  WHERE kv.value::numeric > 0
),
raw AS (
  SELECT p.session_id,
         p.ts AS real_ts,
         100.0 * (p.portfolio_value / NULLIF(n.v, 0) - 1) AS port_pct,
         AVG(100.0 * (kv.value::numeric / NULLIF(m.m0, 0) - 1)) AS all_pct,
         AVG(100.0 * (kv.value::numeric / NULLIF(m.m0, 0) - 1))
           FILTER (WHERE split_part(kv.key, '-', 1) IN ({TOP5_CODES_IN})) AS top5_pct
  FROM pair_trade.portfolio_snapshot p
  JOIN sess_nav0 n ON n.session_id = p.session_id
  CROSS JOIN LATERAL jsonb_each_text(p.marks) AS kv(key, value)
  JOIN m0 m ON m.session_id = p.session_id AND m.asset = split_part(kv.key, '-', 1)
  WHERE p.session_id IN ($session)
    AND {mh}
    AND kv.value::numeric > 0
  GROUP BY p.session_id, p.ts, p.portfolio_value, n.v
),
sess_end AS (
  SELECT DISTINCT ON (r.session_id)
         r.session_id, r.port_pct, r.all_pct, r.top5_pct,
         r.port_pct - r.all_pct AS xs_all,
         r.port_pct - r.top5_pct AS xs_top5
  FROM raw r
  ORDER BY r.session_id, r.real_ts DESC
),
sess_off AS (
  SELECT so.session_id,
         COALESCE(SUM(e.port_pct) OVER (
           ORDER BY so.sess_ord ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
         ), 0) AS port_off,
         COALESCE(SUM(e.all_pct) OVER (
           ORDER BY so.sess_ord ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
         ), 0) AS all_off,
         COALESCE(SUM(e.top5_pct) OVER (
           ORDER BY so.sess_ord ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
         ), 0) AS top5_off,
         COALESCE(SUM(e.xs_all) OVER (
           ORDER BY so.sess_ord ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
         ), 0) AS xs_all_off,
         COALESCE(SUM(e.xs_top5) OVER (
           ORDER BY so.sess_ord ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
         ), 0) AS xs_top5_off
  FROM sess_ord so
  JOIN sess_end e ON e.session_id = so.session_id
)
SELECT r.real_ts AS time,
       r.port_pct + o.port_off AS "{SERIES_PORTFOLIO}",
       r.all_pct + o.all_off AS "{SERIES_ALL_INDEX}",
       r.top5_pct + o.top5_off AS "{SERIES_TOP5_INDEX}",
       (r.port_pct - r.all_pct) + o.xs_all_off AS "{SERIES_EXCESS_ALL}",
       (r.port_pct - r.top5_pct) + o.xs_top5_off AS "{SERIES_EXCESS_TOP5}"
FROM raw r
JOIN sess_off o ON o.session_id = r.session_id
ORDER BY 1;
""".strip()


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
MARKET_SESSION_NOTE = (
    " Only rows with local clock time 12:00–18:00 (Asia/Tehran) are included "
    "(pre-open snapshots excluded)."
)


def color_override(name, fixed_color):
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": fixed_color}}],
    }


def hide_override(name):
    """Hide a series from viz/legend/tooltip (shared query still carries the field)."""
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [{
            "id": "custom.hideFrom",
            "value": {"legend": True, "tooltip": True, "viz": True},
        }],
    }


# Arrow reversed (new←prev): Grafana/Persian RTL renders 'prev→new' backwards.
_ROT_PAIR = "new_symbol || '←' || prev_symbol"


def _rot_base_cte() -> str:
    """One row per rotation with stable +1s nudge for co-timed ts (shared across metric UNIONs)."""
    return f"""
base AS (
  SELECT id, session_id, ts,
         transition_score, last_edge, realized_edge, realized_edge_net,
         sell_slippage, buy_slippage, latency_sec,
         quote_earned, quote_spent,
         ts + ((ROW_NUMBER() OVER (PARTITION BY session_id, ts ORDER BY id) - 1)
               * INTERVAL '1 second') AS time,
         {_ROT_PAIR} AS pair
  FROM pair_trade.rotation
  WHERE {SESS} AND prev_symbol <> 'CASH' AND $__timeFilter(ts)
)"""


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


def pq_dashboard(panel_id, ref="A"):
    """Reuse another panel's query result (Grafana Dashboard datasource — no extra SQL)."""
    return {
        "kind": "PanelQuery",
        "spec": {
            "refId": ref,
            "hidden": False,
            "query": {
                "kind": "DataQuery",
                "group": "dashboard",
                "version": "v0",
                "datasource": {"name": "-- Dashboard --"},
                "spec": {"panelId": panel_id, "withTransforms": False},
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


def ts_viz(
    decimals=2,
    unit=None,
    draw="line",
    fill=10,
    stacking="none",
    overrides=None,
    tooltip_mode="single",
    line_interpolation=None,
    span_nulls=None,
):
    defaults = {
        "decimals": decimals,
        "color": {"mode": "palette-classic"},
        "custom": {
            "drawStyle": draw,
            "fillOpacity": fill,
            "lineWidth": 2 if draw == "line" else 1,
            "showPoints": "never" if draw == "line" else "auto",
            "spanNulls": True if draw == "line" else False if span_nulls is None else span_nulls,
            "stacking": {"group": "A", "mode": stacking},
        },
    }
    if span_nulls is not None:
        defaults["custom"]["spanNulls"] = span_nulls
    if line_interpolation:
        defaults["custom"]["lineInterpolation"] = line_interpolation
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
                    "values": ["percent"],
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

    mh = _in_market_hours("ts")
    wd = _is_weekday("ts")
    # Same gap treatment as non-cum return: market hours + skip Thu/Fri + null between sessions.
    port_value_sql = f"""
WITH sess_ord AS (
  SELECT s.id AS session_id,
         ROW_NUMBER() OVER (ORDER BY s.started_at) - 1 AS sess_ord
  FROM pair_trade.session s
  WHERE s.id IN ($session)
),
pts AS (
  SELECT session_id, ts AS real_ts, portfolio_value / 1e7 AS v
  FROM pair_trade.portfolio_snapshot
  WHERE {SESS} AND {mh} AND {wd}
),
src AS (
  SELECT session_id, real_ts, v FROM pts
  UNION ALL
  SELECT p.session_id, MAX(p.real_ts) + INTERVAL '1 second', NULL
  FROM pts p
  JOIN sess_ord so ON so.session_id = p.session_id
  WHERE so.sess_ord < (SELECT MAX(sess_ord) FROM sess_ord)
  GROUP BY p.session_id
),
{_TMAP_CTE}
SELECT m.time, s.v AS "Portfolio M IRT"
FROM src s
JOIN tmap m ON m.session_id = s.session_id AND m.real_ts = s.real_ts
ORDER BY m.time;
""".strip()

    weights_sql = (
        f"WITH {_sym_map_cte()}, "
        f"src AS ("
        f"SELECT b.session_id, b.ts AS real_ts, "
        f"100.0 * CASE WHEN b.asset = 'IRR' THEN b.total ELSE b.total * COALESCE(("
        f"SELECT kv.value::numeric FROM jsonb_each_text(p.marks) AS kv(key, value) "
        f"WHERE kv.key LIKE b.asset || '-%' LIMIT 1), 0) END / NULLIF(p.portfolio_value, 0) AS value, "
        f"COALESCE(sm.label, b.asset) AS metric "
        f"FROM pair_trade.balance_snapshot b "
        f"JOIN pair_trade.portfolio_snapshot p ON p.session_id = b.session_id AND p.ts = b.ts "
        f"LEFT JOIN sym_map sm ON sm.asset = b.asset "
        f"WHERE b.session_id IN ($session)"
        f"), "
        f"{_TMAP_CTE} "
        f"SELECT m.time, s.value, s.metric FROM src s "
        f"JOIN tmap m ON m.session_id = s.session_id AND m.real_ts = s.real_ts "
        f"ORDER BY m.time;"
    )
    pie_sql = (
        f"WITH {_sym_map_cte()}, "
        f"latest AS (SELECT MAX(ts) AS ts FROM pair_trade.portfolio_snapshot WHERE {SESS}), "
        f"p AS (SELECT ts, portfolio_value, marks FROM pair_trade.portfolio_snapshot "
        f"WHERE {SESS} AND ts = (SELECT ts FROM latest)), "
        f"b AS (SELECT asset, SUM(total) AS total FROM pair_trade.balance_snapshot "
        f"WHERE {SESS} AND ts = (SELECT ts FROM latest) GROUP BY asset), "
        f"valued AS ("
        f"SELECT p.ts AS time, "
        f"COALESCE(sm.label, b.asset) AS metric, "
        f"100.0 * CASE WHEN b.asset = 'IRR' THEN b.total ELSE b.total * COALESCE(("
        f"SELECT kv.value::numeric FROM jsonb_each_text(p.marks) AS kv(key, value) "
        f"WHERE kv.key = b.asset || '-IRR' OR kv.key LIKE b.asset || '-%' LIMIT 1), 0) END "
        f"/ NULLIF(p.portfolio_value, 0) AS value "
        f"FROM b CROSS JOIN p LEFT JOIN sym_map sm ON sm.asset = b.asset"
        f") "
        f"SELECT time, value, metric FROM valued WHERE value > 0 ORDER BY value DESC;"
    )
    # Wall-clock +1s nudge for co-timed rotations. Identify pairs via latency legend/hover.
    _base = _rot_base_cte()
    edges_sql = (
        f"WITH {_base} "
        f"SELECT time, transition_score * 10000 AS value, 'expected' AS metric FROM base "
        f"WHERE transition_score IS NOT NULL "
        f"UNION ALL SELECT time, last_edge * 10000, 'last' FROM base "
        f"WHERE last_edge IS NOT NULL "
        f"UNION ALL SELECT time, realized_edge * 10000, 'realized' FROM base "
        f"WHERE realized_edge IS NOT NULL "
        f"UNION ALL SELECT time, realized_edge_net * 10000, 'realized_net' FROM base "
        f"WHERE realized_edge_net IS NOT NULL "
        f"ORDER BY 1;"
    )
    slip_sql = (
        f"WITH {_base} "
        f"SELECT time, sell_slippage * 10000 AS value, 'sell_slippage_bps' AS metric FROM base "
        f"WHERE sell_slippage IS NOT NULL "
        f"UNION ALL SELECT time, buy_slippage * 10000, 'buy_slippage_bps' FROM base "
        f"WHERE buy_slippage IS NOT NULL "
        f"ORDER BY 1;"
    )
    # Latency: pair is the series name so legend/hover identify the trade.
    latency_sql = (
        f"WITH {_base} "
        f"SELECT time, latency_sec AS value, pair AS metric FROM base "
        f"WHERE latency_sec IS NOT NULL "
        f"ORDER BY 1;"
    )
    # NAV-weighted: contrib_bps = edge_bps * notional/NAV so small trades don't dominate.
    # notional = quote_earned (fallback quote_spent); NAV = prior-session close (same as P&L %).
    # Anchor 0 at $__timeFrom and hold last cum at min(now, $__timeTo) (use stepAfter on panel).
    cum_edge_sql = (
        f"WITH {_start_nav_cte('TRUE')}, "
        f"{_base}, "
        f"w AS ("
        f"SELECT b.time, b.session_id, b.ts, b.id, "
        f"b.realized_edge * 10000 "
        f"* COALESCE(NULLIF(b.quote_earned, 0), b.quote_spent) / NULLIF(n.v, 0) AS contrib_bps, "
        f"b.realized_edge_net * 10000 "
        f"* COALESCE(NULLIF(b.quote_earned, 0), b.quote_spent) / NULLIF(n.v, 0) AS contrib_net_bps "
        f"FROM base b CROSS JOIN start_nav n"
        f"), "
        f"cum AS ("
        f"SELECT time, session_id, ts, id, "
        f"SUM(contrib_bps) OVER ("
        f"PARTITION BY session_id ORDER BY ts, id "
        f"ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS realized, "
        f"SUM(contrib_net_bps) OVER ("
        f"PARTITION BY session_id ORDER BY ts, id "
        f"ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS realized_net "
        f"FROM w WHERE contrib_bps IS NOT NULL OR contrib_net_bps IS NOT NULL"
        f"), "
        f"bounds AS ("
        f"SELECT to_timestamp($__unixEpochFrom()) AS t0, "
        f"LEAST(NOW(), to_timestamp($__unixEpochTo())) AS t1"
        f"), "
        f"last AS ("
        f"SELECT COALESCE(SUM(realized), 0) AS realized, "
        f"COALESCE(SUM(realized_net), 0) AS realized_net "
        f"FROM ("
        f"SELECT DISTINCT ON (session_id) realized, realized_net "
        f"FROM cum ORDER BY session_id, ts DESC, id DESC"
        f") s"
        f") "
        f"SELECT b.t0 AS time, 0::float AS value, "
        f"'cum realized (NAV-wtd bps)' AS metric FROM bounds b "
        f"UNION ALL SELECT c.time, c.realized, 'cum realized (NAV-wtd bps)' "
        f"FROM cum c WHERE c.realized IS NOT NULL "
        f"UNION ALL SELECT b.t1, l.realized, 'cum realized (NAV-wtd bps)' "
        f"FROM bounds b CROSS JOIN last l "
        f"UNION ALL SELECT b.t0, 0::float, 'cum realized_net (NAV-wtd bps)' FROM bounds b "
        f"UNION ALL SELECT c.time, c.realized_net, 'cum realized_net (NAV-wtd bps)' "
        f"FROM cum c WHERE c.realized_net IS NOT NULL "
        f"UNION ALL SELECT b.t1, l.realized_net, 'cum realized_net (NAV-wtd bps)' "
        f"FROM bounds b CROSS JOIN last l "
        f"ORDER BY 1;"
    )

    elements = {
        "panel-1b": panel(
            101,
            "P&L (%)",
            "100 * (latest_NAV / prior_close_NAV − 1); prior-session last NAV shared baseline.",
            qg([pq(
                f"WITH {_start_nav_cte('TRUE')}, "
                f"latest AS ("
                f"SELECT portfolio_value FROM pair_trade.portfolio_snapshot "
                f"WHERE {SESS} ORDER BY ts DESC LIMIT 1) "
                f"SELECT 100.0 * (latest.portfolio_value / NULLIF(start_nav.v, 0) - 1) AS pnl_pct "
                f"FROM latest, start_nav;",
                "table",
            )]),
            stat_viz(2, unit="percent", thresholds=red_green),
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
        "panel-7": panel(
            7,
            "Turnover (×)",
            "Sum(fill notional) / latest NAV. Both buy+sell legs count: ≈2 ≈ one full book renew "
            "(sold once + bought once). From pair_trade.fill.",
            qg([pq(
                f"WITH latest AS ("
                f"SELECT portfolio_value AS v FROM pair_trade.portfolio_snapshot "
                f"WHERE {SESS} ORDER BY ts DESC LIMIT 1"
                f") "
                f"SELECT COALESCE(("
                f"SELECT SUM(f.amount * f.price) FROM pair_trade.fill f WHERE f.session_id IN ($session)"
                f"), 0) / NULLIF(latest.v, 0) AS turnover FROM latest;",
                "table",
            )]),
            stat_viz(2, graph_mode="none"),
        ),
        "panel-8": panel(
            8,
            "Commission / NAV (%)",
            "100 × sum(fill.fee_quote) / latest NAV — total commission as % of portfolio.",
            qg([pq(
                f"WITH latest AS ("
                f"SELECT portfolio_value AS v FROM pair_trade.portfolio_snapshot "
                f"WHERE {SESS} ORDER BY ts DESC LIMIT 1"
                f") "
                f"SELECT 100.0 * COALESCE(("
                f"SELECT SUM(f.fee_quote) FROM pair_trade.fill f WHERE f.session_id IN ($session)"
                f"), 0) / NULLIF(latest.v, 0) AS commission_pct FROM latest;",
                "table",
            )]),
            stat_viz(3, unit="percent", graph_mode="none"),
        ),
        "panel-11": panel(
            11,
            "Portfolio value (M IRT)",
            "Mark-to-mid NAV over trading-time from portfolio_snapshot. "
            "Skips Tehran Thu/Fri; null between sessions (no overnight/weekend flat connector)."
            + MARKET_SESSION_NOTE
            + GAPLESS_NOTE,
            qg([pq(port_value_sql)]),
            ts_viz(2, fill=10, span_nulls=False),
        ),
        "panel-10c": panel(
            BENCHMARK_SOURCE_PANEL_ID,
            "Portfolio return vs benchmark indexes (%)",
            "Portfolio % vs equal-weight mid indexes from portfolio_snapshot.marks (1‑min). "
            "Shared baseline = prior-session last NAV / last marks; fallback = first in-window. "
            "All = every mark key; Top5 = عیار، طلا، کهربا، گنج، مثقال. "
            "Skips Tehran Thu/Fri; null between sessions (no overnight/weekend flat connector). "
            "Source query for the excess panel (Dashboard datasource)."
            + MARKET_SESSION_NOTE
            + GAPLESS_NOTE,
            qg([pq(benchmark_shared_sql())]),
            ts_viz(
                3,
                unit="percent",
                fill=10,
                tooltip_mode="multi",
                span_nulls=False,
                overrides=[
                    color_override(SERIES_PORTFOLIO, COLOR_PORTFOLIO),
                    color_override(SERIES_ALL_INDEX, COLOR_ALL_INDEX),
                    color_override(SERIES_TOP5_INDEX, COLOR_TOP5_INDEX),
                    hide_override(SERIES_EXCESS_ALL),
                    hide_override(SERIES_EXCESS_TOP5),
                ],
            ),
        ),
        "panel-10d": panel(
            113,
            "Portfolio excess return vs indexes (%)",
            "Portfolio % minus each equal-weight mid index % (shared prior-session close baselines). "
            "Positive = outperforming that benchmark. Reuses the return panel query via Grafana "
            "Dashboard datasource (no second Postgres round-trip). Same Thu/Fri + session-break rules."
            + MARKET_SESSION_NOTE
            + GAPLESS_NOTE,
            qg([pq_dashboard(BENCHMARK_SOURCE_PANEL_ID)]),
            ts_viz(
                3,
                unit="percent",
                fill=15,
                tooltip_mode="multi",
                span_nulls=False,
                overrides=[
                    color_override(SERIES_EXCESS_ALL, COLOR_ALL_INDEX),
                    color_override(SERIES_EXCESS_TOP5, COLOR_TOP5_INDEX),
                    hide_override(SERIES_PORTFOLIO),
                    hide_override(SERIES_ALL_INDEX),
                    hide_override(SERIES_TOP5_INDEX),
                ],
            ),
        ),
        "panel-10c-cum": panel(
            BENCHMARK_CUM_SOURCE_PANEL_ID,
            "Portfolio return vs benchmark indexes (%) — cumulative",
            "Style 6: all selected sessions on real wall-clock. Each day is shifted by the sum of "
            "earlier sessions’ end port/index % (vs that session’s prior close). Additive cumulative "
            "from the first selected session. Line interpolation = stepAfter (holds across gaps)."
            + MARKET_SESSION_NOTE,
            qg([pq(benchmark_cumulative_sql())]),
            ts_viz(
                3,
                unit="percent",
                fill=10,
                tooltip_mode="multi",
                line_interpolation="stepAfter",
                span_nulls=True,
                overrides=[
                    color_override(SERIES_PORTFOLIO, COLOR_PORTFOLIO),
                    color_override(SERIES_ALL_INDEX, COLOR_ALL_INDEX),
                    color_override(SERIES_TOP5_INDEX, COLOR_TOP5_INDEX),
                    hide_override(SERIES_EXCESS_ALL),
                    hide_override(SERIES_EXCESS_TOP5),
                ],
            ),
        ),
        "panel-10d-cum": panel(
            115,
            "Portfolio excess return vs indexes (%) — cumulative",
            "Style 6: cumulative excess = port − index with per-session baselines + sum of earlier "
            "sessions’ end excess. stepAfter line (continuous across session gaps). "
            "Reuses the cumulative return panel query."
            + MARKET_SESSION_NOTE,
            qg([pq_dashboard(BENCHMARK_CUM_SOURCE_PANEL_ID)]),
            ts_viz(
                3,
                unit="percent",
                fill=15,
                tooltip_mode="multi",
                line_interpolation="stepAfter",
                span_nulls=True,
                overrides=[
                    color_override(SERIES_EXCESS_ALL, COLOR_ALL_INDEX),
                    color_override(SERIES_EXCESS_TOP5, COLOR_TOP5_INDEX),
                    hide_override(SERIES_PORTFOLIO),
                    hide_override(SERIES_ALL_INDEX),
                    hide_override(SERIES_TOP5_INDEX),
                ],
            ),
        ),
        "panel-12": panel(
            12,
            "Portfolio weights by asset (%)",
            "Share of mark-to-mid NAV per asset (Persian labels via static IR map; نقد = cash). "
            "Stacked area ≈ 100%." + GAPLESS_NOTE,
            qg([pq(weights_sql)]),
            ts_viz(2, unit="percent", fill=40, stacking="normal", tooltip_mode="multi"),
        ),
        "panel-12b": panel(
            112,
            "Latest portfolio composition (%)",
            "Pie of latest asset weights by mark-to-mid value. Persian labels via static IR map; نقد = cash.",
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
        "panel-20": panel(
            20,
            "Return signal by symbol",
            "EWM log-mid residual (signal_snapshot.ret). "
            "Last selected session only (newest started_at)." + GAPLESS_NOTE,
            qg([pq(gapless_metric_sql(
                f"SELECT session_id, bin_ts AS real_ts, ret AS value, symbol AS metric "
                f"FROM pair_trade.signal_snapshot WHERE {LAST_SESS} AND ret IS NOT NULL"
            ))]),
            ts_viz(4, fill=0),
        ),
        "panel-20b": panel(
            120,
            "Return signal demeaned (cross-section)",
            "ret − mean(ret) per bin_ts across symbols. "
            "Last selected session only (newest started_at)." + GAPLESS_NOTE,
            qg([pq(gapless_metric_sql(
                f"SELECT session_id, bin_ts AS real_ts, "
                f"ret - AVG(ret) OVER (PARTITION BY session_id, bin_ts) AS value, symbol AS metric "
                f"FROM pair_trade.signal_snapshot WHERE {LAST_SESS} AND ret IS NOT NULL"
            ))]),
            ts_viz(4, fill=0),
        ),
        "panel-41": panel(
            41,
            "Rotation edges (bps)",
            "Sell→buy only. expected/last/realized/realized_net ×10000 ≈ bps. "
            "Co-timed rotations nudged +1s. Wall-clock x-axis.",
            qg([pq(edges_sql)]),
            ts_viz(1, draw="bars", fill=70, tooltip_mode="multi"),
        ),
        "panel-42": panel(
            42,
            "Buy / sell slippage (bps)",
            "Sell→buy slippage ×10000 ≈ bps. Co-timed rotations nudged +1s. Wall-clock x-axis.",
            qg([pq(slip_sql)]),
            ts_viz(1, draw="bars", fill=70, tooltip_mode="multi"),
        ),
        "panel-43": panel(
            43,
            "Rotation latency (sec)",
            "Seconds from sell placed to buy_complete. "
            "Series name = pair (new←prev); co-timed rotations nudged +1s. Wall-clock x-axis.",
            qg([pq(latency_sql)]),
            ts_viz(1, unit="s", draw="bars", fill=70, tooltip_mode="multi"),
        ),
        "panel-44": panel(
            44,
            "Cumulative realized edge (NAV-weighted bps)",
            "Running sum of contrib_bps = realized_edge×10000×notional/NAV "
            "(notional=quote_earned, NAV=prior-session close). "
            "Starts at 0 ($__timeFrom); last value held to now. stepAfter. Wall-clock x-axis.",
            qg([pq(cum_edge_sql)]),
            ts_viz(1, fill=15, tooltip_mode="multi", line_interpolation="stepAfter"),
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
        "panel-21": panel(
            21,
            "Bid-ask spread % by symbol",
            "BA cost as percent (×100). "
            "Last selected session only (newest started_at)." + GAPLESS_NOTE,
            qg([pq(gapless_metric_sql(
                f"SELECT session_id, bin_ts AS real_ts, ba_spread * 100 AS value, symbol AS metric "
                f"FROM pair_trade.signal_snapshot WHERE {LAST_SESS} AND ba_spread IS NOT NULL"
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
    }

    # Portfolio value TS + 2×2 KPIs; per-session benchmarks; then style-6 cumulative pair.
    layout_items = [
        gird(0, 0, 12, 8, "panel-11"),
        gird(12, 0, 6, 4, "panel-1b"),
        gird(18, 0, 6, 4, "panel-7"),
        gird(12, 4, 6, 4, "panel-2"),
        gird(18, 4, 6, 4, "panel-8"),
        gird(0, 8, 12, 8, "panel-10c"),
        gird(12, 8, 12, 8, "panel-10d"),
        gird(0, 16, 12, 8, "panel-10c-cum"),
        gird(12, 16, 12, 8, "panel-10d-cum"),
        gird(0, 24, 16, 8, "panel-12"),
        gird(16, 24, 8, 8, "panel-12b"),
        gird(0, 32, 12, 8, "panel-13"),
        gird(12, 32, 12, 8, "panel-14"),
        gird(0, 40, 12, 8, "panel-20"),
        gird(12, 40, 12, 8, "panel-20b"),
        gird(0, 48, 12, 8, "panel-41"),
        gird(12, 48, 12, 8, "panel-42"),
        gird(0, 56, 12, 7, "panel-43"),
        gird(12, 56, 12, 7, "panel-44"),
        gird(0, 63, 12, 8, "panel-30"),
        gird(0, 71, 12, 8, "panel-21"),
        gird(12, 71, 12, 8, "panel-22"),
    ]

    dashboard = {
        "apiVersion": "dashboard.grafana.app/v2",
        "kind": "Dashboard",
        "metadata": {
            "name": "hummingbot-pair-trade-mobin-v7",
            "namespace": "default",
            "uid": "hb-pair-trade-mobin-v7",
        },
        "spec": {
            "title": "Hummingbot Pair Trade (Mobin Gold) v7",
            "description": (
                "v7 trimmed: prior-session-close P&L baseline; default session = latest; "
                "default time = today 12:02 → now (Asia/Tehran). Weight labels = static IR→Persian map. "
                "Gapless 12:00–18:00 Asia/Tehran. Benchmark panels kept (optimize later)."
            ),
            "tags": ["hummingbot", "pair-trade", "mobin", "gold-etf", "v7"],
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
                # Start of Tehran calendar day + 12h2m → 12:02 local TSE morning.
                "timezone": "Asia/Tehran",
                "from": "now/d+12h+2m",
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
                        # False → Grafana picks first option = latest (ORDER BY started_at DESC).
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
