#!/usr/bin/env python3
"""Generate x-axis strategy comparison dashboard (test only).

Five axis strategies × three charts (return vs indexes, excess, portfolio value):
  1. Real wall-clock ts (honest labels; overnight gaps visible)
  2. Gapless synthetic time + Wall clock (ms) in tooltip
  3. Overlay: all sessions mapped onto 2000-01-01 + Tehran time-of-day
  4. Concat: session N → 2000-01-(1+N) + Tehran time-of-day
  5. Real wall-clock, one series per session_id (repeat-by-series)

Import into Grafana alongside v7; uid: hb-pair-trade-xaxis-test
"""
from __future__ import annotations

import json
from pathlib import Path

import _gen_dashboard_v7 as v7

OUT = Path(__file__).with_name("hummingbot-pair-trade-xaxis-test.json")
UID = "hb-pair-trade-xaxis-test"
NAME = "hummingbot-pair-trade-xaxis-test"

SERIES_PORT = v7.SERIES_PORTFOLIO
SERIES_ALL = v7.SERIES_ALL_INDEX
SERIES_TOP5 = v7.SERIES_TOP5_INDEX
SERIES_XS_ALL = v7.SERIES_EXCESS_ALL
SERIES_XS_TOP5 = v7.SERIES_EXCESS_TOP5

TZ = v7.MARKET_TZ


def _benchmark_core_ctes() -> str:
    """nav0 / m0 / expanded marks rows (shared by all axis modes)."""
    mh = v7._in_market_hours("p.ts")
    return f"""
{v7._PRIOR_SESSION_CTE},
nav0 AS (
  SELECT COALESCE(
    (SELECT p.portfolio_value FROM pair_trade.portfolio_snapshot p
     WHERE p.session_id = (SELECT session_id FROM prev_session)
     ORDER BY p.ts DESC LIMIT 1),
    (SELECT p.portfolio_value FROM pair_trade.portfolio_snapshot p
     WHERE p.session_id IN ($session) AND {mh}
     ORDER BY p.ts ASC LIMIT 1)
  ) AS v
),
base_marks AS (
  SELECT COALESCE(
    (SELECT p.marks FROM pair_trade.portfolio_snapshot p
     WHERE p.session_id = (SELECT session_id FROM prev_session)
     ORDER BY p.ts DESC LIMIT 1),
    (SELECT p.marks FROM pair_trade.portfolio_snapshot p
     WHERE p.session_id IN ($session) AND {mh}
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
raw AS (
  SELECT p.session_id,
         p.ts AS real_ts,
         100.0 * (p.portfolio_value / NULLIF(n.v, 0) - 1) AS port_pct,
         AVG(100.0 * (kv.value::numeric / NULLIF(m.m0, 0) - 1)) AS all_pct,
         AVG(100.0 * (kv.value::numeric / NULLIF(m.m0, 0) - 1))
           FILTER (WHERE split_part(kv.key, '-', 1) IN ({v7.TOP5_CODES_IN})) AS top5_pct
  FROM pair_trade.portfolio_snapshot p
  CROSS JOIN nav0 n
  CROSS JOIN LATERAL jsonb_each_text(p.marks) AS kv(key, value)
  JOIN m0 m ON m.asset = split_part(kv.key, '-', 1)
  WHERE p.session_id IN ($session)
    AND {mh}
    AND kv.value::numeric > 0
  GROUP BY p.session_id, p.ts, p.portfolio_value, n.v
)"""


def _port_raw_cte() -> str:
    mh = v7._in_market_hours("p.ts")
    return f"""
sess_ord AS (
  SELECT s.id AS session_id,
         ROW_NUMBER() OVER (ORDER BY s.started_at) - 1 AS sess_ord
  FROM pair_trade.session s
  WHERE s.id IN ($session)
),
raw AS (
  SELECT p.session_id, p.ts AS real_ts, p.portfolio_value / 1e7 AS port_m
  FROM pair_trade.portfolio_snapshot p
  WHERE p.session_id IN ($session) AND {mh}
)"""


def _time_expr(mode: int) -> str:
    """SQL expression producing timestamptz `time` from raw.real_ts (+ sess_ord).

    Modes 3–4 anchor into [$__unixEpochFrom, $__unixEpochTo] so the dashboard
    time picker always contains the points (no year-2000 / Zoom-to-data issue).
    """
    # Seconds since Tehran market open (12:00); clamp at 0.
    sec_from_open = (
        f"GREATEST(0, EXTRACT(EPOCH FROM ("
        f"(raw.real_ts AT TIME ZONE '{TZ}')::time - TIME '{v7.MARKET_OPEN}'"
        f")))"
    )
    # Full regular session length in seconds (12:00–18:00 → 21600).
    sess_len = (
        f"EXTRACT(EPOCH FROM (TIME '{v7.MARKET_CLOSE}' - TIME '{v7.MARKET_OPEN}'))"
    )
    win = "($__unixEpochTo() - $__unixEpochFrom())"
    if mode == 1 or mode == 5:
        return "raw.real_ts"
    if mode == 2:
        return "raw.real_ts"
    if mode == 3:
        # Overlay: all sessions start at dashboard From; clock = seconds since 12:00.
        return (
            f"to_timestamp($__unixEpochFrom()) "
            f"+ ({sec_from_open}) * INTERVAL '1 second'"
        )
    if mode == 4:
        # Concat: split dashboard window into N equal slices (N = selected sessions);
        # within each slice, map 12:00–18:00 linearly into that slice.
        n_sess = "GREATEST((SELECT COUNT(*) FROM sess_ord), 1)"
        slice_sec = f"(({win})::double precision / {n_sess})"
        return (
            f"to_timestamp($__unixEpochFrom()) "
            f"+ (so.sess_ord * {slice_sec}) * INTERVAL '1 second' "
            f"+ (({sec_from_open}) / NULLIF({sess_len}, 0) * {slice_sec}) "
            f"* INTERVAL '1 second'"
        )
    raise ValueError(mode)


def _gapless_tmap() -> str:
    return """
tmap AS (
  SELECT d.session_id,
         d.real_ts,
         to_timestamp($__unixEpochFrom())
           + (ROW_NUMBER() OVER (ORDER BY s.started_at, d.real_ts) - 1)
           * (($__unixEpochTo() - $__unixEpochFrom())
              / GREATEST(COUNT(*) OVER (), 1))
           * INTERVAL '1 second' AS time
  FROM (SELECT DISTINCT session_id, real_ts FROM raw) d
  JOIN pair_trade.session s ON s.id = d.session_id
)"""


def benchmark_sql(mode: int) -> str:
    core = _benchmark_core_ctes()
    if mode == 2:
        return f"""
WITH {core},
{_gapless_tmap()}
SELECT m.time,
       r.port_pct AS "{SERIES_PORT}",
       r.all_pct AS "{SERIES_ALL}",
       r.top5_pct AS "{SERIES_TOP5}",
       r.port_pct - r.all_pct AS "{SERIES_XS_ALL}",
       r.port_pct - r.top5_pct AS "{SERIES_XS_TOP5}",
       (EXTRACT(EPOCH FROM r.real_ts) * 1000)::bigint AS "Wall clock"
FROM raw r
JOIN tmap m ON m.session_id = r.session_id AND m.real_ts = r.real_ts
ORDER BY m.time;
""".strip()

    if mode == 5:
        # Long format: one series family per session (readable multi-session).
        return f"""
WITH {core}
SELECT raw.real_ts AS time,
       raw.port_pct AS value,
       raw.session_id::text || ' {SERIES_PORT}' AS metric
FROM raw
UNION ALL
SELECT raw.real_ts, raw.all_pct, raw.session_id::text || ' {SERIES_ALL}'
FROM raw
UNION ALL
SELECT raw.real_ts, raw.top5_pct, raw.session_id::text || ' {SERIES_TOP5}'
FROM raw
ORDER BY 1;
""".strip()

    te = _time_expr(mode)
    return f"""
WITH {core}
SELECT {te} AS time,
       raw.port_pct AS "{SERIES_PORT}",
       raw.all_pct AS "{SERIES_ALL}",
       raw.top5_pct AS "{SERIES_TOP5}",
       raw.port_pct - raw.all_pct AS "{SERIES_XS_ALL}",
       raw.port_pct - raw.top5_pct AS "{SERIES_XS_TOP5}"
FROM raw
JOIN sess_ord so ON so.session_id = raw.session_id
ORDER BY 1;
""".strip()


def excess_sql_from_wide(mode: int) -> str:
    """Same as benchmark wide query — panel hides return series (modes 1–4)."""
    return benchmark_sql(mode)


def excess_sql_mode5() -> str:
    return f"""
WITH {_benchmark_core_ctes()}
SELECT raw.real_ts AS time,
       raw.port_pct - raw.all_pct AS value,
       raw.session_id::text || ' {SERIES_XS_ALL}' AS metric
FROM raw
UNION ALL
SELECT raw.real_ts, raw.port_pct - raw.top5_pct,
       raw.session_id::text || ' {SERIES_XS_TOP5}'
FROM raw
ORDER BY 1;
""".strip()


def _mode6_accum_ctes() -> str:
    """Per-session baselines + running offset from earlier sessions' end returns.

    Every selected session is plotted on real wall-clock time. Within session k,
    Y = (return vs that session's prior close) + sum(end returns of sessions 0..k-1).
    So day 1 starts ~0, day 2 continues from day 1's end, etc. (additive cumulative).
    """
    mh = v7._in_market_hours("p.ts")
    return f"""
sess_ord AS (
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
           FILTER (WHERE split_part(kv.key, '-', 1) IN ({v7.TOP5_CODES_IN})) AS top5_pct
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
-- Offset for session k = sum of end-% of all earlier selected sessions (cumulative).
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
)"""


def benchmark_sql_mode6() -> str:
    return f"""
WITH {_mode6_accum_ctes()}
SELECT r.real_ts AS time,
       r.port_pct + o.port_off AS "{SERIES_PORT}",
       r.all_pct + o.all_off AS "{SERIES_ALL}",
       r.top5_pct + o.top5_off AS "{SERIES_TOP5}",
       (r.port_pct - r.all_pct) + o.xs_all_off AS "{SERIES_XS_ALL}",
       (r.port_pct - r.top5_pct) + o.xs_top5_off AS "{SERIES_XS_TOP5}"
FROM raw r
JOIN sess_off o ON o.session_id = r.session_id
ORDER BY 1;
""".strip()


def portfolio_sql_mode6() -> str:
    """All selected sessions on real wall-clock (NAV level — no % stack)."""
    mh = v7._in_market_hours("p.ts")
    return f"""
SELECT p.ts AS time,
       p.portfolio_value / 1e7 AS "Portfolio M IRT"
FROM pair_trade.portfolio_snapshot p
WHERE p.session_id IN ($session)
  AND {mh}
ORDER BY 1;
""".strip()


def portfolio_sql(mode: int) -> str:
    core = _port_raw_cte()
    if mode == 2:
        return f"""
WITH {core},
{_gapless_tmap()}
SELECT m.time,
       r.port_m AS "Portfolio M IRT",
       (EXTRACT(EPOCH FROM r.real_ts) * 1000)::bigint AS "Wall clock"
FROM raw r
JOIN tmap m ON m.session_id = r.session_id AND m.real_ts = r.real_ts
ORDER BY m.time;
""".strip()
    if mode == 5:
        return f"""
WITH {core}
SELECT raw.real_ts AS time,
       raw.port_m AS value,
       raw.session_id::text || ' Portfolio M IRT' AS metric
FROM raw
ORDER BY 1;
""".strip()
    te = _time_expr(mode)
    return f"""
WITH {core}
SELECT {te} AS time,
       raw.port_m AS "Portfolio M IRT"
FROM raw
JOIN sess_ord so ON so.session_id = raw.session_id
ORDER BY 1;
""".strip()


def wall_clock_override():
    return {
        "matcher": {"id": "byName", "options": "Wall clock"},
        "properties": [
            {"id": "unit", "value": "dateTimeAsIso"},
            {"id": "custom.hideFrom", "value": {"legend": True, "tooltip": False, "viz": True}},
            {"id": "decimals", "value": 0},
        ],
    }


def mode_bench_overrides(mode: int, kind: str):
    """kind: 'return' | 'excess' | 'port'."""
    ovr = []
    if kind == "return":
        ovr = [
            v7.color_override(SERIES_PORT, v7.COLOR_PORTFOLIO),
            v7.color_override(SERIES_ALL, v7.COLOR_ALL_INDEX),
            v7.color_override(SERIES_TOP5, v7.COLOR_TOP5_INDEX),
            v7.hide_override(SERIES_XS_ALL),
            v7.hide_override(SERIES_XS_TOP5),
        ]
    elif kind == "excess":
        ovr = [
            v7.color_override(SERIES_XS_ALL, v7.COLOR_ALL_INDEX),
            v7.color_override(SERIES_XS_TOP5, v7.COLOR_TOP5_INDEX),
            v7.hide_override(SERIES_PORT),
            v7.hide_override(SERIES_ALL),
            v7.hide_override(SERIES_TOP5),
        ]
    if mode == 2:
        ovr = list(ovr) + [wall_clock_override()]
    return ovr


MODE_META = [
    (1, "1. Real time", "Wall-clock ts. Honest labels; overnight/weekend gaps visible."),
    (2, "2. Gapless + tooltip", "Synthetic gapless x-axis; hover Wall clock = real Tehran time (ISO)."),
    (3, "3. Overlay trading day",
     "All sessions overlay from $__unixEpochFrom; x = From + seconds since Tehran 12:00."),
    (4, "4. Concat session days",
     "Sessions share [$__unixEpochFrom,$__unixEpochTo] in equal slices; TOD 12:00–18:00 maps into each slice."),
    (5, "5. Real time × session", "Wall-clock; one series per session_id (repeat-by-series)."),
    (6, "6. Cumulative across sessions",
     "All selected sessions on real wall-clock. Each day’s path is shifted by the sum of "
     "earlier sessions’ end port/index/excess % (vs each session’s own prior close), so the "
     "series is a continuous additive cumulative from the first selected session."),
]


def main():
    elements = {}
    layout = []
    pid = 1
    y = 0

    for mode, title_prefix, desc in MODE_META:
        # text-like description via panel titles; three charts at 8 high
        ret_name = f"m{mode}-ret"
        xs_name = f"m{mode}-xs"
        port_name = f"m{mode}-port"
        ret_id = 100 + mode * 10
        xs_id = 101 + mode * 10
        port_id = 102 + mode * 10

        if mode == 5:
            ret_sql = benchmark_sql(5)
            xs_sql = excess_sql_mode5()
            port_sql = portfolio_sql(5)
            ret_ovr = []
            xs_ovr = []
            port_ovr = []
            tip = "multi"
        elif mode == 6:
            ret_sql = benchmark_sql_mode6()
            xs_sql = benchmark_sql_mode6()
            port_sql = portfolio_sql_mode6()
            ret_ovr = mode_bench_overrides(6, "return")
            xs_ovr = mode_bench_overrides(6, "excess")
            port_ovr = []
            tip = "multi"
        else:
            ret_sql = benchmark_sql(mode)
            xs_sql = excess_sql_from_wide(mode)
            port_sql = portfolio_sql(mode)
            ret_ovr = mode_bench_overrides(mode, "return")
            xs_ovr = mode_bench_overrides(mode, "excess")
            port_ovr = mode_bench_overrides(mode, "port")
            tip = "multi"

        elements[ret_name] = v7.panel(
            ret_id,
            f"{title_prefix} — Portfolio return vs benchmark indexes (%)",
            desc + " " + v7.MARKET_SESSION_NOTE,
            v7.qg([v7.pq(ret_sql)]),
            v7.ts_viz(
                3,
                unit="percent",
                fill=10,
                tooltip_mode=tip,
                overrides=ret_ovr,
                **({"line_interpolation": "stepAfter", "span_nulls": True} if mode == 6 else {}),
            ),
        )
        elements[xs_name] = v7.panel(
            xs_id,
            f"{title_prefix} — Portfolio excess return vs indexes (%)",
            desc + " Excess = port − index (same baseline as return panel)." + v7.MARKET_SESSION_NOTE,
            v7.qg([v7.pq(xs_sql)]),
            v7.ts_viz(
                3,
                unit="percent",
                fill=15,
                tooltip_mode=tip,
                overrides=xs_ovr,
                **({"line_interpolation": "stepAfter", "span_nulls": True} if mode == 6 else {}),
            ),
        )
        elements[port_name] = v7.panel(
            port_id,
            f"{title_prefix} — Portfolio value (M IRT)",
            desc + v7.MARKET_SESSION_NOTE,
            v7.qg([v7.pq(port_sql)]),
            v7.ts_viz(2, fill=10, tooltip_mode=tip, overrides=port_ovr),
        )

        layout.append(v7.gird(0, y, 8, 8, ret_name))
        layout.append(v7.gird(8, y, 8, 8, xs_name))
        layout.append(v7.gird(16, y, 8, 8, port_name))
        y += 8
        pid += 3

    dashboard = {
        "apiVersion": "dashboard.grafana.app/v2",
        "kind": "Dashboard",
        "metadata": {"name": NAME, "namespace": "default", "uid": UID},
        "spec": {
            "title": "X-axis strategies test (Mobin Gold)",
            "description": (
                "Compare five x-axis strategies for multi-session charts. "
                "Only return-vs-index, excess, and portfolio value. "
                "Select multiple sessions to see differences."
            ),
            "tags": ["hummingbot", "pair-trade", "mobin", "xaxis-test"],
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
                            "group": v7.GROUP,
                            "version": "v0",
                            "datasource": {"name": v7.DS},
                            "spec": {
                                "__legacyStringValue": (
                                    "SELECT name AS __text, name AS __value "
                                    "FROM pair_trade.strategy ORDER BY name;"
                                )
                            },
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
                        "includeAll": False,
                        "allowCustomValue": True,
                        "sort": "disabled",
                        "regex": "",
                        "regexApplyTo": "value",
                        "options": [],
                        "current": {"text": "", "value": ""},
                        "query": {
                            "kind": "DataQuery",
                            "group": v7.GROUP,
                            "version": "v0",
                            "datasource": {"name": v7.DS},
                            "spec": {
                                "__legacyStringValue": (
                                    "SELECT s.id::text || ' @ ' || to_char(s.started_at, 'YYYY-MM-DD HH24:MI') "
                                    "AS __text, s.id AS __value "
                                    "FROM pair_trade.session s "
                                    "JOIN pair_trade.strategy st ON st.id = s.strategy_id "
                                    "WHERE st.name = '$strategy' ORDER BY s.started_at DESC;"
                                )
                            },
                        },
                    },
                },
            ],
            "elements": elements,
            "layout": {"kind": "GridLayout", "spec": {"items": layout}},
        },
    }

    OUT.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
    json.loads(OUT.read_text(encoding="utf-8"))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {len(elements)} panels)")


if __name__ == "__main__":
    main()
