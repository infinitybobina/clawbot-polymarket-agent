-- Edge Protocol Daily
-- Daily SQL pack for go/no-go decisions on current strategy iteration.
--
-- Usage in psql:
--   \set since_utc '2026-03-27 10:44:00+00'
--   \set cfg 'exp_A0'
--   \i analytics/sql/edge_protocol_daily.sql
--
-- If you want NO config filter:
--   \set cfg ''
--
-- Notes:
-- - Uses NULLIF(:'cfg','') so empty cfg disables config_version filter.
-- - Run all 5 blocks daily and compare across config_version.

-- =========================================================
-- Q1) CORE KPI SNAPSHOT
-- =========================================================
WITH b AS (
  SELECT *
  FROM trades
  WHERE exit_ts IS NOT NULL
    AND exit_ts >= :'since_utc'::timestamptz
    AND (NULLIF(:'cfg','') IS NULL OR config_version = NULLIF(:'cfg',''))
)
SELECT
  COUNT(*) AS n_closed,
  ROUND(SUM(pnl_usd)::numeric, 2) AS sum_pnl_usd,
  ROUND(AVG(pnl_usd)::numeric, 2) AS avg_pnl_usd,
  ROUND(100.0 * COUNT(*) FILTER (WHERE pnl_usd > 0) / NULLIF(COUNT(*), 0), 1) AS winrate_pct,
  ROUND(AVG(CASE WHEN pnl_usd > 0 THEN pnl_usd END)::numeric, 2) AS avg_win_usd,
  ROUND(AVG(CASE WHEN pnl_usd < 0 THEN pnl_usd END)::numeric, 2) AS avg_loss_usd,
  ROUND(
    (
      (COUNT(*) FILTER (WHERE pnl_usd > 0)::numeric / NULLIF(COUNT(*), 0))
      * COALESCE(AVG(CASE WHEN pnl_usd > 0 THEN pnl_usd END), 0)
      +
      (1 - (COUNT(*) FILTER (WHERE pnl_usd > 0)::numeric / NULLIF(COUNT(*), 0)))
      * COALESCE(AVG(CASE WHEN pnl_usd < 0 THEN pnl_usd END), 0)
    )::numeric,
    2
  ) AS expectancy_usd_per_trade
FROM b;

-- =========================================================
-- Q2) TAIL-RISK CONTRIBUTION
-- =========================================================
WITH b AS (
  SELECT pnl_usd
  FROM trades
  WHERE exit_ts IS NOT NULL
    AND exit_ts >= :'since_utc'::timestamptz
    AND (NULLIF(:'cfg','') IS NULL OR config_version = NULLIF(:'cfg',''))
)
SELECT
  COUNT(*) FILTER (WHERE pnl_usd <= -100) AS tail_n_le_100,
  ROUND(SUM(CASE WHEN pnl_usd <= -100 THEN pnl_usd ELSE 0 END)::numeric, 2) AS tail_pnl_le_100,
  ROUND(SUM(pnl_usd)::numeric, 2) AS total_pnl,
  ROUND(
    100.0 * ABS(SUM(CASE WHEN pnl_usd <= -100 THEN pnl_usd ELSE 0 END))
    / NULLIF(ABS(SUM(pnl_usd)), 0),
    1
  ) AS tail_share_pct_of_total_abs
FROM b;

-- =========================================================
-- Q3) EXIT QUALITY (SL/TP/EXPIRY/TIME_STOP + SL vs уровень стопа)
-- Примечание: старые БД без sl_at_exit — используем sl_price (уровень SL на входе).
-- Точный overshoot как в боте: см. analytics/sql/migrations/001_trades_sl_tp_at_exit.sql
-- =========================================================
WITH b AS (
  SELECT
    exit_reason,
    pnl_usd,
    exit_price,
    sl_price
  FROM trades
  WHERE exit_ts IS NOT NULL
    AND exit_ts >= :'since_utc'::timestamptz
    AND (NULLIF(:'cfg','') IS NULL OR config_version = NULLIF(:'cfg',''))
)
SELECT
  exit_reason,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usd)::numeric, 2) AS sum_pnl_usd,
  ROUND(AVG(pnl_usd)::numeric, 2) AS avg_pnl_usd,
  ROUND(
    AVG(CASE WHEN exit_reason = 'SL' AND sl_price IS NOT NULL THEN (exit_price - sl_price) END)::numeric,
    4
  ) AS avg_exit_minus_sl_price_on_sl
FROM b
GROUP BY exit_reason
ORDER BY sum_pnl_usd ASC;

-- =========================================================
-- Q4) EDGE CALIBRATION (llm_score buckets)
-- =========================================================
WITH b AS (
  SELECT
    llm_score,
    pnl_usd,
    pnl_pct
  FROM trades
  WHERE exit_ts IS NOT NULL
    AND exit_ts >= :'since_utc'::timestamptz
    AND llm_score IS NOT NULL
    AND (NULLIF(:'cfg','') IS NULL OR config_version = NULLIF(:'cfg',''))
)
SELECT
  width_bucket(llm_score, 0.0, 1.0, 5) AS score_bin_1_5,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usd)::numeric, 2) AS sum_pnl_usd,
  ROUND(AVG(pnl_usd)::numeric, 2) AS avg_pnl_usd,
  ROUND(AVG(pnl_pct)::numeric, 4) AS avg_pnl_pct,
  ROUND(100.0 * COUNT(*) FILTER (WHERE pnl_usd > 0) / NULLIF(COUNT(*), 0), 1) AS winrate_pct
FROM b
GROUP BY score_bin_1_5
ORDER BY score_bin_1_5;

-- =========================================================
-- Q5) SEGMENT MAP (score x spread x hold)
-- =========================================================
WITH b AS (
  SELECT
    llm_score,
    spread_at_entry,
    entry_ts,
    exit_ts,
    pnl_usd,
    pnl_pct
  FROM trades
  WHERE exit_ts IS NOT NULL
    AND entry_ts IS NOT NULL
    AND llm_score IS NOT NULL
    AND spread_at_entry IS NOT NULL
    AND exit_ts >= :'since_utc'::timestamptz
    AND (NULLIF(:'cfg','') IS NULL OR config_version = NULLIF(:'cfg',''))
),
seg AS (
  SELECT
    CASE
      WHEN llm_score < 0.20 THEN 's1_0.00-0.20'
      WHEN llm_score < 0.40 THEN 's2_0.20-0.40'
      WHEN llm_score < 0.60 THEN 's3_0.40-0.60'
      WHEN llm_score < 0.80 THEN 's4_0.60-0.80'
      ELSE 's5_0.80-1.00'
    END AS score_bin,
    CASE
      WHEN spread_at_entry < 0.02 THEN 'sp1_<0.02'
      WHEN spread_at_entry < 0.04 THEN 'sp2_0.02-0.04'
      WHEN spread_at_entry < 0.06 THEN 'sp3_0.04-0.06'
      ELSE 'sp4_0.06+'
    END AS spread_bin,
    CASE
      WHEN EXTRACT(EPOCH FROM (exit_ts - entry_ts)) / 3600.0 < 1 THEN 'h1_<1h'
      WHEN EXTRACT(EPOCH FROM (exit_ts - entry_ts)) / 3600.0 < 4 THEN 'h2_1-4h'
      WHEN EXTRACT(EPOCH FROM (exit_ts - entry_ts)) / 3600.0 < 12 THEN 'h3_4-12h'
      ELSE 'h4_12h+'
    END AS hold_bin,
    pnl_usd,
    pnl_pct
  FROM b
)
SELECT
  score_bin,
  spread_bin,
  hold_bin,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usd)::numeric, 2) AS sum_pnl_usd,
  ROUND(AVG(pnl_usd)::numeric, 2) AS avg_pnl_usd,
  ROUND(AVG(pnl_pct)::numeric, 4) AS avg_pnl_pct,
  ROUND(100.0 * COUNT(*) FILTER (WHERE pnl_usd > 0) / NULLIF(COUNT(*), 0), 1) AS winrate_pct
FROM seg
GROUP BY score_bin, spread_bin, hold_bin
HAVING COUNT(*) >= 2
ORDER BY avg_pnl_usd DESC;
