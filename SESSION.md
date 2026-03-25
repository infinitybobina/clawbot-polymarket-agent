# SESSION — снимок для нового чата (Cursor)

**Назначение:** вставь в **первое сообщение** нового чата блок «Текущий фокус» ниже + напиши `@SESSION.md`, чтобы не тащить длинную историю и сократить задержки ответа.

---

## Текущий фокус (копипаст в новый чат)

```
Проект: ClawBot v2, Polymarket, paper (`PROD_CONFIG["trading_mode"]`).
Точка входа: python main_v2.py. Лог: clawbot_v2_run.log (не вставлять целиком — только хвост).

Сейчас важно:
- Собрать статистику SL после деплоя sl_trigger_buffer: exit_ts >= <UTC_ГРАНИЦА>, exit_reason='SL'.
  Метрики: AVG(exit_price - sl_at_exit), SUM(pnl_usd) по SL.
- Старые строки trades с config_version IS NULL — отдельное окно; новые — через CONFIG_VERSION в .env.

Ключевые файлы: main_v2.py, config.py, datafeed.py, position_prices.py, trade_logger.py, reentry_cooldown.py.
Доки: AGENTS.md, OBSERVABILITY.md, ENV.md. Схема БД: schema/trades.sql.
```

_Подставь свою дату границы вместо `2026-03-19T17:06:20+00:00` после каждого значимого рестарта._

---

## Репозиторий и режим

| Что | Значение |
|-----|----------|
| Основной цикл | `main_v2.py`, `LOOP_INTERVAL_SEC = 60` |
| LLM-слот | **`LLM_INTERVAL_SEC = 300`** (5 мин). Не держать 60 с «боевым» конфигом — иначе много входов за четверть часа |
| Лимит «залпа» | **`max_new_trades_per_llm_slot`** (в `PROD_CONFIG`, по умолчанию **2**) — не больше новых рынков за один вызов LLM |
| Режим | `PROD_CONFIG["trading_mode"]` → **paper** (live только по явной просьбе) |
| Лог v2 | `clawbot_v2_run.log` |
| Длительный прогон (14–16 ч) | **`run_session_hours.ps1 -Hours 15`** — отдельное окно PowerShell, автоперезапуск при падении `main_v2.py` до истечения окна |
| Состояние | `portfolio_state.json`, `sl_cooldown.json`, `tp_cooldown.json`, **`reentry_cooldown.json`** |
| Старт сессии | **`bot_session.json`** — `started_at_utc` (ISO Z), `pid`; пишется при входе в `main_loop`. В коде: `bot_runtime` (`record_session_start`, `get_started_at_utc`, `session_elapsed_seconds`, `load_session_file`). Срез БД: `analytics/query_trades_since_restart.py` берёт границу из файла, если нет `RESTART_UTC`. |

---

## Пороги стратегии (сверять с `config.py`)

- **EV:** `min_ev_threshold` = **0.06** (LLM + фильтр полей; без синтетического fallback-сигнала)
- **max_open_positions:** сейчас **0** (без лимита) — ускорение статистики; для боя задать **12–18**
- **SL / TP:** `sl_pct` = **0.12**, `tp_pct` = **0.18**
- **SL trigger buffer:** `sl_trigger_buffer` = **0.005** (раньше срабатывание SL из‑за дискретности цен)
- **Re-entry после BUY:** `reentry_cooldown_minutes` — из `cfg` в `main_v2`, **по умолчанию 60** (тики от `LOOP_INTERVAL_SEC`)
- **EXPIRY-эвристика:** `expiry_tte_seconds` по умолчанию **120** — низкий best_bid не трактуется как EXPIRY, пока до резолва больше этого окна

Категории: `ACTIVE_CATEGORIES` + `MARKET_CATEGORIES` в `config.py`.

---

## Исправления, о которых помнить (контекст для агента)

1. **YES-токен:** после enrich в снапшоте хранится корректный `yes_token_id`; позиции могут ремапиться — не использовать слепо `clob_token_ids[0]`.
2. **Best bid:** в `position_prices.py` — **максимум** по уровням bids (стакан может быть не отсортирован).
3. **`trade_logger`:** в `trades.config_version` не писать NULL — `CONFIG_VERSION` / `CONFIG_HASH` из `.env`, иначе **`MISSING_CONFIG_VERSION`** (см. `ENV.md`).
4. **Telegram:** BUY/EXIT с деталями книги и PnL — см. правки в `main_v2.py`.

---

## База и аналитика

- Postgres, таблица **`trades`** — колонки см. `schema/trades.sql` (`sl_at_exit`, `tp_at_exit`, `spread_at_entry`, `exit_reason`, …).
- Подключение и версия: **`DATABASE_URL`**, **`CONFIG_VERSION`** в `.env` (должны совпадать с ботом для фильтра по версии).
- Скрипты: `analytics/README.md`, `analytics/check_closed_trades.py`, **`analytics/analyze_post_entry.py`** — SL vs TP, время удержания, корзины `entry_price`, категории, MAE/MFE (если заполнены), худшие сделки.

### SQL: SL после границы (шаблон)

```sql
-- Подставь границу деплоя и при необходимости config_version
SELECT
  COUNT(*) AS n_sl,
  AVG(exit_price - sl_at_exit) AS avg_exit_minus_sl,
  SUM(pnl_usd) AS sum_pnl_sl
FROM trades
WHERE exit_ts IS NOT NULL
  AND exit_ts >= TIMESTAMPTZ '2026-03-19 20:06:20+00'  -- <-- обновляй
  AND exit_reason = 'SL';
-- AND config_version = '...'  -- если смотришь только новую версию
```

### SQL: широкий спред на входе (гипотеза убытков)

Исторически убытки коррелировали с **очень широким** `spread_at_entry` — полезно смотреть бакеты по `spread_at_entry` и `exit_reason`.

---

## Следующие шаги (чеклист)

- [ ] Накопить N сделок после последнего рестарта с **`sl_trigger_buffer`**; выполнить SQL выше.
- [ ] При необходимости подстроить **`sl_trigger_buffer`** (например 0.005 vs 0.01) или частоту цикла.
- [ ] Рассмотреть ужесточение отбора по **`spread_at_entry`** / `entry_liquidity` при подтверждении по данным.

---

## Как обновлять этот файл

Раз в этап (новая гипотеза, смена порогов, важный рестарт):

1. Обнови таблицу порогов и блок «Текущий фокус».
2. Пропиши **UTC-границу** последнего значимого деплоя для BEFORE/AFTER в SQL.
3. Кратко одной строкой: что уже проверено / что отменили.
