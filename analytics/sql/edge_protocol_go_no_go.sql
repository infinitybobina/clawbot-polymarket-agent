-- Edge Protocol GO / NO-GO
-- One-screen "traffic light" decision for current config_version.
--
-- Usage:
--   \set since_utc '2026-03-27 10:44:00+00'
--   \set cfg 'exp_A1'         -- set '' to disable config filter
--   \set min_closed 30
--   \set min_expectancy 0
--   \set max_tail_share 35
--   \set min_winrate 35
--   \i analytics/sql/edge_protocol_go_no_go.sql

WITH base AS (
  SELECT
    pnl_usd
  FROM trades
  WHERE exit_ts IS NOT NULL
    AND exit_ts >= :'since_utc'::timestamptz
    AND (NULLIF(:'cfg','') IS NULL OR config_version = NULLIF(:'cfg',''))
),
kpi AS (
  SELECT
    COUNT(*) AS n_closed,
    COALESCE(SUM(pnl_usd), 0) AS sum_pnl_usd,
    COALESCE(AVG(pnl_usd), 0) AS avg_pnl_usd,
    COALESCE(AVG(CASE WHEN pnl_usd > 0 THEN pnl_usd END), 0) AS avg_win_usd,
    COALESCE(AVG(CASE WHEN pnl_usd < 0 THEN pnl_usd END), 0) AS avg_loss_usd,
    (COUNT(*) FILTER (WHERE pnl_usd > 0)::numeric / NULLIF(COUNT(*), 0)) AS winrate,
    COALESCE(SUM(CASE WHEN pnl_usd <= -100 THEN pnl_usd ELSE 0 END), 0) AS tail_pnl_usd
  FROM base
),
scored AS (
  SELECT
    n_closed,
    ROUND(sum_pnl_usd::numeric, 2) AS sum_pnl_usd,
    ROUND(avg_pnl_usd::numeric, 2) AS avg_pnl_usd,
    ROUND((100.0 * COALESCE(winrate, 0))::numeric, 1) AS winrate_pct,
    ROUND((COALESCE(winrate, 0) * avg_win_usd + (1 - COALESCE(winrate, 0)) * avg_loss_usd)::numeric, 2) AS expectancy_usd_per_trade,
    ROUND(
      (
        100.0 * ABS(tail_pnl_usd)
        / NULLIF(ABS(sum_pnl_usd), 0)
      )::numeric,
      1
    ) AS tail_share_pct,
    -- individual checks
    CASE WHEN n_closed >= (:'min_closed')::int THEN 1 ELSE 0 END AS ok_n_closed,
    CASE WHEN (COALESCE(winrate, 0) * avg_win_usd + (1 - COALESCE(winrate, 0)) * avg_loss_usd) >= (:'min_expectancy')::numeric THEN 1 ELSE 0 END AS ok_expectancy,
    CASE
      WHEN ABS(sum_pnl_usd) < 1e-9 THEN 0
      WHEN (100.0 * ABS(tail_pnl_usd) / ABS(sum_pnl_usd)) <= (:'max_tail_share')::numeric THEN 1
      ELSE 0
    END AS ok_tail_share,
    CASE WHEN (100.0 * COALESCE(winrate, 0)) >= (:'min_winrate')::numeric THEN 1 ELSE 0 END AS ok_winrate
  FROM kpi
),
decision AS (
  SELECT
    *,
    (ok_n_closed + ok_expectancy + ok_tail_share + ok_winrate) AS ok_score
  FROM scored
)
SELECT
  COALESCE(NULLIF(:'cfg',''), 'ALL_CONFIGS') AS config_scope,
  :'since_utc' AS since_utc,
  n_closed,
  sum_pnl_usd,
  avg_pnl_usd,
  winrate_pct,
  expectancy_usd_per_trade,
  tail_share_pct,
  ok_n_closed,
  ok_expectancy,
  ok_tail_share,
  ok_winrate,
  ok_score,
  CASE
    WHEN ok_score = 4 THEN 'GREEN_GO'
    WHEN ok_score >= 2 THEN 'YELLOW_HOLD'
    ELSE 'RED_STOP'
  END AS decision
FROM decision;
