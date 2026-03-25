#!/usr/bin/env python3
"""
Диагностика убыточности по закрытым сделкам за текущий "день" (по Европе/Москве по умолчанию).

Считает:
- общий PnL / winrate
- разрез по exit_reason
- разрез по spread_at_entry (бакеты)
- top по category и (side, category)
- для SL: средний сдвиг exit_price - sl_at_exit и PnL по SL

Из окружения читает:
- DATABASE_URL
- CONFIG_VERSION (если задан и DIAG_CONFIG_ONLY=1, фильтрует только эту версию)

Переменные:
- DIAG_TZ (default: Europe/Moscow)
- DIAG_CONFIG_ONLY (default: 1 если CONFIG_VERSION не пустой, иначе 0)
- DIAG_DATE (optional): YYYY-MM-DD — календарный день в DIAG_TZ (иначе «сегодня» по часам)
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

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
DIAG_TZ = os.environ.get("DIAG_TZ", "Europe/Moscow")
DIAG_DATE = os.environ.get("DIAG_DATE", "").strip()
DIAG_CONFIG_ONLY = os.environ.get("DIAG_CONFIG_ONLY", "").strip().lower() in ("1", "true", "yes")

if DIAG_CONFIG_ONLY is False and CONFIG_VERSION.strip():
    # Если пользователь не указал DIAG_CONFIG_ONLY, по умолчанию берем "текущую" версию.
    # (Сделано так, чтобы это поведение совпадало с ожиданиями аналитики по config_version.)
    default_val = os.environ.get("DIAG_CONFIG_ONLY_DEFAULT", "").strip().lower() in ("1", "true", "yes")
    # если явно не просили, оставляем как есть (false); но если переменной нет в env, включаем по умолчанию.
    if "DIAG_CONFIG_ONLY" not in os.environ:
        DIAG_CONFIG_ONLY = True


def _compute_day_range_utc(tz_name: str) -> tuple[datetime, datetime, str]:
    tz_norm = tz_name.strip().lower()
    if tz_norm in ("utc", "etc/utc", "z"):
        tz = timezone.utc
        return (
            datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0),
            datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1),
            "UTC",
        )
    if tz_norm in ("europe/moscow", "msk"):
        tz = timezone(timedelta(hours=3))
        tz_label = "Europe/Moscow (UTC+3)"
    else:
        tz = None
        tz_label = tz_name

    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        if tz is None:
            tz = timezone(timedelta(hours=3))
            tz_label = f"{tz_label} (fallback UTC+3)"
    else:
        if tz is None:
            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                # В некоторых окружениях (особенно Windows без tzdata) ZoneInfo не знает ключ.
                tz = timezone(timedelta(hours=3))
                tz_label = f"{tz_label} (fallback UTC+3)"

    if tz is None:
        tz = timezone(timedelta(hours=3))
        tz_label = f"{tz_label} (fallback UTC+3)"

    now_local = datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    return start_utc, end_utc, tz_label


def _resolve_tz(tz_name: str) -> tuple:
    """Возвращает (tz, tz_label) для построения локальных дат."""
    tz_norm = tz_name.strip().lower()
    if tz_norm in ("utc", "etc/utc", "z"):
        return timezone.utc, "UTC"
    if tz_norm in ("europe/moscow", "msk"):
        return timezone(timedelta(hours=3)), "Europe/Moscow (UTC+3)"
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return timezone(timedelta(hours=3)), f"{tz_name} (fallback UTC+3)"
    try:
        return ZoneInfo(tz_name), tz_name
    except Exception:
        return timezone(timedelta(hours=3)), f"{tz_name} (fallback UTC+3)"


def _day_range_utc_for_local_ymd(tz_name: str, y: int, mo: int, d: int) -> tuple[datetime, datetime, str]:
    tz, tz_label = _resolve_tz(tz_name)
    start_local = datetime(y, mo, d, 0, 0, 0, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), tz_label


def _fmt_num(x) -> str:
    if x is None:
        return "null"
    try:
        return f"{x:.6g}"
    except Exception:
        return str(x)


SQL_BASE = """
WHERE exit_ts IS NOT NULL
  AND exit_ts >= $1::timestamptz
  AND exit_ts <  $2::timestamptz
"""

SQL_CONFIG = " AND config_version = $3"

SQL_TOTAL = f"""
SELECT
  COUNT(*) AS n,
  SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
  SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses,
  AVG(pnl_usd) AS avg_pnl_usd,
  SUM(pnl_usd) AS sum_pnl_usd
FROM trades
{SQL_BASE}
"""

SQL_BY_EXIT_REASON = f"""
SELECT
  COALESCE(exit_reason, 'NULL') AS exit_reason,
  COUNT(*) AS n,
  SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
  AVG(pnl_usd) AS avg_pnl_usd,
  SUM(pnl_usd) AS sum_pnl_usd,
  AVG(size_usd) AS avg_size_usd,
  AVG(spread_at_entry) AS avg_spread_at_entry,
  AVG(entry_price) AS avg_entry_price
FROM trades
{SQL_BASE}
GROUP BY 1
ORDER BY n DESC
"""

SQL_SPREAD_BUCKET = f"""
SELECT
  spread_bucket,
  COUNT(*) AS n,
  SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
  AVG(pnl_usd) AS avg_pnl_usd,
  SUM(pnl_usd) AS sum_pnl_usd
FROM (
  SELECT
    CASE
      WHEN spread_at_entry IS NULL THEN 'null'
      WHEN spread_at_entry < 0.02 THEN '<0.02'
      WHEN spread_at_entry < 0.05 THEN '0.02-0.05'
      WHEN spread_at_entry < 0.10 THEN '0.05-0.10'
      WHEN spread_at_entry < 0.15 THEN '0.10-0.15'
      ELSE '>=0.15'
    END AS spread_bucket,
    pnl_usd
  FROM trades
  {SQL_BASE}
 ) s
GROUP BY 1
ORDER BY n DESC
"""

SQL_LLM_BUCKET = f"""
SELECT
  llm_bucket,
  COUNT(*) AS n,
  SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
  AVG(pnl_usd) AS avg_pnl_usd,
  SUM(pnl_usd) AS sum_pnl_usd
FROM (
  SELECT
    CASE
      WHEN llm_score IS NULL THEN 'null'
      WHEN llm_score < 0.2 THEN '0.0-0.2'
      WHEN llm_score < 0.4 THEN '0.2-0.4'
      WHEN llm_score < 0.6 THEN '0.4-0.6'
      WHEN llm_score < 0.8 THEN '0.6-0.8'
      ELSE '0.8-1.0'
    END AS llm_bucket,
    pnl_usd
  FROM trades
  {SQL_BASE}
) s
GROUP BY 1
ORDER BY n DESC
"""

SQL_SL_LLM_BUCKET = f"""
SELECT
  llm_bucket,
  COUNT(*) AS n_sl,
  SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
  AVG(pnl_usd) AS avg_pnl_usd,
  SUM(pnl_usd) AS sum_pnl_usd
FROM (
  SELECT
    CASE
      WHEN llm_score IS NULL THEN 'null'
      WHEN llm_score < 0.2 THEN '0.0-0.2'
      WHEN llm_score < 0.4 THEN '0.2-0.4'
      WHEN llm_score < 0.6 THEN '0.4-0.6'
      WHEN llm_score < 0.8 THEN '0.6-0.8'
      ELSE '0.8-1.0'
    END AS llm_bucket,
    pnl_usd
  FROM trades
  {SQL_BASE}
    AND exit_reason = 'SL'
) s
GROUP BY 1
ORDER BY n_sl DESC
"""

SQL_TOP_CATEGORY = f"""
SELECT
  COALESCE(category, 'NULL') AS category,
  COUNT(*) AS n,
  AVG(pnl_usd) AS avg_pnl_usd,
  SUM(pnl_usd) AS sum_pnl_usd
FROM trades
{SQL_BASE}
GROUP BY 1
ORDER BY n DESC
LIMIT 10
"""

SQL_TOP_SIDE_CATEGORY = f"""
SELECT
  COALESCE(side, 'NULL') AS side,
  COALESCE(category, 'NULL') AS category,
  COUNT(*) AS n,
  AVG(pnl_usd) AS avg_pnl_usd,
  SUM(pnl_usd) AS sum_pnl_usd
FROM trades
{SQL_BASE}
GROUP BY 1, 2
ORDER BY n DESC
LIMIT 15
"""

SQL_SL_DETAIL = f"""
SELECT
  COUNT(*) AS n_sl,
  AVG(exit_price - sl_at_exit) AS avg_exit_minus_sl_at_exit,
  AVG(ABS(exit_price - sl_at_exit)) AS avg_abs_exit_minus_sl_at_exit,
  AVG(pnl_usd) AS avg_pnl_usd,
  SUM(pnl_usd) AS sum_pnl_usd
FROM trades
{SQL_BASE}
  AND exit_reason = 'SL'
  AND exit_price IS NOT NULL
  AND sl_at_exit IS NOT NULL
"""


def _print_totals(row) -> None:
    n = row["n"]
    wins = row["wins"]
    losses = row["losses"]
    winrate = (wins / n * 100.0) if n else 0.0
    print("\n=== Итого за день ===")
    print(f"  Сделок (n):        {n}")
    print(f"  Выигрышей:         {wins} ({winrate:.1f}%)")
    print(f"  Проигрышей:        {losses} ({(100.0 - winrate):.1f}%)")
    print(f"  Avg PnL, USD:     {_fmt_num(row['avg_pnl_usd'])}")
    print(f"  Sum PnL, USD:     {_fmt_num(row['sum_pnl_usd'])}")


def _print_rows(title: str, rows, keys) -> None:
    print(f"\n=== {title} ===")
    for r in rows:
        parts = []
        for k in keys:
            parts.append(f"{k}={_fmt_num(r[k])}")
        print("  " + ", ".join(parts))


async def main() -> None:
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        sys.exit(1)

    if DIAG_DATE:
        parts = DIAG_DATE.split("-")
        if len(parts) != 3:
            print(f"ERROR: DIAG_DATE must be YYYY-MM-DD, got {DIAG_DATE!r}", file=sys.stderr)
            sys.exit(1)
        try:
            y, mo, d = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            print(f"ERROR: DIAG_DATE parse failed: {DIAG_DATE!r}", file=sys.stderr)
            sys.exit(1)
        start_utc, end_utc, tz_name = _day_range_utc_for_local_ymd(DIAG_TZ, y, mo, d)
    else:
        start_utc, end_utc, tz_name = _compute_day_range_utc(DIAG_TZ)

    print("Диагностика закрытых сделок за день:")
    print(f"  TZ (локально):     {tz_name}")
    if DIAG_DATE:
        print(f"  DIAG_DATE:         {DIAG_DATE}")
    print(f"  Диапазон (UTC):   {start_utc.isoformat()} .. {end_utc.isoformat()}")
    if CONFIG_VERSION:
        print(f"  CONFIG_VERSION:    {CONFIG_VERSION}")
    print(f"  DIAG_CONFIG_ONLY:  {int(DIAG_CONFIG_ONLY)}")

    params = [start_utc, end_utc]
    config_filter_enabled = DIAG_CONFIG_ONLY and CONFIG_VERSION.strip()
    if config_filter_enabled:
        params.append(CONFIG_VERSION.strip())

    def _inject_config_filter(sql: str) -> str:
        if not config_filter_enabled:
            return sql
        snippet = "\n  AND config_version = $3"
        # Вставляем фильтр внутрь WHERE по exit_ts, чтобы запрос оставался синтаксически корректным.
        return sql.replace("AND exit_ts <  $2::timestamptz", "AND exit_ts <  $2::timestamptz" + snippet)

    total_sql = _inject_config_filter(SQL_TOTAL)
    exit_reason_sql = _inject_config_filter(SQL_BY_EXIT_REASON)
    spread_sql = _inject_config_filter(SQL_SPREAD_BUCKET)
    llm_sql = _inject_config_filter(SQL_LLM_BUCKET)
    sl_llm_sql = _inject_config_filter(SQL_SL_LLM_BUCKET)
    cat_sql = _inject_config_filter(SQL_TOP_CATEGORY)
    side_cat_sql = _inject_config_filter(SQL_TOP_SIDE_CATEGORY)
    sl_sql = _inject_config_filter(SQL_SL_DETAIL)

    try:
        import asyncpg
    except ImportError:
        print("ERROR: asyncpg not installed.", file=sys.stderr)
        sys.exit(1)

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        total = await conn.fetchrow(total_sql, *params)
        _print_totals(total)

        by_exit = await conn.fetch(exit_reason_sql, *params)
        _print_rows(
            "PnL по exit_reason",
            by_exit,
            keys=[
                "exit_reason",
                "n",
                "wins",
                "avg_pnl_usd",
                "sum_pnl_usd",
                "avg_size_usd",
                "avg_spread_at_entry",
                "avg_entry_price",
            ],
        )

        by_spread = await conn.fetch(spread_sql, *params)
        _print_rows(
            "PnL по spread_at_entry (бакеты)",
            by_spread,
            keys=["spread_bucket", "n", "wins", "avg_pnl_usd", "sum_pnl_usd"],
        )

        by_llm = await conn.fetch(llm_sql, *params)
        _print_rows(
            "PnL по llm_score (бакеты)",
            by_llm,
            keys=["llm_bucket", "n", "wins", "avg_pnl_usd", "sum_pnl_usd"],
        )

        sl_by_llm = await conn.fetch(sl_llm_sql, *params)
        _print_rows(
            "SL: PnL по llm_score (бакеты)",
            sl_by_llm,
            keys=["llm_bucket", "n_sl", "wins", "avg_pnl_usd", "sum_pnl_usd"],
        )

        top_cat = await conn.fetch(cat_sql, *params)
        _print_rows(
            "Top category",
            top_cat,
            keys=["category", "n", "avg_pnl_usd", "sum_pnl_usd"],
        )

        top_side_cat = await conn.fetch(side_cat_sql, *params)
        _print_rows(
            "Top (side, category)",
            top_side_cat,
            keys=["side", "category", "n", "avg_pnl_usd", "sum_pnl_usd"],
        )

        sl_detail = await conn.fetchrow(sl_sql, *params)
        print("\n=== SL detail (если есть) ===")
        print(f"  n_sl:                               {_fmt_num(sl_detail['n_sl'])}")
        print(
            "  avg(exit_price - sl_at_exit):      "
            f"{_fmt_num(sl_detail['avg_exit_minus_sl_at_exit'])}"
        )
        print(
            "  avg(abs(exit_price - sl_at_exit)): "
            f"{_fmt_num(sl_detail['avg_abs_exit_minus_sl_at_exit'])}"
        )
        print(f"  avg PnL SL, USD:                  {_fmt_num(sl_detail['avg_pnl_usd'])}")
        print(f"  sum PnL SL, USD:                  {_fmt_num(sl_detail['sum_pnl_usd'])}")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

