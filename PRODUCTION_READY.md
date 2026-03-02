# План вывода бота в реальную торговлю (Production-ready)

Перед включением режима **live** нужно реализовать два блока и один переключатель.

---

## 1. Режим работы: paper / live

**Где:** `config.PROD_CONFIG["trading_mode"]` = `"paper"` | `"live"`.

- **paper** — текущее поведение: `PaperTrader`, только запись в `portfolio_state.json`, без реальных ордеров.
- **live** — реальные ордера через Polymarket CLOB; баланс и позиции с API (или синхронизация с состоянием).

**В коде:** используется фабрика `get_trader(cfg)` (см. ниже). При `trading_mode == "live"` возвращается экземпляр с тем же интерфейсом, что и `PaperTrader`, но с реальным исполнением.

---

## 2. Надёжные цены по открытым позициям

**Задача:** для всех открытых позиций иметь актуальную цену YES (для проверки SL/TP и оценки портфеля).

**Проблема сейчас:** Gamma API не даёт стабильно цены по конкретному `condition_id`; в стрим попадают только первые N рынков из пула, поэтому по части позиций цены не обновляются.

**Решение:** отдельный источник цен по списку рынков (market_id = condition_id или token_id).

### Вариант A: CLOB Order Book API (рекомендуется)

- Polymarket CLOB: `GET /book` по token_id (YES token из `clobTokenIds`).
- Из стакана: mid = (best_bid + best_ask) / 2 или best_ask как цена покупки.
- Нужно: по каждому открытому рынку хранить/доставать YES token_id (из Gamma при открытии или из сохранённого снимка).

**Файл:** новый модуль `position_prices.py` (или расширить `price_stream.py`):

```python
# position_prices.py
async def get_position_prices(market_ids: List[str], token_ids_by_market: Dict[str, str]) -> Dict[str, float]:
    """Вернуть market_id -> yes_price из CLOB book. token_ids_by_market = market_id -> yes_token_id."""
    # GET https://clob.polymarket.com/book?token_id=...
    # Парсить bid/ask, возвращать mid или ask.
    ...
```

- Вызывать раз в 10–30 сек для `list(trader.positions.keys())`.
- В `main_v2`: объединять эти цены с `stream.snapshot()` перед `check_stops()` и перед расчётом total value в отчёте.

**Ссылки:**  
- [Polymarket CLOB API](https://docs.polymarket.com/) — order book, markets.  
- Токен ID берётся из Gamma: поле `clobTokenIds` (первый элемент = YES).

### Вариант B: Gamma по condition_id

- Пробовать `GET /markets?condition_id=<full_hex>` или обход по категориям с поиском по `conditionId` (уже пробовали — ненадёжно). Оставить как запасной вариант только если CLOB недоступен.

### Интеграция в main_v2

- После `prices = stream.snapshot()` вызывать `position_prices.get_position_prices(positions_mids, token_ids)`.
- Мержить: `for mid, p in position_prices.items(): prices[mid] = {"yes_price": p, "timestamp": ...}`.
- Тогда `check_stops()` и отчёт раз в час будут использовать актуальные цены по всем позициям.

---

## 3. Реальное исполнение ордеров (CLOB)

**Задача:** по утверждённому сигналу выставить реальный ордер на Polymarket и обновить локальное состояние (баланс/позиции) по факту исполнения.

### Что реализовать

1. **Аутентификация CLOB**  
   - API key + secret (или wallet signing).  
   - Документация Polymarket: создание ключей, подпись запросов.

2. **Модуль исполнения**  
   - Файл: `live_trader.py` (или `clob_executor.py`).  
   - Интерфейс как у `PaperTrader`:
     - `execute_orders(approved_orders) -> {executions, portfolio}`  
     - `close_positions(to_close) -> {closed, portfolio}`  
     - Свойства: `balance`, `positions` (синхронизация с CLOB или с сохранённым состоянием после каждого fill).

3. **Логика execute_orders (live)**  
   - Для каждого ордера: создать limit order на CLOB (market_id → token_id, side=BUY, size, price).  
   - Опционально: ждать fill или проверять статус ордера по таймеру; при fill обновить `positions` и `balance` (или подтянуть с API).  
   - Возвращать структуру, совместимую с текущим кодом (executions с market_id, status, fill_price, tokens, cost_usd).

4. **Логика close_positions (live)**  
   - Продажа YES-токенов: создать sell order на CLOB по текущей цене или по лимиту.  
   - После fill обновить balance и удалить позицию из `positions`.

5. **Баланс и позиции**  
   - Либо читать с CLOB/Polymarket после каждого действия, либо вести локально по fills (как в paper), но с синхронизацией при старте (загрузить открытые позиции с API).

### Безопасность

- Ключи и секреты только в `.env`, не в коде.  
- В live-режиме добавить защиту: лимит на размер ордера, подтверждение (например, флаг в конфиге или разовая проверка перед первым live-запуском).

---

## 4. Чек-лист перед первым live-запуском

- [ ] `config`: `trading_mode = "live"`, ключи CLOB в `.env`.
- [ ] `position_prices.py`: реализован запрос цен по token_id (CLOB book); вызов из main_v2 и слияние с `prices`.
- [ ] `live_trader.py`: реализованы `execute_orders` и `close_positions` через CLOB; баланс/позиции согласованы с ботом.
- [ ] В main_v2 используется `get_trader(cfg)`; при live возвращается live-экземпляр, при paper — PaperTrader.
- [ ] Проверены лимиты размера и риска (risk_per_trade, max_trade_pct_of_volume и т.д.).
- [ ] Тест на маленькой сумме (минимальный ордер) перед полным объёмом.

---

## 5. Файлы и места в коде

| Задача | Файл | Что сделать |
|--------|------|-------------|
| Режим paper/live | `config.py` | Добавить `"trading_mode": "paper"`. |
| Фабрика трейдера | `main_v2.py` или `trader_factory.py` | `get_trader(cfg)` → PaperTrader или LiveTrader. |
| Цены по позициям | `position_prices.py` (новый) | `get_position_prices(market_ids, token_ids)` → dict mid → price; вызов из main_v2, merge в `prices`. |
| Live-исполнение | `live_trader.py` (новый) | Класс с интерфейсом PaperTrader, вызовы CLOB API. |
| Стрим + позиции | `main_v2.py` | После `stream.snapshot()` мержить цены из position_prices для позиций; передавать в check_stops и в отчёт. |

После реализации пунктов 1–3 и прохода по чек-листу бот будет готов к осторожному включению реальной торговли.
