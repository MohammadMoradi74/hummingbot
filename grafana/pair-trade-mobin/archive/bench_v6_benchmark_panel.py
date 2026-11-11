#!/usr/bin/env python3
"""Time v6 benchmark-panel SQL variants (same DB, session list, COUNT result rows)."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _gen_dashboard_v6 as gen  # noqa: E402

SESSIONS = "1,2,3,4,5,6,7,8"
MH_BIN = (
    "((ss.bin_ts AT TIME ZONE 'Asia/Tehran')::time >= TIME '12:00:00' "
    "AND (ss.bin_ts AT TIME ZONE 'Asia/Tehran')::time <= TIME '18:00:00')"
)
MH_P = (
    "((p.ts AT TIME ZONE 'Asia/Tehran')::time >= TIME '12:00:00' "
    "AND (p.ts AT TIME ZONE 'Asia/Tehran')::time <= TIME '18:00:00')"
)
TOP5 = gen.TOP5_IN

PORT_PREV = f"""
bins AS (SELECT DISTINCT session_id, bin_ts AS real_ts FROM raw),
first_bin AS (
  SELECT b.session_id, b.real_ts FROM bins b
  JOIN pair_trade.session s ON s.id = b.session_id
  ORDER BY s.started_at, b.real_ts LIMIT 1
),
bin_nav0 AS (
  SELECT p.portfolio_value AS v FROM pair_trade.portfolio_snapshot p
  INNER JOIN first_bin fb ON fb.session_id = p.session_id AND p.ts <= fb.real_ts
  WHERE {MH_P.replace('p.ts', 'p.ts')}
  ORDER BY p.ts DESC LIMIT 1
),
port AS (
  SELECT p.session_id, p.ts AS real_ts,
         100.0 * (p.portfolio_value / NULLIF(n0.v, 0) - 1) AS port_pct
  FROM pair_trade.portfolio_snapshot p CROSS JOIN bin_nav0 n0
  WHERE p.session_id IN ({SESSIONS}) AND {MH_P}
),
port_events AS (
  SELECT s.started_at, b.session_id, b.real_ts, 0 AS is_port, NULL::numeric AS port_pct
  FROM bins b JOIN pair_trade.session s ON s.id = b.session_id
  UNION ALL
  SELECT s.started_at, p.session_id, p.real_ts, 1, p.port_pct FROM port p
  JOIN pair_trade.session s ON s.id = p.session_id
),
port_seq AS (
  SELECT *, ROW_NUMBER() OVER (ORDER BY started_at, real_ts, is_port DESC) AS rn FROM port_events
),
with_fill_rn AS (
  SELECT *, MAX(CASE WHEN port_pct IS NOT NULL THEN rn END)
    OVER (ORDER BY rn ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS fill_rn FROM port_seq
),
bin_port AS (
  SELECT w.session_id, w.real_ts, p.port_pct
  FROM with_fill_rn w JOIN port_seq p ON p.rn = w.fill_rn WHERE w.is_port = 0
)"""

RAW_INLINE = f"""
raw AS (
  SELECT ss.session_id, ss.bin_ts, ss.symbol, ss.mid_price
  FROM pair_trade.signal_snapshot ss
  WHERE session_id IN ({SESSIONS}) AND {MH_BIN}
)"""

RAW_MAT = RAW_INLINE.replace("raw AS (", "raw AS MATERIALIZED (")

IDX = f"""
sym_start AS (
  SELECT symbol, (array_agg(r.mid_price ORDER BY s.started_at, r.bin_ts))[1] AS m0
  FROM raw r JOIN pair_trade.session s ON s.id = r.session_id
  WHERE r.mid_price IS NOT NULL GROUP BY symbol
),
idx AS (
  SELECT r.session_id, r.bin_ts AS real_ts,
         AVG(100.0 * (r.mid_price / NULLIF(ss.m0, 0) - 1)) AS all_pct,
         AVG(100.0 * (r.mid_price / NULLIF(ss.m0, 0) - 1))
           FILTER (WHERE r.symbol IN ({TOP5})) AS top5_pct
  FROM raw r JOIN sym_start ss ON ss.symbol = r.symbol
  WHERE r.mid_price IS NOT NULL GROUP BY r.session_id, r.bin_ts
)"""


def _core(raw_cte: str, port_and_ffill: str) -> str:
    return f"""
WITH {raw_cte},
{IDX},
{port_and_ffill}
SELECT COUNT(*) FROM bin_port bp JOIN idx i ON i.session_id = bp.session_id AND i.real_ts = bp.real_ts;
"""


def _run_sql(sql: str, runs: int = 5, cold: bool = True) -> tuple[float, str]:
    env = {"PGPASSWORD": "pgadmin123!"}
    times: list[float] = []
    last_out = ""
    for _ in range(runs):
        if cold:
            subprocess.run(
                [
                    "psql",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "pgadmin",
                    "-d",
                    "hummingbot",
                    "-c",
                    "DISCARD PLANS;",
                ],
                capture_output=True,
                env={**subprocess.os.environ, **env},
            )
        t0 = time.perf_counter()
        proc = subprocess.run(
            [
                "psql",
                "-h",
                "127.0.0.1",
                "-U",
                "pgadmin",
                "-d",
                "hummingbot",
                "-v",
                "ON_ERROR_STOP=1",
                "-At",
                "-c",
                sql,
            ],
            capture_output=True,
            text=True,
            env={**subprocess.os.environ, **env},
        )
        elapsed = (time.perf_counter() - t0) * 1000
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
        times.append(elapsed)
        last_out = proc.stdout.strip()
    return sum(times) / len(times), last_out


def main() -> None:
    port_curr = gen._benchmark_port_cte().replace("$session", SESSIONS) + ", " + gen._port_ffill_on_bins_cte()
    variants = [
        (
            "v6_original (inline raw, global bin_nav0, fill_rn self-join)",
            _core(RAW_INLINE, PORT_PREV),
        ),
        (
            "v6_sql_opt (MATERIALIZED raw, global bin_nav0, MATERIALIZED port_seq ffill)",
            _core(RAW_MAT, PORT_PREV.replace("port_seq AS (", "port_seq AS MATERIALIZED (")),
        ),
        (
            "v6_current (generator SQL: window ffill, no result self-join)",
            _core(RAW_MAT, port_curr),
        ),
    ]
    ver_proc = subprocess.run(
        ["psql", "-h", "127.0.0.1", "-U", "pgadmin", "-d", "hummingbot", "-At", "-c", "SHOW server_version;"],
        capture_output=True,
        text=True,
        env={**subprocess.os.environ, "PGPASSWORD": "pgadmin123!"},
    )
    pg_ver = ver_proc.stdout.strip() if ver_proc.returncode == 0 else "unknown"
    print(f"PostgreSQL: {pg_ver}")
    print(f"Sessions: {SESSIONS}")
    print(f"{'Variant':<72} {'ms_avg':>8}  result_rows,port_rows,bin_rows")
    print("-" * 105)
    for label, sql in variants:
        ms, row = _run_sql(sql)
        print(f"{label:<72} {ms:8.1f}  rows={row}")


if __name__ == "__main__":
    main()
