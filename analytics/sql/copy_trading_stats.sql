-- Copy-trading stats from trades table.
--
-- Usage in psql:
--   \c clawbot
--   \set since_utc '2026-03-27 00:00:00+00'
--   \set cfg ''
--   \i 'C:/Dev/Clawbot-polymarket-agent-clean/analytics/sql/copy_trading_stats.sql'
--
-- Note:
--   strategy_id='copy' is recorded for new trades only
--   after main_v2.py patch that persists order source.

WITH base AS (
  SELECT
    trade_id,
    entry_ts,
    exit_ts,
    pnl_usd,
    exit_reason,
    config_version,
    strategy_id
  FROM trades
  WHERE entry_ts >= :'since_utc'::timestamptz
    AND (NULLIF(:'cfg','') IS NULL OR config_version = NULLIF(:'cfg',''))
)
SELECT
  CASE WHEN strategy_id = 'copy' THEN 'copy' ELSE 'non_copy' END AS source_bucket,
  COUNT(*) AS n_opened,
  COUNT(*) FILTER (WHERE exit_ts IS NOT NULL) AS n_closed,
  ROUND(COALESCE(SUM(pnl_usd) FILTER (WHERE exit_ts IS NOT NULL), 0)::numeric, 2) AS sum_pnl_usd,
  ROUND(COALESCE(AVG(pnl_usd) FILTER (WHERE exit_ts IS NOT NULL), 0)::numeric, 2) AS avg_pnl_usd,
  ROUND(
    100.0 * COUNT(*) FILTER (WHERE exit_ts IS NOT NULL AND pnl_usd > 0)
    / NULLIF(COUNT(*) FILTER (WHERE exit_ts IS NOT NULL), 0),
    1
  ) AS winrate_pct
FROM base
GROUP BY source_bucket
ORDER BY source_bucket;

-- Closed copy trades by exit_reason (quick diagnostics).
SELECT
  COALESCE(NULLIF(TRIM(exit_reason), ''), '(null)') AS exit_reason,
  COUNT(*) AS n_closed,
  ROUND(SUM(pnl_usd)::numeric, 2) AS sum_pnl_usd,
  ROUND(AVG(pnl_usd)::numeric, 2) AS avg_pnl_usd
FROM trades
WHERE strategy_id = 'copy'
  AND exit_ts IS NOT NULL
  AND entry_ts >= :'since_utc'::timestamptz
  AND (NULLIF(:'cfg','') IS NULL OR config_version = NULLIF(:'cfg',''))
GROUP BY COALESCE(NULLIF(TRIM(exit_reason), ''), '(null)')
ORDER BY n_closed DESC, exit_reason;
