# Runbook для автономной работы над ClawBot

Документ для агента (AI): как продолжать поиск и устранение неточностей без участия пользователя.

**Быстрый handoff между чатами Cursor:** см. **`SESSION.md`** — снимок контекста, копипаст в новый чат + `@SESSION.md`.

## Цели (не менять без запроса пользователя)

| Метрика | Цель |
|--------|------|
| Сделок в день | 10–15 |
| Прибыль в день | $100–150 |
| Рабочий депозит | $1000 |

---

## Старт сессии: что проверить

1. **Лог**  
   Открыть `clawbot_run.log`, прочитать последние 100–200 строк:
   - Есть ли кандидаты после фильтров (или «0 candidates»)?
   - Есть ли FILLED / CLOSED, какой PnL?
   - Причины отказов: spread, best_size, book_levels, Risk rejected, ENTRY_TOO_HIGH и т.д.

2. **Состояние**  
   При необходимости смотреть:
   - `portfolio_state.json` — баланс, позиции, cumulative_realized_pnl
   - `sl_cooldown.json`, `tp_cooldown.json` — блокировки рынков

3. **Сделки за день**  
   Если настроена БД: запрос по таблице `trades` за текущие сутки (count, sum PnL).  
   Иначе — подсчёт по `clawbot_run.log` по строкам CLOSED / PnL.

---

## Типовые задачи и действия

| Ситуация | Действия |
|----------|----------|
| **0 кандидатов (всё режет ликвидность)** | Проверить в логе значения spread/best_size/book_levels. Ослабить `config.PROD_CONFIG["entry_liquidity"]` (max_spread, min_best_level_size, min_book_levels) или разобрать ответ CLOB в datafeed. Fallback без ликвидности в main_v2 уже есть — проверить, что dead-book фильтр (yes_bid > 0.01, yes_ask < 0.99) не режет всех. |
| **Кандидаты есть, но 0 сделок** | Смотреть Risk/LLM: ENTRY_TOO_HIGH, INVALID_SL_TP, EXCEEDED_*, 0 signals. Подстроить пороги в config или strategy. |
| **Мало сделок (< 10/день)** | Увеличить пул: max_markets_to_enrich, markets_limit по категориям; ослабить min_edge/min_volume в MARKET_CATEGORIES; проверить cooldown (sl_cooldown_runs/tp_cooldown_runs). |
| **Много закрытий по EXPIRY с убытком** | Ужесточить dead-book в main_v2 (например yes_bid > 0.03, yes_ask < 0.97); убедиться что EXPIRY добавляет рынок в cooldown; при необходимости поднять min_edge/объём по категориям. |
| **Прибыль ниже цели ($100–150/день)** | При достаточном количестве сделок — пересмотреть risk_per_trade, размер позиции, sl_pct/tp_pct; проверить win rate и долю EXPIRY. |

---

## Команды

- **Paper-режим (основной цикл):**  
  `python main_v2.py`

- **Бэктест (после смены стратегии/конфига):**  
  `python backtest.py`  
  (параметры/даты — см. backtest.py и config)

- **Анализ закрытых сделок (если есть скрипты):**  
  `python analytics/check_closed_trades.py`  
  (при наличии)

---

## Важно

- Не переключать `trading_mode` на `live` без явного указания пользователя.
- Не коммитить `.env`, ключи, пароли.
- Правило с целями и чеклистом: `.cursor/rules/clawbot-goals.mdc` (alwaysApply).

После правок — по возможности прогнать один цикл или бэктест и убедиться, что лог/метрики соответствуют ожиданиям.
