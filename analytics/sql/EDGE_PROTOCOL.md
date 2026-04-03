# Edge Protocol SQL Pack

Короткий runbook для ежедневной оценки стратегии и решения `GO / HOLD / STOP`.

## Файлы

- `edge_protocol_daily.sql` — базовый ежедневный пакет (5 запросов)
- `edge_protocol_compare.sql` — сравнение двух `config_version` (A vs B)
- `edge_protocol_go_no_go.sql` — светофор-решение по порогам
- `edge_protocol_old_vs_new.sql` — разрез «старые / новые» по времени (`entry_ts` и `exit_ts`)
- `copy_trading_stats.sql` — сводка copy vs non-copy по `strategy_id`
- `opened_after_split_by_category.sql` — диагностика «нового» бота: `category × exit_reason` + худшие SL
- `reentry_audit_opened_after_split.sql` — аудит повторных входов (re-entry) по `market_id` после `split_utc`
- `migrations/001_trades_sl_tp_at_exit.sql` — добавить `sl_at_exit` / `tp_at_exit`, если таблица старая

### Старая таблица `trades` без `sl_at_exit`

Скрипты Q3 / compare C3 используют **`sl_price`** (стоп на входе), чтобы не падать на старых БД.

Для метрик «как в боте» (exit vs уровень SL на закрытии) один раз в `psql`:

```sql
\i 'C:/Dev/Clawbot-polymarket-agent-clean/analytics/sql/migrations/001_trades_sl_tp_at_exit.sql'
```

После миграции новые закрытия от `trade_logger` начнут заполнять `sl_at_exit`; при желании Q3 можно снова переписать под `COALESCE(sl_at_exit, sl_price)`.

---

## 1) Daily (ежедневный снимок)

**Только в клиенте `psql`** (не в «Query» pgAdmin/DBeaver без поддержки `\set` и `:'var'`).

Сначала подключись к нужной БД (`\c clawbot`), **потом** задай переменные и `\i`:

```sql
\c clawbot
\set since_utc '2026-03-27 10:44:00+00'
\set cfg 'exp_A1'
\i 'C:/Dev/Clawbot-polymarket-agent-clean/analytics/sql/edge_protocol_daily.sql'
```

Если нужно без фильтра по версии:

```sql
\set since_utc '2026-03-25 02:06:00+03'
\set cfg ''
\i 'C:/Dev/Clawbot-polymarket-agent-clean/analytics/sql/edge_protocol_daily.sql'
```

### Ошибка «syntax error at ":"»

Обычно значит одно из двух:

1. Переменные не заданы **в этой же сессии** `psql` перед `\i` (после перезапуска `psql` нужно снова `\set since_utc` и `\set cfg`).
2. Скрипт открыт не в `psql`, а в редакторе, который шлёт SQL на сервер как есть — тогда `:'since_utc'` серверу непонятен.

---

## 2) Compare (A vs B)

```sql
\set since_utc '2026-03-27 10:44:00+00'
\set cfg_a ' '
\set cfg_b ' '
\i 'C:/Dev/Clawbot-polymarket-agent-clean/analytics/sql/edge_protocol_compare.sql'
```

Смотри в первую очередь:
- `DELTA_B_MINUS_A.expectancy_usd_per_trade`
- `tail_share_pct_of_total_abs`
- `sum_pnl_usd`, `winrate_pct`

---

## 3) GO / NO-GO (светофор)

```sql
\set since_utc '2026-03-27 13:38:00+03'
\set cfg ''
\set min_closed 30
\set min_expectancy 0
\set max_tail_share 35
\set min_winrate 35
\i C:/Dev/Clawbot-polymarket-agent-clean/analytics/sql/edge_protocol_go_no_go.sql
```

`decision`:
- `GREEN_GO` — можно масштабировать осторожно
- `YELLOW_HOLD` — продолжаем сбор данных / точечный тюнинг
- `RED_STOP` — гипотеза не подтверждена, переход к следующей

---

## 4) Старые vs новые сделки (порог деплоя / рестарта)

Когда менялся конфиг, а в Telegram видно много закрытий от **старых** входов, удобно резать не только по `config_version`, но и по **времени**.

Пример: последнее важное изменение **27.03.2026 17:04 MSK** → в UTC: **`2026-03-27 14:04:00+00`**.

```sql
\c clawbot
\set since_utc '2026-03-25 02:06:00+03'
\set split_utc '2026-03-30 13:14:00+03'
\set cfg ''
\i 'C:/Dev/Clawbot-polymarket-agent-clean/analytics/sql/edge_protocol_old_vs_new.sql'
```

Скрипт сначала выводит **три** основные таблицы:

1. **`opened_before_split` / `opened_after_split`** — позиция открыта до или после порога (главный смысл «старый бот / новый бот»).
2. **`closed_before_split` / `closed_after_split`** — когда пришло закрытие в БД (ближе к потоку Telegram).
3. **2×2** (`open_before` × `close_before/after`) — видно, например, «открыто до порога, закрыто после».

Две дополнительные таблицы только по **новым** входам описаны в **разделе 5** ниже.

---

## 5) Детализация только `opened_after_split` (в том же скрипте)


```sql
\c clawbot
\set since_utc '2026-03-25 02:06:00+03'
\set split_utc '2026-03-30 13:14:00+03'
\set cfg ''
\i 'C:/Dev/Clawbot-polymarket-agent-clean/analytics/sql/edge_protocol_old_vs_new.sql'
```

Запуск **тот же**, что в разделе 4: `\set since_utc`, `\set split_utc`, `\set cfg`, затем `\i .../edge_protocol_old_vs_new.sql`.

После трёх таблиц из раздела 4 скрипт печатает ещё **два** результата (все строки с `entry_ts >= split_utc`):

- **По `exit_reason`** — сколько закрытий SL / TP / `TIME_STOP` / прочее и суммарный PnL по каждой причине.
- **По `exit_day_utc`** — день **закрытия** в UTC (`(exit_ts AT TIME ZONE 'UTC')::date`); по строке видно дневной PnL и счётчики SL/TP/TIME_STOP.

---
6) Как посмотреть copy vs non_copy
В psql:

\c clawbot
\set since_utc '2026-03-22 14:38:00+03'
\set cfg ''
\i 'C:/Dev/Clawbot-polymarket-agent-clean/analytics/sql/copy_trading_stats.sql'
## Рекомендуемый ритуал (ежедневно)

7)Как запустить opened_after_split_by_category.sql в psql 
\c clawbot
\set since_utc '2026-03-22 14:38:00+03'
\set split_utc '2026-03-30 13:14:00+03'
\set cfg ''
\i 'C:/Dev/Clawbot-polymarket-agent-clean/analytics/sql/opened_after_split_by_category.sql'

8)Открыта ли ещё copy-сделка:
SELECT trade_id, market_id, strategy_id, entry_ts, exit_ts
FROM trades
WHERE strategy_id = 'copy'
ORDER BY entry_ts DESC
LIMIT 5;


1. Запустить `edge_protocol_daily.sql`
2. Если была новая версия конфига — запустить `edge_protocol_compare.sql`
3. Зафиксировать итог через `edge_protocol_go_no_go.sql`
4. Записать решение в `SESSION.md` (что меняли, что получили, next step)

---

## Дисциплина эксперимента

- На итерацию менять не больше 1–2 параметров.
- Каждая итерация = новый `CONFIG_VERSION`.
- Если за 48 часов нет улучшений по KPI — закрывать гипотезу, не "докручивать бесконечно".
