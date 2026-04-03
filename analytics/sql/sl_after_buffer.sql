-- SL-статистика после деплоя sl_trigger_buffer.
-- Подставь UTC-границу рестарта; при необходимости отфильтруй по config_version.
--
-- Если в таблице нет sl_at_exit — один раз выполни:
--   \i analytics/sql/migrations/001_trades_sl_tp_at_exit.sql
-- Ниже для совместимости со старой схемой используется sl_price (уровень на входе).

-- 1) Все SL после границы
SELECT
    COUNT(*) AS n_sl,
    AVG(exit_price - sl_price) FILTER (WHERE exit_price IS NOT NULL AND sl_price IS NOT NULL) AS avg_exit_minus_sl_price,
    SUM(pnl_usd) AS sum_pnl_sl
FROM trades
WHERE exit_ts IS NOT NULL
  AND exit_ts >= TIMESTAMPTZ '2026-03-19 20:06:20+00'  -- <-- обновляй
  AND exit_reason = 'SL';

-- 2) Только строки с известной версией (как в .env бота)
-- AND config_version = '2026-03-05_15m_conservative_v3'

-- 3) Разрез: legacy (NULL) vs версионированные
SELECT
    CASE WHEN config_version IS NULL THEN 'legacy_null' ELSE 'versioned' END AS bucket,
    COUNT(*) AS n_sl,
    AVG(exit_price - sl_price) FILTER (WHERE exit_price IS NOT NULL AND sl_price IS NOT NULL) AS avg_exit_minus_sl_price,
    SUM(pnl_usd) AS sum_pnl_sl
FROM trades
WHERE exit_ts IS NOT NULL
  AND exit_ts >= TIMESTAMPTZ '2026-03-19 20:06:20+00'
  AND exit_reason = 'SL'
GROUP BY 1
ORDER BY 1;
