-- PnL по бакетам llm_score (0–0.2, 0.2–0.4, … 0.8–1.0).
-- Заменить '2026-03-05_15m_conservative_v3' на свою версию.
WITH t AS (
    SELECT *,
           width_bucket(llm_score, 0.0, 1.0, 5) AS score_bucket
    FROM trades
    WHERE config_version = '2026-03-05_15m_conservative_v3'
      AND exit_ts IS NOT NULL
)
SELECT
    score_bucket,
    MIN(llm_score)    AS bucket_min_score,
    MAX(llm_score)    AS bucket_max_score,
    COUNT(*)          AS trades,
    SUM(pnl_usd)      AS pnl_usd,
    AVG(pnl_usd)      AS avg_pnl_usd
FROM t
GROUP BY score_bucket
ORDER BY score_bucket;
