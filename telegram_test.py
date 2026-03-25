"""
Тест отправки сообщения в Telegram. Запуск: python telegram_test.py
"""
import asyncio
import os

from dotenv import load_dotenv
load_dotenv(".env")

from telegram import Bot

async def main():
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Ошибка: задайте TELEGRAM_TOKEN и TELEGRAM_CHAT_ID в .env")
        return
    bot = Bot(token=token)
    await bot.send_message(
        chat_id=chat_id,
        text="🚀 CLAWBOT TELEGRAM ✅\nСигналы LIVE!\nPnL: +$9,065,607 симуляция"
    )
    print("Отправлено!")

if __name__ == "__main__":
    asyncio.run(main())
