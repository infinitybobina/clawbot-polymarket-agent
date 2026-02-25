"""
Отправка сообщений в Telegram (Block 5 — сигналы в чат).
"""

import asyncio
import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from telegram import Bot
    _TELEGRAM_AVAILABLE = True
except ImportError:
    Bot = None
    _TELEGRAM_AVAILABLE = False


def _get_env():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return token, chat_id


async def send_telegram_message(text: str) -> bool:
    """Отправить сообщение в Telegram. Возвращает True при успехе."""
    if not _TELEGRAM_AVAILABLE or Bot is None:
        logger.warning("python-telegram-bot не установлен: pip install python-telegram-bot")
        return False
    token, chat_id = _get_env()
    if not token or not chat_id:
        logger.warning("TELEGRAM_TOKEN или TELEGRAM_CHAT_ID не заданы в .env")
        return False
    try:
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=text)
        logger.info("Telegram: сообщение отправлено")
        return True
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)
        return False


def send_telegram_sync(text: str) -> bool:
    """Синхронная обёртка для отправки из скрипта."""
    return asyncio.run(send_telegram_message(text))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    send_telegram_sync(
        "ClawBot Telegram\nСигналы LIVE!\nPnL: симуляция"
    )
    print("Done.")
