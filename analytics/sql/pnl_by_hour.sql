-- PnL по времени суток (час входа, без таймзоны — часы как в entry_ts).
-- Заменить '2026-03-05_15m_conservative_v3' на свою версию.
WITH t AS (
    SELECT *,
           EXTRACT(HOUR FROM entry_ts) AS entry_hour
    FROM trades
    WHERE config_version = '2026-03-05_15m_conservative_v3'
      AND exit_ts IS NOT NULL
)
SELECT
    entry_hour,
    COUNT(*)     AS trades,
    SUM(pnl_usd) AS pnl_usd,
    AVG(pnl_usd) AS avg_pnl_usd
FROM t
GROUP BY entry_hour
ORDER BY entry_hour;
