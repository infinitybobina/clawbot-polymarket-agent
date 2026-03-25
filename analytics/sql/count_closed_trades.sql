-- Количество закрытых сделок по config_version.
-- В psql: \set config_version '2026-03-05_15m_conservative_v3'
-- затем: \i analytics/sql/count_closed_trades.sql
SELECT COUNT(*) AS closed_trades
FROM trades
WHERE config_version = :'config_version'
  AND exit_ts IS NOT NULL;
