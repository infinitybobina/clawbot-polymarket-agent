#!/usr/bin/env python3
"""
SL-метрики после границы деплоя (sl_trigger_buffer): COUNT, AVG(exit_price - sl_at_exit), SUM(pnl_usd).

Читает DATABASE_URL (и CONFIG_VERSION для сплита) из корневого .env / analytics/config/analytics.env.

  SL_STATS_SINCE=2026-03-19T20:06:20+00:00   — обязательно (ISO 8601, UTC)
  python analytics/sl_stats.py

Опционально:
  SL_STATS_CONFIG_ONLY=1  — только строки с config_version = CONFIG_VERSION из .env
"""

import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

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

DATABASE_URL = os.environ.get("DATABASE_URL")
CONFIG_VERSION = os.environ.get("CONFIG_VERSION", "")
SL_STATS_SINCE = os.environ.get("SL_STATS_SINCE", "").strip()
CONFIG_ONLY = os.environ.get("SL_STATS_CONFIG_ONLY", "").strip().lower() in ("1", "true", "yes")

def _parse_iso_utc(s: str) -> datetime:
    s = (s or "").strip()
    if not s:
        raise ValueError("empty timestamp")
    # Support "Z" suffix.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


SQL_AGG = """
SELECT
    COUNT(*) AS n_sl,
    AVG(exit_price - sl_at_exit) FILTER (WHERE exit_price IS NOT NULL AND sl_at_exit IS NOT NULL) AS avg_exit_minus_sl,
    SUM(pnl_usd) AS sum_pnl_sl
FROM trades
WHERE exit_ts IS NOT NULL
  AND exit_ts >= $1::timestamptz
  AND exit_reason = 'SL'
"""

SQL_AGG_VERSIONED = SQL_AGG + " AND config_version = $2"

SQL_BUCKET = """
SELECT
    CASE WHEN config_version IS NULL THEN 'legacy_null' ELSE 'versioned' END AS bucket,
    COUNT(*) AS n_sl,
    AVG(exit_price - sl_at_exit) FILTER (WHERE exit_price IS NOT NULL AND sl_at_exit IS NOT NULL) AS avg_exit_minus_sl,
    SUM(pnl_usd) AS sum_pnl_sl
FROM trades
WHERE exit_ts IS NOT NULL
  AND exit_ts >= $1::timestamptz
  AND exit_reason = 'SL'
GROUP BY 1
ORDER BY 1
"""


def _print_row(title: str, row) -> None:
    print(f"\n=== {title} ===")
    print(f"  n_sl:              {row['n_sl']}")
    print(f"  avg_exit_minus_sl: {row['avg_exit_minus_sl']}")
    print(f"  sum_pnl_sl:        {row['sum_pnl_sl']}")


async def main() -> None:
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        sys.exit(1)
    if not SL_STATS_SINCE:
        print(
            "ERROR: SL_STATS_SINCE not set (ISO UTC), e.g. SL_STATS_SINCE=2026-03-19T20:06:20+00:00",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        since_dt = _parse_iso_utc(SL_STATS_SINCE)
    except Exception as e:
        print(f"ERROR: SL_STATS_SINCE parse failed: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        import asyncpg
    except ImportError:
        print("ERROR: asyncpg not installed (pip install asyncpg)", file=sys.stderr)
        sys.exit(1)

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        if CONFIG_ONLY:
            if not CONFIG_VERSION:
                print("ERROR: SL_STATS_CONFIG_ONLY=1 but CONFIG_VERSION empty.", file=sys.stderr)
                sys.exit(1)
            row = await conn.fetchrow(SQL_AGG_VERSIONED, since_dt, CONFIG_VERSION)
            _print_row(f"SL only config_version={CONFIG_VERSION!r}", row)
        else:
            row = await conn.fetchrow(SQL_AGG, since_dt)
            _print_row("SL all (any config_version)", row)
            rows = await conn.fetch(SQL_BUCKET, since_dt)
            print("\n=== SL by legacy_null vs versioned ===")
            for r in rows:
                print(
                    f"  {r['bucket']}: n={r['n_sl']}, avg_exit_minus_sl={r['avg_exit_minus_sl']}, sum_pnl={r['sum_pnl_sl']}"
                )
            if CONFIG_VERSION:
                row2 = await conn.fetchrow(SQL_AGG_VERSIONED, since_dt, CONFIG_VERSION)
                _print_row(f"SL config_version={CONFIG_VERSION!r} (detail)", row2)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
