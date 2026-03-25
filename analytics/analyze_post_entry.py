#!/usr/bin/env python3
"""
Диагностика «что происходит после входа»: закрытые сделки из Postgres.

Отвечает на вопросы в духе: сколько SL vs TP, как долго держали до SL,
в каких коридорах entry_price чаще режет стоп, есть ли смысл в mae_pct/mfe_pct.

Граница времени (первая сработавшая):
  1) аргумент --since ISO (UTC)
  2) переменная ANALYZE_SINCE
  3) RESTART_UTC
  4) bot_session.json → started_at_utc
  5) --all — все закрытые (осторожно: большие таблицы; см. --limit)

Примеры:
  python analytics/analyze_post_entry.py
  python analytics/analyze_post_entry.py --since 2026-03-21T03:36:35+00:00
  python analytics/analyze_post_entry.py --all --limit 2000

Читает DATABASE_URL из корневого .env (и analytics/config/analytics.env, если есть).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

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


def _parse_ts(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _resolve_since(cli_since: Optional[str], use_all: bool) -> Tuple[Optional[datetime], str]:
    if use_all:
        return None, "all closed trades (--all)"
    env_s = (os.environ.get("ANALYZE_SINCE") or "").strip()
    if cli_since:
        return _parse_ts(cli_since), "CLI --since"
    if env_s:
        return _parse_ts(env_s), "ANALYZE_SINCE"
    restart = (os.environ.get("RESTART_UTC") or "").strip()
    if restart:
        return _parse_ts(restart), "RESTART_UTC"
    try:
        from bot_runtime import load_session_file

        sess = load_session_file(str(_root))
        iso = (sess or {}).get("started_at_utc")
        if isinstance(iso, str) and iso.strip():
            return _parse_ts(iso.strip()), "bot_session.json"
    except ImportError:
        pass
    raise SystemExit(
        "Задайте окно: --since ISO UTC, или ANALYZE_SINCE, или RESTART_UTC, "
        "или положите bot_session.json, или используйте --all"
    )


SQL_BY_REASON = """
SELECT
    COALESCE(exit_reason, '(null)') AS exit_reason,
    COUNT(*)::bigint AS n,
    SUM(pnl_usd)::float AS sum_pnl,
    AVG(pnl_usd)::float AS avg_pnl,
    AVG(EXTRACT(EPOCH FROM (exit_ts - entry_ts)))::float AS avg_hold_sec,
    percentile_cont(0.5) WITHIN GROUP (
        ORDER BY EXTRACT(EPOCH FROM (exit_ts - entry_ts))
    )::float AS median_hold_sec,
    MIN(EXTRACT(EPOCH FROM (exit_ts - entry_ts)))::float AS min_hold_sec,
    MAX(EXTRACT(EPOCH FROM (exit_ts - entry_ts)))::float AS max_hold_sec
FROM trades
WHERE exit_ts IS NOT NULL
  AND ($1::timestamptz IS NULL OR exit_ts >= $1::timestamptz)
GROUP BY 1
ORDER BY n DESC;
"""

SQL_ENTRY_BUCKET = """
SELECT
    CASE
        WHEN entry_price < 0.2 THEN '0.00-0.20'
        WHEN entry_price < 0.4 THEN '0.20-0.40'
        WHEN entry_price < 0.6 THEN '0.40-0.60'
        WHEN entry_price < 0.8 THEN '0.60-0.80'
        ELSE '0.80-1.00'
    END AS entry_bucket,
    COALESCE(exit_reason, '(null)') AS exit_reason,
    COUNT(*)::bigint AS n,
    SUM(pnl_usd)::float AS sum_pnl,
    AVG(EXTRACT(EPOCH FROM (exit_ts - entry_ts)))::float AS avg_hold_sec
FROM trades
WHERE exit_ts IS NOT NULL
  AND ($1::timestamptz IS NULL OR exit_ts >= $1::timestamptz)
GROUP BY 1, 2
ORDER BY 1, 2;
"""

SQL_CATEGORY = """
SELECT
    COALESCE(NULLIF(TRIM(category), ''), '(null)') AS category,
    COALESCE(exit_reason, '(null)') AS exit_reason,
    COUNT(*)::bigint AS n,
    SUM(pnl_usd)::float AS sum_pnl,
    AVG(pnl_usd)::float AS avg_pnl
FROM trades
WHERE exit_ts IS NOT NULL
  AND ($1::timestamptz IS NULL OR exit_ts >= $1::timestamptz)
GROUP BY 1, 2
ORDER BY n DESC
LIMIT 80;
"""

SQL_MAE_MFE = """
SELECT
    COALESCE(exit_reason, '(null)') AS exit_reason,
    COUNT(*) FILTER (WHERE mae_pct IS NOT NULL)::bigint AS n_mae,
    COUNT(*) FILTER (WHERE mfe_pct IS NOT NULL)::bigint AS n_mfe,
    AVG(mae_pct)::float AS avg_mae_pct,
    AVG(mfe_pct)::float AS avg_mfe_pct
FROM trades
WHERE exit_ts IS NOT NULL
  AND ($1::timestamptz IS NULL OR exit_ts >= $1::timestamptz)
GROUP BY 1
ORDER BY 1;
"""

SQL_SL_OVERSHOOT = """
SELECT
    COUNT(*)::bigint AS n_sl,
    AVG((exit_price - sl_at_exit)::float) FILTER (
        WHERE exit_price IS NOT NULL AND sl_at_exit IS NOT NULL
    ) AS avg_exit_minus_sl,
    COUNT(*) FILTER (
        WHERE exit_price IS NOT NULL
          AND sl_at_exit IS NOT NULL
          AND exit_price <= sl_at_exit + 0.002
    )::bigint AS n_exit_near_sl
FROM trades
WHERE exit_ts IS NOT NULL
  AND exit_reason = 'SL'
  AND ($1::timestamptz IS NULL OR exit_ts >= $1::timestamptz);
"""

SQL_TOP_WORST = """
SELECT
    trade_id,
    substring(market_id from 1 for 18) AS market_prefix,
    category,
    entry_ts,
    exit_ts,
    exit_reason,
    (entry_price)::float AS entry_price,
    (exit_price)::float AS exit_price,
    (pnl_usd)::float AS pnl_usd,
    EXTRACT(EPOCH FROM (exit_ts - entry_ts))::float AS hold_sec,
    (llm_score)::float AS llm_score
FROM trades
WHERE exit_ts IS NOT NULL
  AND ($1::timestamptz IS NULL OR exit_ts >= $1::timestamptz)
ORDER BY pnl_usd ASC NULLS LAST
LIMIT $2;
"""


def _fmt_sec(s: Optional[float]) -> str:
    if s is None:
        return "-"
    if s < 120:
        return f"{s:.0f}s"
    if s < 7200:
        return f"{s/60:.1f}m"
    return f"{s/3600:.2f}h"


def _print_table(title: str, rows: list, keys: list) -> None:
    print(f"\n=== {title} ===")
    if not rows:
        print("  (no rows)")
        return
    for r in rows:
        d = dict(r)
        parts = [f"{k}={d.get(k)}" for k in keys if k in d]
        print("  " + " | ".join(parts))


async def _run(conn: Any, since: Optional[datetime]) -> None:
    p = since
    rows = await conn.fetch(SQL_BY_REASON, p)
    keys = [
        "exit_reason",
        "n",
        "sum_pnl",
        "avg_pnl",
        "avg_hold_sec",
        "median_hold_sec",
        "min_hold_sec",
        "max_hold_sec",
    ]
    print("\n=== Hold time: use avg_hold_sec / median_hold_sec (shown human-readable below) ===")
    for r in rows:
        d = dict(r)
        print(
            f"  {d.get('exit_reason')}: n={d.get('n')} "
            f"sum_pnl=${d.get('sum_pnl'):.2f} avg_pnl=${d.get('avg_pnl') or 0:.2f} | "
            f"hold median={_fmt_sec(d.get('median_hold_sec'))} "
            f"avg={_fmt_sec(d.get('avg_hold_sec'))} "
            f"min={_fmt_sec(d.get('min_hold_sec'))} max={_fmt_sec(d.get('max_hold_sec'))}"
        )

    sl_n = sum(dict(x)["n"] for x in rows if dict(x).get("exit_reason") == "SL")
    tp_n = sum(dict(x)["n"] for x in rows if dict(x).get("exit_reason") == "TP")
    closed_n = sum(dict(x)["n"] for x in rows)
    if sl_n + tp_n > 0:
        print(
            f"\n=== SL vs TP (among rows with these reasons) ===\n"
            f"  SL: {sl_n}  TP: {tp_n}  ratio SL/TP: {sl_n / max(tp_n, 1):.2f}:1"
        )
    print(f"  Total closed (all reasons in window): {closed_n}")

    b = await conn.fetch(SQL_ENTRY_BUCKET, p)
    _print_table(
        "Entry price bucket x exit_reason",
        b,
        ["entry_bucket", "exit_reason", "n", "sum_pnl", "avg_hold_sec"],
    )

    c = await conn.fetch(SQL_CATEGORY, p)
    _print_table(
        "Category x exit_reason (top by n)",
        c,
        ["category", "exit_reason", "n", "sum_pnl", "avg_pnl"],
    )

    m = await conn.fetch(SQL_MAE_MFE, p)
    _print_table(
        "MAE/MFE % by exit_reason (if columns populated in DB)",
        m,
        ["exit_reason", "n_mae", "n_mfe", "avg_mae_pct", "avg_mfe_pct"],
    )

    o = await conn.fetchrow(SQL_SL_OVERSHOOT, p)
    if o and o["n_sl"]:
        print("\n=== SL execution vs sl_at_exit ===")
        print(f"  n_sl: {o['n_sl']}")
        print(f"  avg_exit_minus_sl: {o['avg_exit_minus_sl']}")
        print(
            f"  n_exit_within_0.002_of_sl: {o['n_exit_near_sl']} "
            f"({100.0 * o['n_exit_near_sl'] / o['n_sl']:.1f}% of SL)"
        )


async def main() -> None:
    ap = argparse.ArgumentParser(description="Post-entry / exit analytics from trades table.")
    ap.add_argument("--since", type=str, default=None, help="ISO timestamp UTC (inclusive lower bound on exit_ts)")
    ap.add_argument("--all", action="store_true", help="Ignore time window; all closed trades")
    ap.add_argument("--limit", type=int, default=5000, help="With --all, cap worst-trade query (default 5000)")
    ap.add_argument("--show-worst", type=int, default=15, help="Print N worst PnL trades (0=skip)")
    args = ap.parse_args()

    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        sys.exit(1)

    try:
        since_dt, source = _resolve_since(args.since, args.all)
    except SystemExit as e:
        print(f"ERROR: {e.args[0]}", file=sys.stderr)
        sys.exit(1)

    try:
        import asyncpg
    except ImportError:
        print("ERROR: pip install asyncpg", file=sys.stderr)
        sys.exit(1)

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        print(f"Window: {source}")
        if since_dt is not None:
            print(f"  exit_ts >= {since_dt.isoformat()}")
        await _run(conn, since_dt)

        if args.show_worst > 0:
            worst = await conn.fetch(SQL_TOP_WORST, since_dt, int(args.limit))
            print(f"\n=== Worst {args.show_worst} trades by pnl_usd ===")
            for r in worst[: args.show_worst]:
                d = dict(r)
                print(
                    f"  id={d.get('trade_id')} {d.get('exit_reason')} "
                    f"pnl=${d.get('pnl_usd')} hold={_fmt_sec(d.get('hold_sec'))} "
                    f"entry={d.get('entry_price')} exit={d.get('exit_price')} "
                    f"cat={d.get('category')} {d.get('market_prefix')}.."
                )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
