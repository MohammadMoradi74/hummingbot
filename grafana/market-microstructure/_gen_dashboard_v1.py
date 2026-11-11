#!/usr/bin/env python3
"""Generate market-microstructure-v1.json Grafana dashboard.

Side-by-side L1 book + trades for two gold ETFs (QuestDB).
Defaults: درخشان vs رز ترنج (Nov 8 buy-reject incident).
No Hummingbot/strategy overlays in v1.
"""
import json
from pathlib import Path

DS = "df3b4brg6ejggc"
GROUP = "questdb-questdb-datasource"
VER = "13.0.1+security-01"
OUT = Path(__file__).resolve().parent / "market-microstructure-v1.json"

# QuestDB stores TSE wall-clock as TIMESTAMP (naive, shown as Z).
# Dashboard TZ=UTC so axis 12:00 = TSE open. Filter hour 12–18 on that clock.
MARKET_HOUR_PRED = "hour(timestamp) >= 12 AND hour(timestamp) < 18"

# Persian label → IR code (same map as pair-trade v7, minus IRR cash).
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
)

# Incident defaults
DEFAULT_A = ("IRTKDRKS0001", "درخشان")
DEFAULT_B = ("IRTKROSE0001", "رز ترنج")


def _sym_options() -> list[dict]:
    # Explicit options so Grafana always substitutes ISIN (column `symbol` = ISIN).
    return [
        {
            "selected": False,
            "text": f"{label} · {code}",
            "value": code,
        }
        for code, label in _ASSET_LABELS
    ]


def _custom_query() -> str:
    # Grafana custom var: "text : value, text : value" (ISIN is the value).
    return ", ".join(f"{label} · {code} : {code}" for code, label in _ASSET_LABELS)


def pq(raw_sql: str, ref: str = "A") -> dict:
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


def qg(queries: list) -> dict:
    return {
        "kind": "QueryGroup",
        "spec": {"queries": queries, "transformations": [], "queryOptions": {}},
    }


def ts_viz(
    decimals=0,
    unit=None,
    draw="line",
    fill=0,
    overrides=None,
    tooltip_mode="multi",
    span_nulls=True,
    show_points="never",
    axis_label="",
):
    defaults = {
        "decimals": decimals,
        "color": {"mode": "palette-classic"},
        "custom": {
            "drawStyle": draw,
            "fillOpacity": fill,
            "lineWidth": 1 if draw == "bars" else 2,
            "showPoints": show_points,
            "spanNulls": span_nulls,
            "lineInterpolation": "linear",
            "axisBorderShow": False,
            "axisCenteredZero": False,
            "axisColorMode": "text",
            "axisLabel": axis_label,
            "axisPlacement": "auto",
            "barAlignment": 0,
            "barWidthFactor": 0.6,
            "gradientMode": "none",
            "hideFrom": {"legend": False, "tooltip": False, "viz": False},
            "insertNulls": False,
            "pointSize": 5,
            "scaleDistribution": {"type": "linear"},
            "showValues": False,
            "stacking": {"group": "A", "mode": "none"},
            "thresholdsStyle": {"mode": "off"},
        },
        "thresholds": {
            "mode": "absolute",
            "steps": [{"value": 0, "color": "green"}, {"value": 80, "color": "red"}],
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
                "annotations": {"clustering": -1, "multiLane": False},
                "legend": {
                    "calcs": [],
                    "displayMode": "list",
                    "placement": "bottom",
                    "showLegend": True,
                },
                "tooltip": {
                    "hideZeros": False,
                    "mode": tooltip_mode,
                    "sort": "none" if tooltip_mode == "single" else "desc",
                },
            },
            "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
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


def _sym_var(name: str, label: str, default_code: str, default_label: str) -> dict:
    opts = _sym_options()
    for o in opts:
        o["selected"] = o["value"] == default_code
    return {
        "kind": "CustomVariable",
        "spec": {
            "name": name,
            "label": label,
            "description": "QuestDB `symbol` column holds ISIN (e.g. IRTKROSE0001), not Persian ticker.",
            "hide": "dontHide",
            "skipUrlSync": False,
            "multi": False,
            "includeAll": False,
            "allowCustomValue": True,
            "options": opts,
            "current": {
                "text": f"{default_label} · {default_code}",
                "value": default_code,
            },
            "query": _custom_query(),
        },
    }


def price_sql(sym_var: str) -> str:
    # L1 bid/ask/mid + last trade + day_range band (ASOF).
    return f"""
WITH ob AS (
  SELECT timestamp,
    avg(CASE WHEN side = 'bid' THEN price END) AS bid,
    avg(CASE WHEN side = 'ask' THEN price END) AS ask,
    (avg(CASE WHEN side = 'bid' THEN price END)
      + avg(CASE WHEN side = 'ask' THEN price END)) / 2 AS mid
  FROM orderbook_historical
  WHERE symbol = '${{{sym_var}}}'
    AND level = '1'
    AND is_deleted = false
    AND $__timeFilter(timestamp)
    AND {MARKET_HOUR_PRED}
  SAMPLE BY ${{bucket}} ALIGN TO CALENDAR
),
dr AS (
  SELECT timestamp, day_range_min, day_range_max
  FROM day_range_historical
  WHERE symbol = '${{{sym_var}}}' AND is_deleted = false
),
tr AS (
  SELECT timestamp, last(price) AS last_px
  FROM trades_historical
  WHERE symbol = '${{{sym_var}}}'
    AND is_deleted = false
    AND canceled = false
    AND $__timeFilter(timestamp)
    AND {MARKET_HOUR_PRED}
  SAMPLE BY ${{bucket}} ALIGN TO CALENDAR
)
SELECT ob.timestamp AS time,
  ob.bid, ob.ask, ob.mid, tr.last_px,
  dr.day_range_min, dr.day_range_max
FROM ob
ASOF JOIN dr
ASOF JOIN tr;
""".strip()


def volume_sql(sym_var: str) -> str:
    # Bid volume positive, ask volume negated so they fan around zero.
    return f"""
SELECT timestamp AS time,
  avg(CASE WHEN side = 'bid' THEN volume END) AS bid_vol,
  -avg(CASE WHEN side = 'ask' THEN volume END) AS ask_vol_neg
FROM orderbook_historical
WHERE symbol = '${{{sym_var}}}'
  AND level = '1'
  AND is_deleted = false
  AND $__timeFilter(timestamp)
  AND {MARKET_HOUR_PRED}
SAMPLE BY ${{bucket}} ALIGN TO CALENDAR;
""".strip()


def spread_sql(sym_var: str) -> str:
    return f"""
SELECT timestamp AS time,
  10000.0 * (avg(CASE WHEN side = 'ask' THEN price END)
    - avg(CASE WHEN side = 'bid' THEN price END))
    / nullif(
      (avg(CASE WHEN side = 'bid' THEN price END)
        + avg(CASE WHEN side = 'ask' THEN price END)) / 2, 0) AS spread_bps
FROM orderbook_historical
WHERE symbol = '${{{sym_var}}}'
  AND level = '1'
  AND is_deleted = false
  AND $__timeFilter(timestamp)
  AND {MARKET_HOUR_PRED}
SAMPLE BY ${{bucket}} ALIGN TO CALENDAR;
""".strip()


def trades_sql(sym_var: str) -> str:
    return f"""
SELECT timestamp AS time,
  last(price) AS trade_px,
  sum(volume) AS trade_vol
FROM trades_historical
WHERE symbol = '${{{sym_var}}}'
  AND is_deleted = false
  AND canceled = false
  AND $__timeFilter(timestamp)
  AND {MARKET_HOUR_PRED}
SAMPLE BY ${{bucket}} ALIGN TO CALENDAR;
""".strip()


def compare_sql() -> str:
    # Mid % change from first in-window mid for each symbol (same axis).
    return f"""
WITH a AS (
  SELECT timestamp,
    (avg(CASE WHEN side = 'bid' THEN price END)
      + avg(CASE WHEN side = 'ask' THEN price END)) / 2 AS mid
  FROM orderbook_historical
  WHERE symbol = '$symbol_a'
    AND level = '1' AND is_deleted = false
    AND $__timeFilter(timestamp) AND {MARKET_HOUR_PRED}
  SAMPLE BY ${{bucket}} ALIGN TO CALENDAR
),
b AS (
  SELECT timestamp,
    (avg(CASE WHEN side = 'bid' THEN price END)
      + avg(CASE WHEN side = 'ask' THEN price END)) / 2 AS mid
  FROM orderbook_historical
  WHERE symbol = '$symbol_b'
    AND level = '1' AND is_deleted = false
    AND $__timeFilter(timestamp) AND {MARKET_HOUR_PRED}
  SAMPLE BY ${{bucket}} ALIGN TO CALENDAR
),
a0 AS (SELECT first(mid) AS m0 FROM a),
b0 AS (SELECT first(mid) AS m0 FROM b)
SELECT a.timestamp AS time,
  100.0 * (a.mid / a0.m0 - 1) AS mid_pct_a,
  100.0 * (b.mid / b0.m0 - 1) AS mid_pct_b
FROM a
JOIN b ON a.timestamp = b.timestamp
CROSS JOIN a0
CROSS JOIN b0;
""".strip()


def main():
    price_overrides = [
        {
            "matcher": {"id": "byName", "options": "day_range_min"},
            "properties": [
                {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [4, 4]}},
                {"id": "color", "value": {"fixedColor": "semi-dark-orange", "mode": "fixed"}},
            ],
        },
        {
            "matcher": {"id": "byName", "options": "day_range_max"},
            "properties": [
                {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [4, 4]}},
                {"id": "color", "value": {"fixedColor": "semi-dark-orange", "mode": "fixed"}},
            ],
        },
        {
            "matcher": {"id": "byName", "options": "bid"},
            "properties": [
                {"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}},
            ],
        },
        {
            "matcher": {"id": "byName", "options": "ask"},
            "properties": [
                {"id": "color", "value": {"fixedColor": "red", "mode": "fixed"}},
            ],
        },
        {
            "matcher": {"id": "byName", "options": "last_px"},
            "properties": [
                {"id": "custom.showPoints", "value": "always"},
                {"id": "custom.pointSize", "value": 4},
                {"id": "color", "value": {"fixedColor": "purple", "mode": "fixed"}},
            ],
        },
    ]

    vol_overrides = [
        {
            "matcher": {"id": "byName", "options": "bid_vol"},
            "properties": [
                {"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}},
                {"id": "custom.drawStyle", "value": "bars"},
                {"id": "custom.fillOpacity", "value": 50},
            ],
        },
        {
            "matcher": {"id": "byName", "options": "ask_vol_neg"},
            "properties": [
                {"id": "color", "value": {"fixedColor": "red", "mode": "fixed"}},
                {"id": "custom.drawStyle", "value": "bars"},
                {"id": "custom.fillOpacity", "value": 50},
                {"id": "displayName", "value": "ask_vol (neg)"},
            ],
        },
    ]

    trade_overrides = [
        {
            "matcher": {"id": "byName", "options": "trade_px"},
            "properties": [
                {"id": "custom.axisPlacement", "value": "left"},
                {"id": "color", "value": {"fixedColor": "purple", "mode": "fixed"}},
            ],
        },
        {
            "matcher": {"id": "byName", "options": "trade_vol"},
            "properties": [
                {"id": "custom.axisPlacement", "value": "right"},
                {"id": "custom.drawStyle", "value": "bars"},
                {"id": "custom.fillOpacity", "value": 40},
                {"id": "color", "value": {"fixedColor": "blue", "mode": "fixed"}},
            ],
        },
    ]

    elements = {
        "panel-1a": panel(
            1,
            "A · L1 price + day range",
            "L1 bid/ask/mid, last trade, day_range_min/max. Symbol = $symbol_a.",
            qg([pq(price_sql("symbol_a"))]),
            ts_viz(decimals=0, overrides=price_overrides),
        ),
        "panel-1b": panel(
            2,
            "B · L1 price + day range",
            "L1 bid/ask/mid, last trade, day_range_min/max. Symbol = $symbol_b.",
            qg([pq(price_sql("symbol_b"))]),
            ts_viz(decimals=0, overrides=price_overrides),
        ),
        "panel-2a": panel(
            3,
            "A · L1 volume (bid up / ask down)",
            "Best-bid size positive; best-ask size negated. Symbol = $symbol_a.",
            qg([pq(volume_sql("symbol_a"))]),
            ts_viz(decimals=0, draw="bars", fill=40, overrides=vol_overrides, span_nulls=False),
        ),
        "panel-2b": panel(
            4,
            "B · L1 volume (bid up / ask down)",
            "Best-bid size positive; best-ask size negated. Symbol = $symbol_b.",
            qg([pq(volume_sql("symbol_b"))]),
            ts_viz(decimals=0, draw="bars", fill=40, overrides=vol_overrides, span_nulls=False),
        ),
        "panel-3a": panel(
            5,
            "A · spread (bps)",
            "10000 * (ask − bid) / mid. Symbol = $symbol_a.",
            qg([pq(spread_sql("symbol_a"))]),
            ts_viz(decimals=1, unit="none"),
        ),
        "panel-3b": panel(
            6,
            "B · spread (bps)",
            "10000 * (ask − bid) / mid. Symbol = $symbol_b.",
            qg([pq(spread_sql("symbol_b"))]),
            ts_viz(decimals=1, unit="none"),
        ),
        "panel-4a": panel(
            7,
            "A · trades",
            "Last trade price (left) + traded volume (right). Symbol = $symbol_a.",
            qg([pq(trades_sql("symbol_a"))]),
            ts_viz(decimals=0, overrides=trade_overrides, show_points="auto"),
        ),
        "panel-4b": panel(
            8,
            "B · trades",
            "Last trade price (left) + traded volume (right). Symbol = $symbol_b.",
            qg([pq(trades_sql("symbol_b"))]),
            ts_viz(decimals=0, overrides=trade_overrides, show_points="auto"),
        ),
        "panel-5": panel(
            9,
            "Compare · mid % from window open",
            "Each mid indexed to its first in-window value. A = $symbol_a, B = $symbol_b.",
            qg([pq(compare_sql())]),
            ts_viz(decimals=2, unit="percent"),
        ),
    }

    layout_items = [
        gird(0, 0, 12, 9, "panel-1a"),
        gird(12, 0, 12, 9, "panel-1b"),
        gird(0, 9, 12, 7, "panel-2a"),
        gird(12, 9, 12, 7, "panel-2b"),
        gird(0, 16, 12, 6, "panel-3a"),
        gird(12, 16, 12, 6, "panel-3b"),
        gird(0, 22, 12, 7, "panel-4a"),
        gird(12, 22, 12, 7, "panel-4b"),
        gird(0, 29, 24, 7, "panel-5"),
    ]

    dashboard = {
        "apiVersion": "dashboard.grafana.app/v2",
        "kind": "Dashboard",
        "metadata": {
            "name": "market-microstructure-v1",
            "namespace": "default",
            "uid": "market-microstructure-v1",
        },
        "spec": {
            "title": "Market Microstructure (L1) v1",
            "description": (
                "Side-by-side L1 bid/ask/mid, depth volume, spread, trades, and day_range. "
                "QuestDB column `symbol` is ISIN (IRTK…). Variables pass ISIN values. "
                "Timestamps are TSE wall-clock (naive); TZ=UTC so 12:00 = TSE open; "
                "rows filtered to hour 12–18. Default range = 2026-11-08 12:00–13:00 "
                "(incident window). No strategy overlays."
            ),
            "tags": ["questdb", "orderbook", "trades", "microstructure", "gold-etf", "v1"],
            "editable": True,
            "cursorSync": "Crosshair",
            "liveNow": False,
            "preload": False,
            "links": [],
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
                # Absolute incident window (data exists). TZ=UTC matches naive TSE clock.
                "timezone": "UTC",
                "from": "2026-11-08T12:00:00.000Z",
                "to": "2026-11-08T13:00:00.000Z",
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
                _sym_var("symbol_a", "ISIN A", DEFAULT_A[0], DEFAULT_A[1]),
                _sym_var("symbol_b", "ISIN B", DEFAULT_B[0], DEFAULT_B[1]),
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

    OUT.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
    json.loads(OUT.read_text(encoding="utf-8"))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {len(elements)} panels)")


if __name__ == "__main__":
    main()
