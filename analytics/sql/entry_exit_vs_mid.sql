-- Диагностика: платим ли стабильно спред на входе. entry_price vs mid_at_entry по config_version.
-- В psql: \set config_version '2026-03-05_15m_conservative_v3'
WITH t AS (
    SELECT entry_price, exit_price, mid_at_entry, spread_at_entry, pnl_usd
    FROM trades
    WHERE config_version = '2026-03-05_15m_conservative_v3'
      AND exit_ts IS NOT NULL
      AND mid_at_entry IS NOT NULL
)
SELECT
    COUNT(*) AS trades,
    ROUND(AVG(entry_price - mid_at_entry)::numeric, 4) AS avg_entry_minus_mid,
    ROUND(AVG(entry_price)::numeric, 4) AS avg_entry_price,
    ROUND(AVG(mid_at_entry)::numeric, 4) AS avg_mid_at_entry,
    ROUND(AVG(exit_price)::numeric, 4) AS avg_exit_price,
    ROUND(SUM(pnl_usd)::numeric, 2) AS total_pnl_usd
FROM t;
