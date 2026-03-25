# Настройка MM-стратегии после первой сессии

После набора **10–20 MM-сделок** в paper проверь:

## 1. Распределение exit_reason и PnL

```bash
psql $DATABASE_URL -f schema/pnl_by_exit_reason.sql
```

Или выполни вручную запросы по MM (в конце файла):  
**strategy_id = 'mm'** → группа по `exit_reason`, `COUNT`, `AVG(pnl_usd)`.

- Важно: доля **TP** vs **SL** vs **TIME** и средний `pnl_usd` по каждой группе.

## 2. Логи: place / FILL / timeout

```bash
python scripts/analyze_mm_logs.py clawbot_v2_run.log
```

- **Слишком мало сделок и много тайм-аутов** → лимитки редко исполняются:
  - поднять **max_spread** (например до **0.06**);
  - или снизить **min_best_level_size** до **40**.

- **Сделок много, но почти все уходят в SL/TIME** → TP редко срабатывает:
  - подвинуть **tp_ticks** / **sl_ticks** (например TP **0.01**, SL **0.015**);
  - или уменьшить **position_time_stop_seconds**, чтобы не резать позицию по времени слишком рано.

Параметры в **config.py** → **mm_params**.
