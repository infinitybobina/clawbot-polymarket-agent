# Analytics — слой аналитики по сделкам

Аналитика только **читает** таблицу `trades`; боевой код бота не зависит от этого модуля.

## Структура

```
analytics/
  config/
    analytics.env.example   # шаблон переменных окружения
  sql/
    count_closed_trades.sql  # число закрытых сделок по config_version
    equity_drawdown.sql      # эквити + просадка по config_version
    stats_overall.sql        # винрейт, avg win/loss, total PnL
    pnl_by_category.sql      # PnL по category / side
    pnl_by_llm_score.sql     # PnL по бакетам llm_score
    pnl_by_hour.sql          # PnL по часу входа (UTC)
  check_closed_trades.py     # скрипт/cron: COUNT, при >= THRESHOLD — вывод + опц. Telegram
  analyze_post_entry.py      # SL vs TP, время удержания, бакеты entry_price, категории, худшие сделки
  notebooks/
    daily_report.ipynb    # эквити, дроудаун, базовые статы
    llm_score_analysis.ipynb  # детализация по llm_score
  README.md               # этот файл
```

В корне репо: `schema/trades.sql`, `trade_logger.py` — схема и запись сделок; аналитика их не меняет.

## Запуск

1. **Настроить окружение**
   - Скопировать `config/analytics.env.example` в `config/analytics.env` (или в `.env` в корне репо).
   - Заполнить `DATABASE_URL`, `CONFIG_VERSION`, при необходимости `STRATEGY_ID`, `ANALYTICS_TIMEZONE`, `MAX_TRADES_FOR_REPORT`, `SHOW_PLOTS`.

2. **SQL из psql**
   - Подключиться: `psql $DATABASE_URL`
   - Задать версию: `\set config_version '2026-03-05_15m_conservative_v3'`
   - Запустить скрипт: `\i sql/equity_drawdown.sql` (путь от корня репо или полный).

3. **Счётчик закрытых сделок (скрипт / cron)**
   - Из корня репо: `python analytics/check_closed_trades.py`
   - Читает `analytics/config/analytics.env` или `.env`; выполняет `COUNT(*)` по `config_version` и закрытым сделкам.
   - При `closed_trades >= CHECK_CLOSED_TRADES_THRESHOLD` (по умолчанию 30) выводит `THRESHOLD_REACHED` и при заданных `TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID` шлёт уведомление.
   - Cron (пример): `*/30 * * * * cd /path/to/repo && python analytics/check_closed_trades.py`

4. **Пост-входовая диагностика (почему режет SL)**  
   - `python analytics/analyze_post_entry.py`  
   - Окно времени: `--since ISO`, или `ANALYZE_SINCE`, или `RESTART_UTC`, или `bot_session.json`, либо `--all` (опц. `--limit`).  
   - См. шапку скрипта для деталей.

5. **Ноутбуки**
   - Из корня репо или из `analytics/`: `jupyter notebook notebooks/daily_report.ipynb`
   - В первом блоке загрузить `analytics.env` (или `.env`) через `python-dotenv`, выставить `CONFIG_VERSION` и `DATABASE_URL`, далее использовать asyncpg/pandas/plotly для запросов и графиков.

## Ключевые переменные

Полный список переменных (включая корневой .env) — **[ENV.md](../ENV.md)** в корне репо.

| Переменная | Описание |
|------------|----------|
| `DATABASE_URL` | Строка подключения к Postgres (та же БД, что у бота). |
| `CONFIG_VERSION` | Версия стратегии/конфига для фильтра `trades.config_version` (как в .env бота). |
| `STRATEGY_ID` | Идентификатор стратегии, например `LLM_CRYPTO_15M`. |
| `ANALYTICS_TIMEZONE` | Таймзона для группировок по времени (опционально, в SQL по умолчанию UTC). |
| `MAX_TRADES_FOR_REPORT` | Лимит строк для отчёта (0 = без лимита). |
| `SHOW_PLOTS` | Выводить ли графики интерактивно (`true`/`false`). |
| `CHECK_CLOSED_TRADES_THRESHOLD` | Порог для `check_closed_trades.py` (по умолчанию 30). |

## Контракт по таблице trades

Все SQL и ноутбуки опираются на имена полей:

- **Идентификация:** trade_id, market_id, condition_id, yes_token_id, category  
- **Время:** entry_ts, exit_ts  
- **Направление и размер:** side, size_tokens, size_usd  
- **Цены:** entry_price, exit_price, avg_entry_price, avg_exit_price  
- **Результат и риск:** pnl_usd, pnl_pct, max_dd_pct, mae_pct, mfe_pct  
- **Книга/ликвидность:** book_ok_at_entry, hit_volume_cap, spread_at_entry, mid_at_entry  
- **LLM/стратегия:** llm_score, llm_raw_json, strategy_id, config_version, pattern_id  
- **Выход:** exit_reason, sl_price, tp_price  
- **Служебное:** created_at, updated_at  

В запросах фильтр по версии: `WHERE config_version = $1` (в ноутбуках) или `WHERE config_version = :'config_version'` (в psql после `\set config_version '...'`).
