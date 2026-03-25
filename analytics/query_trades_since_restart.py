#!/usr/bin/env python3
"""Срез trades с момента перезапуска (UTC).

Граница времени (по приоритету):
1) RESTART_UTC в окружении (пример: 2026-03-20T16:06:00+00:00)
2) bot_session.json → started_at_utc (пишет main_v2 при старте)
3) запасной дефолт: 2026-03-20 19:06 MSK = 16:06 UTC
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

if load_dotenv:
    for base in (_root, Path(os.getcwd())):
        for name in (".env", ".env.txt"):
            p = base / name
            if p.exists():
                load_dotenv(p)
                break

from bot_runtime import load_session_file

DATABASE_URL = os.environ.get("DATABASE_URL")
_FALLBACK_RESTART = "2026-03-20T16:06:00+00:00"


def _resolve_restart_raw() -> tuple[str, str]:
    """Возвращает (строка для парсинга, метка источника для вывода)."""
    env = (os.environ.get("RESTART_UTC") or "").strip()
    if env:
        return env, "RESTART_UTC"
    sess = load_session_file(str(_root))
    iso = (sess or {}).get("started_at_utc")
    if isinstance(iso, str) and iso.strip():
        return iso.strip(), "bot_session.json"
    return _FALLBACK_RESTART, "fallback (no RESTART_UTC, no bot_session.json)"


RESTART_RAW, RESTART_SOURCE = _resolve_restart_raw()


def _parse_ts(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def main() -> None:
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    restart = _parse_ts(RESTART_RAW)
    import asyncpg

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        n_open = await conn.fetchval(
            "SELECT COUNT(*) FROM trades WHERE entry_ts >= $1 AND exit_ts IS NULL",
            restart,
        )
        agg = await conn.fetch(
            """
            SELECT COALESCE(exit_reason, 'NULL') AS exit_reason, COUNT(*) AS n, SUM(pnl_usd) AS sum_pnl
            FROM trades
            WHERE exit_ts IS NOT NULL AND exit_ts >= $1
            GROUP BY 1
            ORDER BY n DESC
            """,
            restart,
        )
        rows = await conn.fetch(
            """
            SELECT
                trade_id,
                substring(market_id from 1 for 20) AS market_prefix,
                category,
                entry_ts,
                exit_ts,
                exit_reason,
                pnl_usd,
                entry_price,
                exit_price,
                sl_at_exit,
                size_usd,
                llm_score,
                config_version
            FROM trades
            WHERE entry_ts >= $1 OR (exit_ts IS NOT NULL AND exit_ts >= $1)
            ORDER BY COALESCE(exit_ts, entry_ts) ASC
            LIMIT 100
            """,
            restart,
        )
        print(f"Since restart (UTC): {restart.isoformat()} [{RESTART_SOURCE}]")
        print(f"Open rows (entry >= restart, exit_ts NULL): {n_open}")
        print("\nClosed since restart by exit_reason:")
        for r in agg:
            print(f"  {r['exit_reason']}: n={r['n']}, sum_pnl={r['sum_pnl']}")
        print("\nChronological (COALESCE(exit_ts, entry_ts)):")
        for r in rows:
            d = dict(r)
            print(d)

        strict = await conn.fetch(
            """
            SELECT COALESCE(exit_reason, 'OPEN') AS exit_reason,
                   COUNT(*) AS n, SUM(pnl_usd) AS sum_pnl
            FROM trades
            WHERE entry_ts >= $1 AND exit_ts IS NOT NULL
            GROUP BY 1
            ORDER BY n DESC
            """,
            restart,
        )
        print("\nStrict: entry_ts >= restart AND closed:")
        for r in strict:
            print(f"  {r['exit_reason']}: n={r['n']}, sum_pnl={r['sum_pnl']}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
