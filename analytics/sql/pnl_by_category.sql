-- PnL по категориям и сторонам по config_version.
-- Заменить '2026-03-05_15m_conservative_v3' на свою версию.
WITH t AS (
    SELECT *
    FROM trades
    WHERE config_version = '2026-03-05_15m_conservative_v3'
      AND exit_ts IS NOT NULL
)
SELECT
    category,
    side,
    COUNT(*)     AS trades,
    SUM(pnl_usd) AS pnl_usd,
    AVG(pnl_usd) AS avg_pnl_usd
FROM t
GROUP BY category, side
ORDER BY category, side;
