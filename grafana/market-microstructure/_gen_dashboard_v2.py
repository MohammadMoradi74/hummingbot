#!/usr/bin/env python3
"""Generate market-microstructure-v2.json Grafana dashboard.

v2 vs v1:
  - Session + rotation pickers (Postgres pair_trade)
  - Both rotation legs (sell ISIN | buy ISIN) with L1–L5 ask/bid lines
  - Strategy overlays: intended prices, fills, failed buy events (Tehran wall clock)
  - Counterfactual: L1 ask vs intended_buy; passive_vol when ask ≤ intended
  - Slippage KPI table + session scatter (latency vs buy slip)
  - Time: panels use $__timeFilter; rotations-table id links set from/to (−5m/+10m)
  - Intended buy/sell shown as integers
"""
import json
from pathlib import Path

QDS = "df3b4brg6ejggc"
QGROUP = "questdb-questdb-datasource"
PDS = "afz4am1a4lywwf"
PGROUP = "grafana-postgresql-datasource"
VER = "13.0.1+security-01"
OUT = Path(__file__).resolve().parent / "market-microstructure-v2.json"
DASH_UID = "market-microstructure-v2"

# QuestDB timestamps = TSE wall-clock (naive). Postgres overlays use
# (ts AT TIME ZONE 'Asia/Tehran') so both land on the same UTC-axis labels.
# Time picker drives $__timeFilter; win_*_ms vars + links jump the picker to rotation±window.
MARKET_HOUR = "hour(timestamp) >= 12 AND hour(timestamp) < 18"
TEHRAN = "(ts AT TIME ZONE 'Asia/Tehran')"
# Tehran wall clock interpreted as UTC epoch (matches QuestDB naive-Z axis).
WIN_FROM_MS_SQL = (
    "(EXTRACT(EPOCH FROM (((ts AT TIME ZONE 'Asia/Tehran') - interval '5 minutes')"
    "::timestamp AT TIME ZONE 'UTC')) * 1000)::bigint"
)
WIN_TO_MS_SQL = (
    "(EXTRACT(EPOCH FROM (((ts AT TIME ZONE 'Asia/Tehran') + interval '10 minutes')"
    "::timestamp AT TIME ZONE 'UTC')) * 1000)::bigint"
)
# Row id link: absolute from/to + session/rotation from the clicked row.
# (Grafana vars must stay as ${...} — escape braces inside the f-string.)
ROW_WINDOW_URL = (
    f"/d/{DASH_UID}/{DASH_UID}"
    f"?from=${{__data.fields.from_ms}}&to=${{__data.fields.to_ms}}"
    f"&var-strategy=${{strategy}}"
    f"&var-session=${{__data.fields.session_id}}"
    f"&var-rotation=${{__data.fields.id}}"
    f"&var-bucket=${{bucket}}&var-buy_slippage_bps=${{buy_slippage_bps}}"
)


def pq_quest(raw_sql: str, ref: str = "A") -> dict:
    return {
        "kind": "PanelQuery",
        "spec": {
            "refId": ref,
            "hidden": False,
            "query": {
                "kind": "DataQuery",
                "group": QGROUP,
                "version": "v0",
                "datasource": {"name": QDS},
                "spec": {
                    "format": 1,
                    "meta": {
                        "builderOptions": {
                            "fields": [],
                            "limit": "",
                            "mode": "list",
                            "timeField": "",
                        }
                    },
                    "queryType": "sql",
                    "rawSql": raw_sql,
                    "selectedFormat": 2,
                },
            },
        },
    }


def pq_pg(raw_sql: str, fmt: str = "time_series", ref: str = "A") -> dict:
    return {
        "kind": "PanelQuery",
        "spec": {
            "refId": ref,
            "hidden": False,
            "query": {
                "kind": "DataQuery",
                "group": PGROUP,
                "version": "v0",
                "datasource": {"name": PDS},
                "spec": {"format": fmt, "rawQuery": True, "rawSql": raw_sql},
            },
        },
    }


def qg(queries: list) -> dict:
    return {
        "kind": "QueryGroup",
        "spec": {"queries": queries, "transformations": [], "queryOptions": {}},
    }


def ts_viz(decimals=0, unit=None, overrides=None, tooltip_mode="multi", fill=0, draw="line"):
    defaults = {
        "decimals": decimals,
        "color": {"mode": "palette-classic"},
        "custom": {
            "drawStyle": draw,
            "fillOpacity": fill,
            "lineWidth": 2,
            "showPoints": "never",
            "spanNulls": True,
            "lineInterpolation": "linear",
            "axisPlacement": "auto",
            "stacking": {"group": "A", "mode": "none"},
            "thresholdsStyle": {"mode": "off"},
        },
        "thresholds": {
            "mode": "absolute",
            "steps": [{"value": 0, "color": "green"}],
        },
    }
    if unit:
        defaults["unit"] = unit
    return {
        "kind": "VizConfig",
        "group": "timeseries",
        "version": VER,
        "spec": {
            "options": {
                "legend": {
                    "calcs": [],
                    "displayMode": "list",
                    "placement": "bottom",
                    "showLegend": True,
                },
                "tooltip": {"mode": tooltip_mode, "sort": "desc"},
            },
            "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        },
    }


def table_viz(sort_by=None, overrides=None):
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
                "defaults": {
                    "custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False}
                },
                "overrides": overrides or [],
            },
        },
    }


def stat_viz(decimals=2, unit=None):
    defaults = {
        "decimals": decimals,
        "thresholds": {"mode": "absolute", "steps": [{"value": None, "color": "blue"}]},
        "color": {"mode": "thresholds"},
    }
    if unit:
        defaults["unit"] = unit
    return {
        "kind": "VizConfig",
        "group": "stat",
        "version": VER,
        "spec": {
            "options": {
                "colorMode": "value",
                "graphMode": "none",
                "justifyMode": "auto",
                "orientation": "auto",
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "textMode": "auto",
                "wideLayout": True,
            },
            "fieldConfig": {"defaults": defaults, "overrides": []},
        },
    }


def panel(pid, title, description, data, viz):
    return {
        "kind": "Panel",
        "spec": {
            "id": pid,
            "title": title,
            "description": description,
            "links": [],
            "data": data,
            "vizConfig": viz,
        },
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


def qvar(
    name,
    label,
    sql,
    multi=False,
    include_all=False,
    sort="disabled",
    refresh="onDashboardLoad",
    current=None,
    hide="dontHide",
):
    return {
        "kind": "QueryVariable",
        "spec": {
            "name": name,
            "label": label,
            "hide": hide,
            "refresh": refresh,
            "skipUrlSync": False,
            "multi": multi,
            "includeAll": include_all,
            "allowCustomValue": True,
            "sort": sort,
            "regex": "",
            "regexApplyTo": "value",
            "options": [],
            "current": current or {"text": "", "value": ""},
            "query": {
                "kind": "DataQuery",
                "group": PGROUP,
                "version": "v0",
                "datasource": {"name": PDS},
                "spec": {"__legacyStringValue": sql},
            },
        },
    }


def depth_sql(isin_var: str, intended_col: str, intended_var: str) -> str:
    # Light L1–L5 + intended constant (must live in this query: SAMPLE BY needs aggregates).
    return f"""
WITH ob AS (
  SELECT timestamp,
    avg(CASE WHEN side='bid' AND level='1' THEN price END) AS bid1,
    avg(CASE WHEN side='bid' AND level='2' THEN price END) AS bid2,
    avg(CASE WHEN side='bid' AND level='3' THEN price END) AS bid3,
    avg(CASE WHEN side='bid' AND level='4' THEN price END) AS bid4,
    avg(CASE WHEN side='bid' AND level='5' THEN price END) AS bid5,
    avg(CASE WHEN side='ask' AND level='1' THEN price END) AS ask1,
    avg(CASE WHEN side='ask' AND level='2' THEN price END) AS ask2,
    avg(CASE WHEN side='ask' AND level='3' THEN price END) AS ask3,
    avg(CASE WHEN side='ask' AND level='4' THEN price END) AS ask4,
    avg(CASE WHEN side='ask' AND level='5' THEN price END) AS ask5
  FROM orderbook_historical
  WHERE symbol = '${{{isin_var}}}'
    AND is_deleted = false
    AND $__timeFilter(timestamp)
    AND {MARKET_HOUR}
  SAMPLE BY ${{bucket}} ALIGN TO CALENDAR
),
dr AS (
  SELECT timestamp, day_range_min, day_range_max
  FROM day_range_historical
  WHERE symbol = '${{{isin_var}}}' AND is_deleted = false
)
SELECT ob.timestamp AS time,
  ob.bid1, ob.bid2, ob.bid3, ob.bid4, ob.bid5,
  ob.ask1, ob.ask2, ob.ask3, ob.ask4, ob.ask5,
  (ob.bid1 + ob.ask1) / 2 AS mid,
  dr.day_range_min, dr.day_range_max,
  ${{{intended_var}}} AS {intended_col},
  (ob.bid1 / ${{{intended_var}}} - 1) * 10000 AS bid1_bps,
  (ob.ask1 / ${{{intended_var}}} - 1) * 10000 AS ask1_bps
FROM ob
ASOF JOIN dr;
""".strip()


def volume_sql(isin_var: str) -> str:
    return f"""
SELECT timestamp AS time,
  avg(CASE WHEN side='bid' AND level='1' THEN volume END) AS bid1_vol,
  -avg(CASE WHEN side='ask' AND level='1' THEN volume END) AS ask1_vol_neg
FROM orderbook_historical
WHERE symbol = '${{{isin_var}}}'
  AND is_deleted = false
  AND $__timeFilter(timestamp)
  AND {MARKET_HOUR}
SAMPLE BY ${{bucket}} ALIGN TO CALENDAR;
""".strip()


def counterfactual_sql() -> str:
    # Buy-leg ask path vs intended_buy; passive_vol only when ask ≤ intended.
    return f"""
WITH ob AS (
  SELECT timestamp AS time,
    avg(CASE WHEN side='ask' AND level='1' THEN price END) AS ask1,
    avg(CASE WHEN side='ask' AND level='1' THEN volume END) AS ask1_vol
  FROM orderbook_historical
  WHERE symbol = '${{isin_buy}}'
    AND is_deleted = false
    AND $__timeFilter(timestamp)
    AND {MARKET_HOUR}
  SAMPLE BY ${{bucket}} ALIGN TO CALENDAR
)
SELECT time,
  ask1,
  ask1 * (1.0 + ${{buy_slippage_bps}} / 10000.0) AS aggressive_buy,
  ${{intended_buy}} AS intended_buy,
  CASE WHEN ask1 <= ${{intended_buy}} THEN ask1_vol ELSE NULL END AS passive_vol
FROM ob;
""".strip()


def _rx(pattern: str) -> str:
    # Grafana anchors bare patterns as ^pat$ — so "bid1" never matches "A bid1".
    # Slash form is an unanchored JS regex.
    return f"/{pattern}/"


def _default_visible(names: list) -> dict:
    # Grafana's legend-toggle system override ("All except: …").
    # Regex hideFrom breaks click-to-show (chart goes blank). This form is what
    # Grafana itself writes when you toggle legend + Save — clicks extend `names`.
    return {
        "__systemRef": "hideSeriesFrom",
        "matcher": {
            "id": "byNames",
            "options": {
                "mode": "exclude",
                "names": names,
                "prefix": "All except:",
                "readOnly": True,
            },
        },
        "properties": [
            {
                "id": "custom.hideFrom",
                "value": {"legend": False, "tooltip": True, "viz": True},
            }
        ],
    }


def depth_overrides(side: str):
    """side='buy' → default visible intended_buy, buy_fill, ask1;
    side='sell' → intended_sell, sell_fill, bid1.
    """
    thin = [
        ("bid2", 1),
        ("bid3", 1),
        ("bid4", 1),
        ("bid5", 1),
        ("ask2", 1),
        ("ask3", 1),
        ("ask4", 1),
        ("ask5", 1),
    ]
    ov = []
    for name, w in thin:
        ov.append(
            {
                "matcher": {"id": "byRegexp", "options": _rx(name)},
                "properties": [
                    {"id": "custom.lineWidth", "value": w},
                    {"id": "custom.lineStyle", "value": {"fill": "dot", "dash": [2, 2]}},
                ],
            }
        )
    ov.extend(
        [
            {
                "matcher": {"id": "byRegexp", "options": _rx(r"bid1(?!\d)")},
                "properties": [
                    {"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}},
                    {"id": "custom.lineWidth", "value": 2},
                ],
            },
            {
                "matcher": {"id": "byRegexp", "options": _rx(r"ask1(?!\d)")},
                "properties": [
                    {"id": "color", "value": {"fixedColor": "red", "mode": "fixed"}},
                    {"id": "custom.lineWidth", "value": 2},
                ],
            },
            {
                "matcher": {"id": "byRegexp", "options": _rx(r"\bmid\b")},
                "properties": [
                    {"id": "color", "value": {"fixedColor": "blue", "mode": "fixed"}},
                ],
            },
            {
                "matcher": {"id": "byRegexp", "options": _rx("day_range_")},
                "properties": [
                    {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [4, 4]}},
                    {"id": "color", "value": {"fixedColor": "orange", "mode": "fixed"}},
                ],
            },
            {
                "matcher": {"id": "byRegexp", "options": _rx("intended_")},
                "properties": [
                    {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [8, 4]}},
                    {"id": "color", "value": {"fixedColor": "purple", "mode": "fixed"}},
                    {"id": "custom.lineWidth", "value": 2},
                ],
            },
            {
                "matcher": {"id": "byRegexp", "options": _rx(r"(buy|sell)_fill")},
                "properties": [
                    {"id": "custom.drawStyle", "value": "points"},
                    {"id": "custom.showPoints", "value": "always"},
                    {"id": "custom.pointSize", "value": 10},
                    {"id": "color", "value": {"fixedColor": "yellow", "mode": "fixed"}},
                ],
            },
            {
                "matcher": {"id": "byRegexp", "options": _rx("buy_fail")},
                "properties": [
                    {"id": "custom.drawStyle", "value": "points"},
                    {"id": "custom.showPoints", "value": "always"},
                    {"id": "custom.pointSize", "value": 7},
                    {"id": "color", "value": {"fixedColor": "dark-red", "mode": "fixed"}},
                ],
            },
            {
                # Right axis: bps vs intended (0 = at intended).
                "matcher": {"id": "byRegexp", "options": _rx(r"_bps\b")},
                "properties": [
                    {"id": "custom.axisPlacement", "value": "right"},
                    {"id": "custom.axisLabel", "value": "bps vs intended"},
                    {"id": "unit", "value": "none"},
                    {"id": "decimals", "value": 1},
                    {"id": "custom.lineWidth", "value": 1},
                    {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [4, 3]}},
                ],
            },
        ]
    )
    if side == "buy":
        # Exact legend display names (QuestDB → "A …", Postgres fill → bare).
        ov.append(
            _default_visible(["A ask1", "A intended_buy", "buy_fill", "A ask1_bps"])
        )
    else:
        ov.append(
            _default_visible(["A bid1", "A intended_sell", "sell_fill", "A bid1_bps"])
        )
    return ov


def main():
    sell_fills_sql = f"""
SELECT {TEHRAN} AS time, price::float8 AS sell_fill,
  (price::float8 / ${{intended_sell}}::float8 - 1) * 10000 AS sell_fill_bps
FROM pair_trade.fill
WHERE rotation_id = ${{rotation}} AND upper(side) = 'SELL';
""".strip()
    buy_fills_sql = f"""
SELECT {TEHRAN} AS time, price::float8 AS buy_fill,
  (price::float8 / ${{intended_buy}}::float8 - 1) * 10000 AS buy_fill_bps
FROM pair_trade.fill
WHERE rotation_id = ${{rotation}} AND upper(side) = 'BUY';
""".strip()
    # Plain strings: use ${var} once. (${{var}} is only for f-strings.)
    buy_fail_sql = """
SELECT (e.ts AT TIME ZONE 'Asia/Tehran') AS time,
  ${intended_buy}::float8 AS buy_fail
FROM pair_trade.event_log e
JOIN pair_trade.rotation r ON r.session_id = e.session_id AND r.id = ${rotation}
WHERE e.session_id IN (${session})
  AND e.event_type = 'order_end'
  AND e.payload->>'side' = 'buy'
  AND e.payload->>'reason' = 'failed'
  AND e.ts BETWEEN r.ts - interval '2 minutes' AND r.ts + interval '30 minutes';
""".strip()

    slip_table_sql = f"""
SELECT
  r.id AS rotation_id,
  r.status,
  r.prev_symbol || ' → ' || r.new_symbol AS legs,
  r.intended_sell_price,
  r.sell_vwap,
  ROUND((r.sell_slippage * 10000)::numeric, 2) AS sell_slip_bps,
  r.intended_buy_price,
  r.buy_vwap,
  ROUND((r.buy_slippage * 10000)::numeric, 2) AS buy_slip_bps,
  ROUND(r.latency_sec::numeric, 1) AS latency_sec,
  ROUND(r.last_edge::numeric, 6) AS last_edge,
  ROUND(r.realized_edge::numeric, 6) AS realized_edge,
  ROUND(r.transition_score::numeric, 6) AS expected_edge,
  {TEHRAN} AS tehran_ts
FROM pair_trade.rotation r
WHERE r.id = ${{rotation}};
""".strip()

    scatter_sql = """
SELECT
  COALESCE(r.latency_sec, 0)::float8 AS latency_sec,
  (r.buy_slippage * 10000)::float8 AS buy_slip_bps,
  r.status
FROM pair_trade.rotation r
WHERE r.session_id IN (${session})
  AND r.prev_symbol <> 'CASH'
  AND r.buy_slippage IS NOT NULL;
""".strip()

    rotations_table_sql = f"""
SELECT
  r.id,
  r.session_id,
  {TEHRAN} AS tehran_ts,
  r.prev_symbol,
  r.new_symbol,
  r.status,
  ROUND((r.sell_slippage * 10000)::numeric, 2) AS sell_slip_bps,
  ROUND((r.buy_slippage * 10000)::numeric, 2) AS buy_slip_bps,
  ROUND(r.latency_sec::numeric, 1) AS latency_sec,
  ROUND(r.intended_buy_price::numeric, 0) AS intended_buy,
  ROUND(r.buy_vwap::numeric, 0) AS buy_vwap,
  {WIN_FROM_MS_SQL} AS from_ms,
  {WIN_TO_MS_SQL} AS to_ms
FROM pair_trade.rotation r
WHERE r.session_id IN (${{session}})
ORDER BY r.ts DESC
LIMIT 100;
""".strip()

    rots_overrides = [
        {
            "matcher": {"id": "byName", "options": "id"},
            "properties": [
                {
                    "id": "links",
                    "value": [
                        {
                            "title": "Open −5m / +10m",
                            "url": ROW_WINDOW_URL,
                            "targetBlank": False,
                        }
                    ],
                }
            ],
        },
        {
            "matcher": {"id": "byRegexp", "options": "^(session_id|from_ms|to_ms)$"},
            "properties": [{"id": "custom.hidden", "value": True}],
        },
    ]

    # Single summary table — Stat panels show "No data" on NULL numerics / text status.
    kpi_summary_sql = """
SELECT
  r.status,
  COALESCE(ROUND((r.buy_slippage * 10000)::numeric, 2)::text, 'n/a') AS buy_slip_bps,
  COALESCE(ROUND((r.sell_slippage * 10000)::numeric, 2)::text, 'n/a') AS sell_slip_bps,
  COALESCE(ROUND(r.latency_sec::numeric, 1)::text, 'n/a') AS latency_sec,
  COALESCE(
    ROUND(EXTRACT(EPOCH FROM (MIN(f.ts) - r.ts))::numeric, 1)::text,
    'n/a (no buy fill)'
  ) AS sec_to_first_buy_fill,
  ROUND(r.intended_buy_price::numeric, 0) AS intended_buy,
  ROUND(r.intended_sell_price::numeric, 0) AS intended_sell
FROM pair_trade.rotation r
LEFT JOIN pair_trade.fill f
  ON f.rotation_id = r.id AND upper(f.side) = 'BUY'
WHERE r.id = ${rotation}
GROUP BY r.id, r.status, r.buy_slippage, r.sell_slippage, r.latency_sec,
         r.intended_buy_price, r.intended_sell_price, r.ts;
""".strip()

    vol_ov = [
        {
            "matcher": {"id": "byName", "options": "bid1_vol"},
            "properties": [
                {"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}},
                {"id": "custom.drawStyle", "value": "bars"},
                {"id": "custom.fillOpacity", "value": 50},
            ],
        },
        {
            "matcher": {"id": "byName", "options": "ask1_vol_neg"},
            "properties": [
                {"id": "color", "value": {"fixedColor": "red", "mode": "fixed"}},
                {"id": "custom.drawStyle", "value": "bars"},
                {"id": "custom.fillOpacity", "value": 50},
                {"id": "displayName", "value": "ask1_vol (neg)"},
            ],
        },
    ]
    cf_ov = [
        {
            "matcher": {"id": "byName", "options": "intended_buy"},
            "properties": [
                {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [8, 4]}},
                {"id": "color", "value": {"fixedColor": "purple", "mode": "fixed"}},
            ],
        },
        {
            "matcher": {"id": "byName", "options": "aggressive_buy"},
            "properties": [
                {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [2, 4]}},
                {"id": "color", "value": {"fixedColor": "semi-dark-red", "mode": "fixed"}},
            ],
        },
        {
            "matcher": {"id": "byName", "options": "passive_vol"},
            "properties": [
                {"id": "custom.drawStyle", "value": "bars"},
                {"id": "custom.fillOpacity", "value": 60},
                {"id": "custom.axisPlacement", "value": "right"},
                {"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}},
            ],
        },
        {
            "matcher": {"id": "byName", "options": "ask1"},
            "properties": [
                {"id": "color", "value": {"fixedColor": "red", "mode": "fixed"}},
            ],
        },
    ]

    elements = {
        "panel-kpi": panel(
            1,
            "Rotation summary",
            "Postgres pair_trade.rotation (+ first buy fill lag). n/a = no fill / not completed.",
            qg([pq_pg(kpi_summary_sql, "table")]),
            table_viz(),
        ),
        "panel-sell-depth": panel(
            10,
            "Sell leg · L1–L5 + intended/fills",
            "QuestDB depth for $isin_sell + intended_sell. Yellow = sell fills. "
            "Right axis: bid1_bps vs intended (0 = at intended). "
            "Default: intended_sell, sell_fill, bid1, bid1_bps.",
            qg(
                [
                    pq_quest(depth_sql("isin_sell", "intended_sell", "intended_sell"), "A"),
                    pq_pg(sell_fills_sql, "time_series", "B"),
                ]
            ),
            ts_viz(overrides=depth_overrides("sell")),
        ),
        "panel-buy-depth": panel(
            11,
            "Buy leg · L1–L5 + intended/fills/fails",
            "QuestDB depth for $isin_buy + intended_buy. Yellow = buy fills; dark-red = failed buys. "
            "Right axis: ask1_bps vs intended (0 = at intended). "
            "Default: intended_buy, buy_fill, ask1, ask1_bps.",
            qg(
                [
                    pq_quest(depth_sql("isin_buy", "intended_buy", "intended_buy"), "A"),
                    pq_pg(buy_fills_sql, "time_series", "B"),
                    pq_pg(buy_fail_sql, "time_series", "C"),
                ]
            ),
            ts_viz(overrides=depth_overrides("buy")),
        ),
        "panel-sell-vol": panel(
            12,
            "Sell leg · L1 volume",
            "bid up / ask down. ISIN=$isin_sell.",
            qg([pq_quest(volume_sql("isin_sell"))]),
            ts_viz(draw="bars", fill=40, overrides=vol_ov),
        ),
        "panel-buy-vol": panel(
            13,
            "Buy leg · L1 volume",
            "bid up / ask down. ISIN=$isin_buy.",
            qg([pq_quest(volume_sql("isin_buy"))]),
            ts_viz(draw="bars", fill=40, overrides=vol_ov),
        ),
        "panel-cf": panel(
            20,
            "Would waiting help? · ask ≤ intended_buy",
            "HOW TO READ: Purple = signal intended_buy. Red = live L1 ask. Dashed dark-red = "
            "aggressive limit (ask×(1+buy_slippage_bps)). Green bars (right axis) = L1 ask size "
            "ONLY when ask ≤ intended (passive fill was possible). "
            "If ask stays above purple and green never appears → waiting would NOT get you filled "
            "at intended; you needed to cross or abandon. If green bars appear later → a patient "
            "limit at intended could have filled.",
            qg([pq_quest(counterfactual_sql())]),
            ts_viz(overrides=cf_ov),
        ),
        "panel-slip": panel(
            21,
            "Rotation slippage breakdown",
            "Stored rotation fields. buy_slip mixes wait+cross+impact; use depth/CF panels to unbundle.",
            qg([pq_pg(slip_table_sql, "table")]),
            table_viz(),
        ),
        "panel-scatter": panel(
            22,
            "Session · latency vs buy slip",
            "Non-cash rotations with buy fill. Sort/scan for slow+expensive outliers.",
            qg([pq_pg(scatter_sql, "table")]),
            table_viz([{"desc": True, "displayName": "buy_slip_bps"}]),
        ),
        "panel-rots": panel(
            23,
            "Session rotations",
            "Click id → reload with that rotation and −5m/+10m time range. Tehran wall clock.",
            qg([pq_pg(rotations_table_sql, "table")]),
            table_viz([{"desc": True, "displayName": "tehran_ts"}], overrides=rots_overrides),
        ),
    }

    layout_items = [
        gird(0, 0, 24, 4, "panel-kpi"),
        gird(0, 4, 12, 10, "panel-sell-depth"),
        gird(12, 4, 12, 10, "panel-buy-depth"),
        gird(0, 14, 12, 6, "panel-sell-vol"),
        gird(12, 14, 12, 6, "panel-buy-vol"),
        gird(0, 20, 16, 9, "panel-cf"),
        gird(16, 20, 8, 9, "panel-slip"),
        gird(0, 29, 10, 8, "panel-scatter"),
        gird(10, 29, 14, 8, "panel-rots"),
    ]

    dashboard = {
        "apiVersion": "dashboard.grafana.app/v2",
        "kind": "Dashboard",
        "metadata": {
            "name": "market-microstructure-v2",
            "namespace": "default",
            "uid": "market-microstructure-v2",
        },
        "spec": {
            "title": "Market Microstructure (Rotation) v2",
            "description": (
                "Both legs of a pair_trade rotation: L1–L5 book (QuestDB ISIN) + strategy "
                "overlays (Postgres). Panels use $__timeFilter. Click a rotations-table id "
                "to jump −5m/+10m around that rotation. "
                "Default: session 58 / rotation 3141 (Nov 8 درخشان→رز ترنج)."
            ),
            # Grafana app v2 rejects classic dashboard links here ("incompatible list lengths").
            # Time-window jump: rotations-table id data links.
            "links": [],
            "tags": [
                "questdb",
                "postgres",
                "microstructure",
                "pair-trade",
                "slippage",
                "v2",
            ],
            "editable": True,
            "cursorSync": "Crosshair",
            "liveNow": False,
            "preload": False,
            "annotations": [
                {
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
                }
            ],
            "timeSettings": {
                # Default ≈ rotation 3141 window (Tehran wall as UTC labels).
                "timezone": "UTC",
                "from": "2026-11-08T11:56:17.000Z",
                "to": "2026-11-08T12:11:17.000Z",
                "autoRefresh": "",
                "autoRefreshIntervals": [
                    "5s",
                    "10s",
                    "30s",
                    "1m",
                    "5m",
                    "15m",
                    "30m",
                    "1h",
                    "2h",
                    "1d",
                ],
                "hideTimepicker": False,
                "fiscalYearStartMonth": 0,
            },
            "variables": [
                qvar(
                    "strategy",
                    "Strategy",
                    "SELECT name AS __text, name AS __value FROM pair_trade.strategy ORDER BY name;",
                    sort="alphabeticalAsc",
                    current={
                        "text": "gold_etf_statistical_arbitrage",
                        "value": "gold_etf_statistical_arbitrage",
                    },
                ),
                qvar(
                    "session",
                    "Session",
                    (
                        "SELECT s.id::text || ' @ ' || to_char(s.started_at AT TIME ZONE 'Asia/Tehran', "
                        "'YYYY-MM-DD HH24:MI') AS __text, s.id AS __value "
                        "FROM pair_trade.session s "
                        "JOIN pair_trade.strategy st ON st.id = s.strategy_id "
                        "WHERE st.name = '$strategy' "
                        "ORDER BY s.started_at DESC;"
                    ),
                    multi=True,
                    refresh="onTimeRangeChanged",
                    current={"text": "58", "value": "58"},
                ),
                qvar(
                    "rotation",
                    "Rotation",
                    (
                        "SELECT r.id::text || ' · ' || r.prev_symbol || '→' || r.new_symbol || ' · ' "
                        "|| r.status || ' @ ' || to_char(r.ts AT TIME ZONE 'Asia/Tehran', 'HH24:MI:SS') "
                        "AS __text, r.id AS __value "
                        "FROM pair_trade.rotation r "
                        "WHERE r.session_id IN ($session) "
                        "ORDER BY r.ts DESC;"
                    ),
                    refresh="onTimeRangeChanged",
                    current={"text": "3141", "value": "3141"},
                ),
                qvar(
                    "isin_sell",
                    "ISIN sell",
                    (
                        "SELECT COALESCE(split_part(sell_pair,'-',1), 'IRTKDRKS0001') AS __value, "
                        "prev_symbol || ' · ' || COALESCE(split_part(sell_pair,'-',1), '?') AS __text "
                        "FROM pair_trade.rotation WHERE id = $rotation;"
                    ),
                    refresh="onTimeRangeChanged",
                    current={"text": "درخشان · IRTKDRKS0001", "value": "IRTKDRKS0001"},
                ),
                qvar(
                    "isin_buy",
                    "ISIN buy",
                    (
                        "SELECT COALESCE(split_part(buy_pair,'-',1), 'IRTKROSE0001') AS __value, "
                        "new_symbol || ' · ' || COALESCE(split_part(buy_pair,'-',1), '?') AS __text "
                        "FROM pair_trade.rotation WHERE id = $rotation;"
                    ),
                    refresh="onTimeRangeChanged",
                    current={"text": "رز ترنج · IRTKROSE0001", "value": "IRTKROSE0001"},
                ),
                qvar(
                    "intended_sell",
                    "Intended sell",
                    (
                        "SELECT ROUND(COALESCE(intended_sell_price, 0))::bigint::text AS __value, "
                        "ROUND(COALESCE(intended_sell_price, 0))::bigint::text AS __text "
                        "FROM pair_trade.rotation WHERE id = $rotation;"
                    ),
                    refresh="onTimeRangeChanged",
                    current={"text": "44000", "value": "44000"},
                ),
                qvar(
                    "intended_buy",
                    "Intended buy",
                    (
                        "SELECT ROUND(COALESCE(intended_buy_price, 0))::bigint::text AS __value, "
                        "ROUND(COALESCE(intended_buy_price, 0))::bigint::text AS __text "
                        "FROM pair_trade.rotation WHERE id = $rotation;"
                    ),
                    refresh="onTimeRangeChanged",
                    current={"text": "30190", "value": "30190"},
                ),
                {
                    "kind": "CustomVariable",
                    "spec": {
                        "name": "buy_slippage_bps",
                        "label": "Buy slip bps",
                        "description": "Markup for aggressive_buy line (config buy_slippage_bps).",
                        "hide": "dontHide",
                        "skipUrlSync": False,
                        "multi": False,
                        "includeAll": False,
                        "allowCustomValue": True,
                        "options": [],
                        "current": {"text": "10", "value": "10"},
                        "query": "10, 30, 50",
                    },
                },
                {
                    "kind": "CustomVariable",
                    "spec": {
                        "name": "bucket",
                        "label": "Sample by",
                        "hide": "dontHide",
                        "skipUrlSync": False,
                        "multi": False,
                        "includeAll": False,
                        "allowCustomValue": True,
                        "options": [],
                        "current": {"text": "5s", "value": "5s"},
                        "query": "1s, 5s, 15s, 30s, 1m",
                    },
                },
            ],
            "elements": elements,
            "layout": {"kind": "GridLayout", "spec": {"items": layout_items}},
        },
    }

    blob = json.dumps(dashboard, indent=2) + "\n"
    # Catch plain-string ${{var}} leaks (Grafana sends that to Postgres → syntax error).
    if "${{" in blob:
        raise SystemExit("refusing to write: literal ${{ found (use f-string ${{var}} or plain ${var})")
    OUT.write_text(blob, encoding="utf-8")
    json.loads(OUT.read_text(encoding="utf-8"))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {len(elements)} panels)")


if __name__ == "__main__":
    main()
