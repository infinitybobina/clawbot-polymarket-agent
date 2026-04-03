-- Re-entry audit for "new bot" period: opened_after_split only.
-- Goal: find repeated entries into same market_id and see if it correlates with SL clusters.
--
-- Usage in psql:
--   \c clawbot
--   \set since_utc '2026-03-22 14:38:00+03'
--   \set split_utc '2026-03-27 13:38:00+03'
--   \set cfg ''
--   \set mids_csv ''   -- optional: comma-separated market_id list (no quotes), e.g. 0xaaa,0xbbb
--   \i 'C:/Dev/Clawbot-polymarket-agent-clean/analytics/sql/reentry_audit_opened_after_split.sql'

-- =========================================================
-- 1) Markets with multiple entries (opened_after_split)
-- =========================================================
WITH base AS (
  SELECT
    trade_id,
    market_id,
    COALESCE(NULLIF(TRIM(category), ''), '(null)') AS category,
    entry_ts,
    exit_ts,
    COALESCE(NULLIF(TRIM(exit_reason), ''), '(null)') AS exit_reason,
    pnl_usd
  FROM trades
  WHERE entry_ts IS NOT NULL
    AND entry_ts >= :'split_utc'::timestamptz
    AND (exit_ts IS NULL OR exit_ts >= :'since_utc'::timestamptz)
    AND (NULLIF(:'cfg','') IS NULL OR config_version = NULLIF(:'cfg',''))
)
SELECT
  market_id,
  category,
  COUNT(*) AS n_entries,
  COUNT(*) FILTER (WHERE exit_ts IS NOT NULL) AS n_closed,
  COUNT(*) FILTER (WHERE exit_reason = 'SL') AS n_sl,
  COUNT(*) FILTER (WHERE exit_reason = 'TP') AS n_tp,
  COUNT(*) FILTER (WHERE exit_reason = 'TIME_STOP') AS n_time_stop,
  ROUND(COALESCE(SUM(pnl_usd) FILTER (WHERE exit_ts IS NOT NULL), 0)::numeric, 2) AS sum_pnl_usd_closed,
  STRING_AGG(
    CASE
      WHEN exit_ts IS NULL THEN 'OPEN'
      ELSE exit_reason || '(' || COALESCE(TO_CHAR(pnl_usd, 'FM999990D00'), 'null') || ')'
    END,
    ' → '
    ORDER BY entry_ts
  ) AS sequence
FROM base
GROUP BY market_id, category
HAVING COUNT(*) >= 2
ORDER BY sum_pnl_usd_closed ASC, n_entries DESC, market_id
LIMIT 50;

-- =========================================================
-- 2) Detailed timeline for the worst repeated markets
--    (take market_ids from query #1 and paste into the IN (...) list)
-- =========================================================
-- Example:
--   \set mids_csv '0xabc...,0xdef...'
--   then run the query below.
WITH base AS (
  SELECT
    trade_id,
    market_id,
    COALESCE(NULLIF(TRIM(category), ''), '(null)') AS category,
    entry_ts,
    exit_ts,
    COALESCE(NULLIF(TRIM(exit_reason), ''), '(null)') AS exit_reason,
    pnl_usd,
    entry_price,
    exit_price,
    spread_at_entry,
    book_ok_at_entry
  FROM trades
  WHERE entry_ts IS NOT NULL
    AND entry_ts >= :'split_utc'::timestamptz
    AND (exit_ts IS NULL OR exit_ts >= :'since_utc'::timestamptz)
    AND (NULLIF(:'cfg','') IS NULL OR config_version = NULLIF(:'cfg',''))
    AND (
      NULLIF(:'mids_csv','') IS NULL
      OR market_id = ANY(regexp_split_to_array(:'mids_csv', '\s*,\s*'))
    )
)
SELECT
  trade_id,
  market_id,
  category,
  entry_ts,
  exit_ts,
  exit_reason,
  pnl_usd,
  entry_price,
  exit_price,
  spread_at_entry,
  book_ok_at_entry
FROM base
ORDER BY market_id, entry_ts;

