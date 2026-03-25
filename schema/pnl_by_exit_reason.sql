-- PnL по типу выхода: нормальные SL/TP/TIME vs EXPIRY (инфраструктурный риск).
-- Использовать для оценки стратегии без «шума» от экспираций по 0.01.

-- Сводка по exit_reason (все сделки)
SELECT
  exit_reason,
  COUNT(*) AS trades,
  ROUND(SUM(pnl_usd)::numeric, 2) AS pnl_usd_total,
  ROUND(AVG(pnl_usd)::numeric, 2) AS pnl_usd_avg
FROM trades
WHERE exit_ts IS NOT NULL
GROUP BY exit_reason
ORDER BY exit_reason;

-- Только «живые» выходы (стратегия): SL, TP, TIME — без EXPIRY
SELECT
  COUNT(*) AS trades_live,
  ROUND(SUM(pnl_usd)::numeric, 2) AS pnl_usd_total,
  ROUND(AVG(pnl_usd)::numeric, 2) AS pnl_usd_avg
FROM trades
WHERE exit_ts IS NOT NULL
  AND exit_reason IN ('SL', 'TP', 'TIME');

-- EXPIRY отдельно (инфраструктурный риск: стакан умер до резолва)
SELECT
  COUNT(*) AS trades_expiry,
  ROUND(SUM(pnl_usd)::numeric, 2) AS pnl_usd_expiry
FROM trades
WHERE exit_ts IS NOT NULL
  AND exit_reason = 'EXPIRY';

-- После деплоя фикса (подставь свою дату)
-- WHERE entry_ts >= TIMESTAMPTZ '2026-03-10 10:05:00+03'

-- ========== MM-стратегия: распределение exit_reason и средний pnl по группам ==========
-- Запускать после 10–20 MM-сделок в paper.
SELECT
  exit_reason,
  COUNT(*) AS trades,
  ROUND(SUM(pnl_usd)::numeric, 2) AS pnl_usd_total,
  ROUND(AVG(pnl_usd)::numeric, 2) AS pnl_usd_avg
FROM trades
WHERE exit_ts IS NOT NULL
  AND strategy_id = 'mm'
GROUP BY exit_reason
ORDER BY exit_reason;

-- Сводка по MM: всего сделок и общий PnL
SELECT
  COUNT(*) AS mm_trades,
  ROUND(SUM(pnl_usd)::numeric, 2) AS pnl_usd_total,
  ROUND(AVG(pnl_usd)::numeric, 2) AS pnl_usd_avg
FROM trades
WHERE exit_ts IS NOT NULL
  AND strategy_id = 'mm';
