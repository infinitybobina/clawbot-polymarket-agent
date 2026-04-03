-- Edge Protocol Compare (A vs B)
-- Compare two config versions on the same time window.
--
-- Usage:
--   \set since_utc '2026-03-27 10:44:00+00'
--   \set cfg_a 'exp_A0'
--   \set cfg_b 'exp_A1'
--   \i analytics/sql/edge_protocol_compare.sql

-- =========================================================
-- C1) KPI compare + deltas
-- =========================================================
WITH b AS (
  SELECT
    config_version,
    pnl_usd
  FROM trades
  WHERE exit_ts IS NOT NULL
    AND exit_ts >= :'since_utc'::timestamptz
    AND config_version IN (:'cfg_a', :'cfg_b')
),
agg AS (
  SELECT
    config_version,
    COUNT(*) AS n_closed,
    SUM(pnl_usd) AS sum_pnl_usd,
    AVG(pnl_usd) AS avg_pnl_usd,
    COUNT(*) FILTER (WHERE pnl_usd > 0)::numeric / NULLIF(COUNT(*), 0) AS winrate,
    AVG(CASE WHEN pnl_usd > 0 THEN pnl_usd END) AS avg_win_usd,
    AVG(CASE WHEN pnl_usd < 0 THEN pnl_usd END) AS avg_loss_usd
  FROM b
  GROUP BY config_version
),
fx AS (
  SELECT
    config_version,
    n_closed,
    ROUND(sum_pnl_usd::numeric, 2) AS sum_pnl_usd,
    ROUND(avg_pnl_usd::numeric, 2) AS avg_pnl_usd,
    ROUND((100.0 * winrate)::numeric, 1) AS winrate_pct,
    ROUND(avg_win_usd::numeric, 2) AS avg_win_usd,
    ROUND(avg_loss_usd::numeric, 2) AS avg_loss_usd,
    ROUND((winrate * COALESCE(avg_win_usd,0) + (1 - winrate) * COALESCE(avg_loss_usd,0))::numeric, 2) AS expectancy_usd_per_trade
  FROM agg
),
a AS (
  SELECT * FROM fx WHERE config_version = :'cfg_a'
),
bv AS (
  SELECT * FROM fx WHERE config_version = :'cfg_b'
)
SELECT
  'A' AS side,
  a.config_version,
  a.n_closed,
  a.sum_pnl_usd,
  a.avg_pnl_usd,
  a.winrate_pct,
  a.avg_win_usd,
  a.avg_loss_usd,
  a.expectancy_usd_per_trade
FROM a
UNION ALL
SELECT
  'B' AS side,
  bv.config_version,
  bv.n_closed,
  bv.sum_pnl_usd,
  bv.avg_pnl_usd,
  bv.winrate_pct,
  bv.avg_win_usd,
  bv.avg_loss_usd,
  bv.expectancy_usd_per_trade
FROM bv
UNION ALL
SELECT
  'DELTA_B_MINUS_A' AS side,
  (:'cfg_b' || ' - ' || :'cfg_a') AS config_version,
  (COALESCE(bv.n_closed,0) - COALESCE(a.n_closed,0)) AS n_closed,
  ROUND((COALESCE(bv.sum_pnl_usd,0) - COALESCE(a.sum_pnl_usd,0))::numeric, 2) AS sum_pnl_usd,
  ROUND((COALESCE(bv.avg_pnl_usd,0) - COALESCE(a.avg_pnl_usd,0))::numeric, 2) AS avg_pnl_usd,
  ROUND((COALESCE(bv.winrate_pct,0) - COALESCE(a.winrate_pct,0))::numeric, 1) AS winrate_pct,
  ROUND((COALESCE(bv.avg_win_usd,0) - COALESCE(a.avg_win_usd,0))::numeric, 2) AS avg_win_usd,
  ROUND((COALESCE(bv.avg_loss_usd,0) - COALESCE(a.avg_loss_usd,0))::numeric, 2) AS avg_loss_usd,
  ROUND((COALESCE(bv.expectancy_usd_per_trade,0) - COALESCE(a.expectancy_usd_per_trade,0))::numeric, 2) AS expectancy_usd_per_trade
FROM a
FULL OUTER JOIN bv ON TRUE;

-- =========================================================
-- C2) Tail-risk compare
-- =========================================================
WITH b AS (
  SELECT
    config_version,
    pnl_usd
  FROM trades
  WHERE exit_ts IS NOT NULL
    AND exit_ts >= :'since_utc'::timestamptz
    AND config_version IN (:'cfg_a', :'cfg_b')
)
SELECT
  config_version,
  COUNT(*) FILTER (WHERE pnl_usd <= -100) AS tail_n_le_100,
  ROUND(SUM(CASE WHEN pnl_usd <= -100 THEN pnl_usd ELSE 0 END)::numeric, 2) AS tail_pnl_le_100,
  ROUND(SUM(pnl_usd)::numeric, 2) AS total_pnl,
  ROUND(
    100.0 * ABS(SUM(CASE WHEN pnl_usd <= -100 THEN pnl_usd ELSE 0 END))
    / NULLIF(ABS(SUM(pnl_usd)), 0),
    1
  ) AS tail_share_pct_of_total_abs
FROM b
GROUP BY config_version
ORDER BY config_version;

-- =========================================================
-- C3) Exit reason quality compare (sl_price если нет sl_at_exit в БД)
-- =========================================================
WITH b AS (
  SELECT
    config_version,
    exit_reason,
    pnl_usd,
    exit_price,
    sl_price
  FROM trades
  WHERE exit_ts IS NOT NULL
    AND exit_ts >= :'since_utc'::timestamptz
    AND config_version IN (:'cfg_a', :'cfg_b')
)
SELECT
  config_version,
  exit_reason,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usd)::numeric, 2) AS sum_pnl_usd,
  ROUND(AVG(pnl_usd)::numeric, 2) AS avg_pnl_usd,
  ROUND(
    AVG(CASE WHEN exit_reason = 'SL' AND sl_price IS NOT NULL THEN (exit_price - sl_price) END)::numeric,
    4
  ) AS avg_exit_minus_sl_price_on_sl
FROM b
GROUP BY config_version, exit_reason
ORDER BY config_version, sum_pnl_usd ASC;
