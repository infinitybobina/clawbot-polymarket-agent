"""
Обработчик отправки в Telegram. Синхронный вызов send_telegram(text).
"""
try:
    from telegram_notify import send_telegram_sync
    def send_telegram(text: str) -> bool:
        return send_telegram_sync(text)
except ImportError:
    def send_telegram(text: str) -> bool:
        return False
