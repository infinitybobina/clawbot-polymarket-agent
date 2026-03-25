#!/usr/bin/env python3
"""
Проверка количества закрытых сделок по config_version.
Читает DATABASE_URL и CONFIG_VERSION из корневого .env (приоритет), затем analytics/config/analytics.env при необходимости.
Если closed_trades >= THRESHOLD (по умолчанию 30), выводит THRESHOLD_REACHED и опционально шлёт в Telegram.

Запуск из корня репо: python analytics/check_closed_trades.py
Cron: */30 * * * * cd /path/to/repo && python analytics/check_closed_trades.py
"""

import asyncio
import os
import sys
from pathlib import Path

# корень репо: скрипт может быть вызван из корня или из analytics/
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# Вариант 2: корневой .env (DATABASE_URL и т.д.). Пробуем оба: по пути скрипта и по cwd.
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
CONFIG_VERSION = os.environ.get("CONFIG_VERSION", "2026-03-05_15m_conservative_v3")
THRESHOLD = int(os.environ.get("CHECK_CLOSED_TRADES_THRESHOLD", "30"))


async def main():
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        print("Add to project root .env: DATABASE_URL=postgres://user:password@host:5432/dbname", file=sys.stderr)
        sys.exit(1)
    try:
        import asyncpg
    except ImportError:
        print("ERROR: asyncpg not installed (pip install asyncpg)", file=sys.stderr)
        sys.exit(1)

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS closed_trades "
            "FROM trades WHERE config_version = $1 AND exit_ts IS NOT NULL",
            CONFIG_VERSION,
        )
    finally:
        await conn.close()

    n = row["closed_trades"]
    print(f"Closed trades for {CONFIG_VERSION}: {n}")

    if n >= THRESHOLD:
        print("THRESHOLD_REACHED")
        if os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
            try:
                from telegram_notify import send_telegram_message
                await send_telegram_message(
                    f"ClawBot analytics: closed_trades={n} (config_version={CONFIG_VERSION}), threshold {THRESHOLD} reached."
                )
            except Exception as e:
                print(f"Telegram send failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
