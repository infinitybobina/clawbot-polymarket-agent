-- Diagnose "new bot" behavior: opened_after_split only.
-- Focus: where SL losses concentrate (category × exit_reason), and whether entry book looks thin.
--
-- Usage in psql:
--   \c clawbot
--   \set since_utc '2026-03-22 14:38:00+03'
--   \set split_utc '2026-03-27 13:38:00+03'
--   \set cfg ''
--   \i 'C:/Dev/Clawbot-polymarket-agent-clean/analytics/sql/opened_after_split_by_category.sql'

-- =========================================================
-- 1) Category × exit_reason (PnL concentration map)
-- =========================================================
WITH base AS (
  SELECT
    COALESCE(NULLIF(TRIM(category), ''), '(null)') AS category,
    COALESCE(NULLIF(TRIM(exit_reason), ''), '(null)') AS exit_reason,
    pnl_usd,
    entry_ts,
    exit_ts,
    spread_at_entry,
    book_ok_at_entry
  FROM trades
  WHERE exit_ts IS NOT NULL
    AND entry_ts IS NOT NULL
    AND exit_ts >= :'since_utc'::timestamptz
    AND entry_ts >= :'split_utc'::timestamptz
    AND (NULLIF(:'cfg','') IS NULL OR config_version = NULLIF(:'cfg',''))
)
SELECT
  category,
  exit_reason,
  COUNT(*) AS n_closed,
  ROUND(SUM(pnl_usd)::numeric, 2) AS sum_pnl_usd,
  ROUND(AVG(pnl_usd)::numeric, 2) AS avg_pnl_usd,
  ROUND(100.0 * COUNT(*) FILTER (WHERE pnl_usd > 0) / NULLIF(COUNT(*), 0), 1) AS winrate_pct,
  ROUND(AVG(spread_at_entry)::numeric, 4) AS avg_spread_at_entry,
  ROUND(100.0 * AVG(CASE WHEN book_ok_at_entry THEN 1 ELSE 0 END)::numeric, 1) AS book_ok_pct
FROM base
GROUP BY category, exit_reason
HAVING COUNT(*) >= 2
ORDER BY sum_pnl_usd ASC, n_closed DESC, category, exit_reason;

-- =========================================================
-- 2) Category totals (quick ranking)
-- =========================================================
WITH base AS (
  SELECT
    COALESCE(NULLIF(TRIM(category), ''), '(null)') AS category,
    pnl_usd
  FROM trades
  WHERE exit_ts IS NOT NULL
    AND entry_ts IS NOT NULL
    AND exit_ts >= :'since_utc'::timestamptz
    AND entry_ts >= :'split_utc'::timestamptz
    AND (NULLIF(:'cfg','') IS NULL OR config_version = NULLIF(:'cfg',''))
)
SELECT
  category,
  COUNT(*) AS n_closed,
  ROUND(SUM(pnl_usd)::numeric, 2) AS sum_pnl_usd,
  ROUND(AVG(pnl_usd)::numeric, 2) AS avg_pnl_usd,
  ROUND(100.0 * COUNT(*) FILTER (WHERE pnl_usd > 0) / NULLIF(COUNT(*), 0), 1) AS winrate_pct
FROM base
GROUP BY category
ORDER BY sum_pnl_usd ASC, n_closed DESC, category;

-- =========================================================
-- 3) Worst SL trades (inspect entry book & sizing signals)
-- =========================================================
SELECT
  trade_id,
  entry_ts,
  exit_ts,
  market_id,
  COALESCE(NULLIF(TRIM(category), ''), '(null)') AS category,
  pnl_usd,
  pnl_pct,
  size_usd,
  entry_price,
  exit_price,
  spread_at_entry,
  mid_at_entry,
  book_ok_at_entry
FROM trades
WHERE exit_ts IS NOT NULL
  AND entry_ts IS NOT NULL
  AND exit_reason = 'SL'
  AND exit_ts >= :'since_utc'::timestamptz
  AND entry_ts >= :'split_utc'::timestamptz
  AND (NULLIF(:'cfg','') IS NULL OR config_version = NULLIF(:'cfg',''))
ORDER BY pnl_usd ASC NULLS LAST
LIMIT 25;
