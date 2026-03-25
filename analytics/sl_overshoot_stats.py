#!/usr/bin/env python3
"""
Проверка overshoot на SL.

Что считаем:
- exit_reason='SL'
- считаем "late" если exit_price <= sl_at_exit - SL_OVERSHOOT_DELTA_ABS

Флаги (все ISO UTC):
- SL_OVERSHOOT_SINCE=2026-03-19T21:00:00+00:00 (обязательно для запроса окна)
- SL_OVERSHOOT_UNTIL=2026-03-20T21:00:00+00:00 (обязательно для запроса окна)
- SL_OVERSHOOT_DELTA_ABS=0.002 (по умолчанию)
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

_env_analytics = _root / "analytics" / "config" / "analytics.env"
if load_dotenv:
    for base in (_root, Path(os.getcwd())):
        for name in (".env", ".env.txt"):
            p = base / name
            if p.exists():
                load_dotenv(p)
                break
        if _env_analytics.exists():
            load_dotenv(_env_analytics)
            break

DATABASE_URL = os.environ.get("DATABASE_URL")
SL_OVERSHOOT_SINCE = os.environ.get("SL_OVERSHOOT_SINCE", "").strip()
SL_OVERSHOOT_UNTIL = os.environ.get("SL_OVERSHOOT_UNTIL", "").strip()
SL_OVERSHOOT_DELTA_ABS = float(os.environ.get("SL_OVERSHOOT_DELTA_ABS", "0.002"))


def _parse_iso_utc(s: str) -> datetime:
    s = (s or "").strip()
    if not s:
        raise ValueError("empty timestamp")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


SQL_MAIN = """
WITH sl AS (
    SELECT
        config_version,
        llm_score,
        spread_at_entry,
        entry_price,
        exit_price,
        sl_at_exit,
        pnl_usd,
        exit_ts
    FROM trades
    WHERE exit_reason = 'SL'
      AND exit_ts IS NOT NULL
      AND exit_ts >= $1::timestamptz
      AND exit_ts <  $2::timestamptz
      AND exit_price IS NOT NULL
      AND sl_at_exit IS NOT NULL
)
SELECT
    COUNT(*) AS n_sl,
    AVG(exit_price - sl_at_exit) AS avg_exit_minus_sl_at_exit,
    MIN(exit_price - sl_at_exit) AS min_exit_minus_sl_at_exit,
    PERCENTILE_CONT(0.05) WITHIN GROUP (ORDER BY (exit_price - sl_at_exit)) AS p05_exit_minus_sl_at_exit,
    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY (exit_price - sl_at_exit)) AS p50_exit_minus_sl_at_exit,
    SUM(pnl_usd) AS sum_pnl_usd,
    SUM(CASE WHEN exit_price <= (sl_at_exit - $3) THEN 1 ELSE 0 END) AS n_late_abs,
    AVG(CASE WHEN exit_price <= (sl_at_exit - $3) THEN 1 ELSE 0 END)::float AS late_rate_abs,
    SUM(CASE WHEN exit_price <= (sl_at_exit - ($3 * 2.0)) THEN 1 ELSE 0 END) AS n_late_abs_x2,
    AVG(CASE WHEN exit_price <= (sl_at_exit - ($3 * 2.0)) THEN 1 ELSE 0 END)::float AS late_rate_abs_x2,
    SUM(CASE WHEN exit_price <= (sl_at_exit - $3) THEN pnl_usd ELSE 0 END) AS sum_pnl_usd_late_abs
FROM sl;
"""

SQL_BY_BUCKET = """
WITH sl AS (
    SELECT
        CASE WHEN config_version IS NULL THEN 'legacy_null' ELSE 'versioned' END AS bucket,
        exit_price,
        sl_at_exit,
        pnl_usd,
        exit_ts
    FROM trades
    WHERE exit_reason = 'SL'
      AND exit_ts IS NOT NULL
      AND exit_ts >= $1::timestamptz
      AND exit_ts <  $2::timestamptz
      AND exit_price IS NOT NULL
      AND sl_at_exit IS NOT NULL
)
SELECT
    bucket,
    COUNT(*) AS n_sl,
    SUM(pnl_usd) AS sum_pnl_usd,
    AVG(exit_price - sl_at_exit) AS avg_exit_minus_sl_at_exit,
    SUM(CASE WHEN exit_price <= (sl_at_exit - $3) THEN 1 ELSE 0 END) AS n_late_abs,
    AVG(CASE WHEN exit_price <= (sl_at_exit - $3) THEN 1 ELSE 0 END)::float AS late_rate_abs
FROM sl
GROUP BY 1
ORDER BY n_sl DESC;
"""

SQL_BY_LLM_BUCKET = """
WITH sl AS (
    SELECT
        CASE
            WHEN llm_score IS NULL THEN 'null'
            WHEN llm_score < 0.2 THEN '0.0-0.2'
            WHEN llm_score < 0.4 THEN '0.2-0.4'
            WHEN llm_score < 0.6 THEN '0.4-0.6'
            WHEN llm_score < 0.8 THEN '0.6-0.8'
            ELSE '0.8-1.0'
        END AS llm_bucket,
        exit_price,
        sl_at_exit,
        pnl_usd,
        exit_ts
    FROM trades
    WHERE exit_reason = 'SL'
      AND exit_ts IS NOT NULL
      AND exit_ts >= $1::timestamptz
      AND exit_ts <  $2::timestamptz
      AND exit_price IS NOT NULL
      AND sl_at_exit IS NOT NULL
)
SELECT
    llm_bucket,
    COUNT(*) AS n_sl,
    SUM(pnl_usd) AS sum_pnl_usd,
    AVG(exit_price - sl_at_exit) AS avg_exit_minus_sl_at_exit,
    AVG(CASE WHEN exit_price <= (sl_at_exit - $3) THEN 1 ELSE 0 END)::float AS late_rate_abs
FROM sl
GROUP BY 1
ORDER BY n_sl DESC;
"""


def _print_kv(k: str, v) -> None:
    print(f"{k:40s}: {v}")


async def main() -> None:
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        sys.exit(1)
    if not SL_OVERSHOOT_SINCE or not SL_OVERSHOOT_UNTIL:
        print(
            "ERROR: set SL_OVERSHOOT_SINCE and SL_OVERSHOOT_UNTIL (ISO UTC).",
            file=sys.stderr,
        )
        sys.exit(1)

    since_dt = _parse_iso_utc(SL_OVERSHOOT_SINCE)
    until_dt = _parse_iso_utc(SL_OVERSHOOT_UNTIL)
    if until_dt <= since_dt:
        raise ValueError("SL_OVERSHOOT_UNTIL must be > SL_OVERSHOOT_SINCE")

    try:
        import asyncpg
    except ImportError:
        print("ERROR: asyncpg not installed (pip install asyncpg)", file=sys.stderr)
        sys.exit(1)

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow(SQL_MAIN, since_dt, until_dt, SL_OVERSHOOT_DELTA_ABS)
        print("\n=== SL overshoot (late=exit_price <= sl_at_exit - delta) ===")
        print(f"window_utc: [{since_dt.isoformat()} .. {until_dt.isoformat()})")
        print(f"delta_abs:  {SL_OVERSHOOT_DELTA_ABS}")
        _print_kv("n_sl", row["n_sl"])
        _print_kv("avg_exit_minus_sl_at_exit", row["avg_exit_minus_sl_at_exit"])
        _print_kv("min_exit_minus_sl_at_exit", row["min_exit_minus_sl_at_exit"])
        _print_kv("p05_exit_minus_sl_at_exit", row["p05_exit_minus_sl_at_exit"])
        _print_kv("p50_exit_minus_sl_at_exit", row["p50_exit_minus_sl_at_exit"])
        _print_kv("sum_pnl_usd", row["sum_pnl_usd"])
        _print_kv("late_abs n", row["n_late_abs"])
        _print_kv("late_abs rate", row["late_rate_abs"])
        _print_kv("late_abs_x2 n", row["n_late_abs_x2"])
        _print_kv("late_abs_x2 rate", row["late_rate_abs_x2"])
        _print_kv("sum_pnl_usd_late_abs", row["sum_pnl_usd_late_abs"])

        rows = await conn.fetch(SQL_BY_BUCKET, since_dt, until_dt, SL_OVERSHOOT_DELTA_ABS)
        print("\n=== Late rate by config_version bucket ===")
        for r in rows:
            print(
                f"  bucket={r['bucket']}: n={r['n_sl']}, late_rate_abs={r['late_rate_abs']}, avg_exit_minus_sl={r['avg_exit_minus_sl_at_exit']}, sum_pnl={r['sum_pnl_usd']}"
            )

        rows2 = await conn.fetch(SQL_BY_LLM_BUCKET, since_dt, until_dt, SL_OVERSHOOT_DELTA_ABS)
        print("\n=== Late rate by llm_score bucket ===")
        for r in rows2:
            print(
                f"  llm_bucket={r['llm_bucket']}: n={r['n_sl']}, late_rate_abs={r['late_rate_abs']}, avg_exit_minus_sl={r['avg_exit_minus_sl_at_exit']}, sum_pnl={r['sum_pnl_usd']}"
            )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

