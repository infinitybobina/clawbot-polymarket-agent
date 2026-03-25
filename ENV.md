# Переменные окружения ClawBot — сводка в одном файле

Данные собраны из: **OBSERVABILITY.md**, **PRODUCTION_READY.md**, **analytics/README.md**, **analytics/config/analytics.env.example**, кода (main.py, telegram_notify.py, strategy.py, trade_logger.py, analytics/check_closed_trades.py).

Скопируйте блок в конце файла в `.env` в корне проекта и заполните значения.

---

## Таблица переменных

| Переменная | Обязательность | Где используется | Описание |
|------------|----------------|------------------|----------|
| **TELEGRAM_TOKEN** | да (для отчётов) | main.py, main_v2.py, telegram_notify.py, telegram_test.py, analytics/check_closed_trades.py | OBSERVABILITY: «Нужны TELEGRAM_TOKEN и TELEGRAM_CHAT_ID в .env». Токен бота от @BotFather. |
| **TELEGRAM_CHAT_ID** | да (для отчётов) | те же | Куда слать сводки (число или строка). |
| **OPENAI_API_KEY** | да (для LLM) | strategy.py, run_test_a_llm_table.py, simple_test.py, test_api.py | strategy.py: при отсутствии — ValueError. Ключ API OpenAI для генерации сигналов. |
| **DATABASE_URL** | нет | trade_logger.py, analytics (check_closed_trades.py, ноутбуки, SQL через psql) | analytics/README: «Строка подключения к Postgres (та же БД, что у бота)». Пример: `postgres://user:password@localhost:5432/clawbot`. |
| **CONFIG_VERSION** | нет | trade_logger.py (запись в trades.config_version), аналитика (фильтр по версии) | analytics/README: «Должна совпадать с CONFIG_VERSION в .env бота». Пример: `2026-03-05_15m_conservative_v3`. |
| **CONFIG_HASH** | нет | trade_logger.py | Альтернатива CONFIG_VERSION, если бот пишет по хешу конфига. |
| **STRATEGY_ID** | нет | trade_logger.py (по умолчанию "LLM_CRYPTO_15M"), аналитика | analytics/README: «Идентификатор стратегии, например LLM_CRYPTO_15M». |
| **CHECK_CLOSED_TRADES_THRESHOLD** | нет | analytics/check_closed_trades.py | analytics/README: «Порог для check_closed_trades.py (по умолчанию 30)». При достижении — THRESHOLD_REACHED и опц. Telegram. |
| **ANALYTICS_TIMEZONE** | нет | Ноутбуки analytics | analytics/README: «Таймзона для группировок по времени (в SQL по умолчанию UTC)». |
| **MAX_TRADES_FOR_REPORT** | нет | Ноутбуки/отчёты | analytics/README: «Лимит строк для отчёта (0 = без лимита)». |
| **SHOW_PLOTS** | нет | Ноутбуки | analytics/README: «Выводить ли графики интерактивно (true/false)». |
| **POLYMARKET_API_KEY** / **POLYMARKET_API_SECRET** | нет (на будущее) | live_trader.py (не реализовано) | PRODUCTION_READY: «Ключи и секреты только в .env»; «ключи CLOB в .env». |

---

## Шаблон для копирования в `.env`

Скопируйте блок ниже в файл `.env` в корне проекта и заполните значения.

```
# Telegram (обязательно для отчётов)
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=

# OpenAI / LLM (обязательно для стратегии с LLM)
OPENAI_API_KEY=

# База данных (опционально)
DATABASE_URL=

# Версионирование и стратегия (опционально)
CONFIG_VERSION=2026-03-05_15m_conservative_v3
STRATEGY_ID=LLM_CRYPTO_15M

# Аналитика (опционально)
CHECK_CLOSED_TRADES_THRESHOLD=30
ANALYTICS_TIMEZONE=UTC
MAX_TRADES_FOR_REPORT=0
SHOW_PLOTS=true
```

Файл `.env` не коммитить в git (уже в .gitignore). Шаблон с комментариями по каждой переменной: **.env.example** в корне репо.
