# Copy Trading (Paper MVP)

Быстрый мост между внешним сервисом сигналов (включая будущий A2A) и ботом.

## Что уже реализовано

- `copy_trading` блок в `config.py`
- адаптер в `main_v2.py`, который читает `copy_signals.json` и добавляет сигналы в pipeline risk manager
- утилита `scripts/copy_wallets_fetch.py` для генерации `copy_signals.json` из:
  - файла (`--from-file`)
  - API URL (`--from-url`)

## 1) Включить в конфиге

В `config.py`:

- `PROD_CONFIG["copy_trading"]["enabled"] = True`

Остальные параметры можно оставить по умолчанию:
- `signals_file`: `copy_signals.json`
- `base_size_usd`: 300
- `min_expected_ev`: 0.04
- `max_entry_price`: 0.50

## 2) Сгенерировать copy_signals.json

### Вариант A: из локального файла

```bash
python scripts/copy_wallets_fetch.py --from-file source_signals.json --output copy_signals.json
```

### Вариант B: из API (A2A/внешний сервис)

```bash
python scripts/copy_wallets_fetch.py --from-url "https://your-service/signals" --token "YOUR_TOKEN" --output copy_signals.json
```

Можно передать токен через env:

```bash
set A2A_TOKEN=YOUR_TOKEN
python scripts/copy_wallets_fetch.py --from-url "https://your-service/signals" --output copy_signals.json
```

## 3) (Опционально) фильтр по лидер-кошелькам

Создай `copy_leaders.json` по шаблону `copy_leaders.sample.json`, затем:

```bash
python scripts/copy_wallets_fetch.py --from-url "https://your-service/signals" --leaders-file copy_leaders.json --output copy_signals.json
```

## 4) Формат результата

`copy_signals.json` должен иметь вид:

```json
{
  "signals": [
    {
      "market_id": "0x...",
      "wallet": "0x...",
      "weight": 1.0,
      "max_entry_price": 0.5
    }
  ]
}
```

## 5) Запуск бота

После обновления `copy_signals.json` запускай:

```bash
python main_v2.py
```

В логе появятся строки вида:
- `Copy-trading adapter: raw=... kept=...`
- `Signals merged (LLM/simple + copy): total=...`

## Примечания

- Режим только `paper`.
- Copy-сигналы проходят те же risk-фильтры, что и обычные сигналы.
- Если copy-сигнал и LLM-сигнал на один `market_id`, оставляется более сильный по `expected_ev`.
