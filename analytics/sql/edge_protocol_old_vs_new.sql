-- Edge Protocol: старые vs новые сделки (разрез по порогу времени)
--
-- Порог по умолчанию в примерах: 2026-03-27 17:04+03 = 2026-03-27 14:04:00+00
--
-- Usage в psql:
--   \c clawbot
--   \set since_utc '2026-03-22 14:38:00+03'
--   \set split_utc '2026-03-27 13:38:00+03'
--   \set cfg ''
--   \i 'C:/Dev/Clawbot-polymarket-agent-clean/analytics/sql/edge_protocol_old_vs_new.sql'
--
-- cfg '' = без фильтра по config_version; иначе \set cfg 'твоя_версия'

-- =========================================================
-- 1) По ВХОДУ: позиция открыта до / после порога (главный смысл «старый бот / новый бот»)
-- =========================================================
WITH base AS (
  SELECT
    pnl_usd,
    pnl_pct,
    exit_reason,
    entry_ts,
    exit_ts,
    CASE
      WHEN entry_ts < :'split_utc'::timestamptz THEN 'opened_before_split'
      ELSE 'opened_after_split'
    END AS period
  FROM trades
  WHERE exit_ts IS NOT NULL
    AND entry_ts IS NOT NULL
    AND exit_ts >= :'since_utc'::timestamptz
    AND (NULLIF(:'cfg','') IS NULL OR config_version = NULLIF(:'cfg',''))
)
SELECT
  period,
  COUNT(*) AS n_closed,
  ROUND(SUM(pnl_usd)::numeric, 2) AS sum_pnl_usd,
  ROUND(AVG(pnl_usd)::numeric, 2) AS avg_pnl_usd,
  ROUND(100.0 * COUNT(*) FILTER (WHERE pnl_usd > 0) / NULLIF(COUNT(*), 0), 1) AS winrate_pct,
  COUNT(*) FILTER (WHERE exit_reason = 'SL') AS n_sl,
  COUNT(*) FILTER (WHERE exit_reason = 'TP') AS n_tp,
  COUNT(*) FILTER (WHERE exit_reason = 'TIME_STOP') AS n_time_stop,
  COUNT(*) FILTER (WHERE pnl_usd <= -100) AS tail_n_le_100
FROM base
GROUP BY period
ORDER BY period;

-- =========================================================
-- 2) По ВЫХОДУ: закрыта до / после порога (когда в логе/Telegram пришло закрытие)
-- =========================================================
WITH base AS (
  SELECT
    pnl_usd,
    exit_reason,
    exit_ts,
    CASE
      WHEN exit_ts < :'split_utc'::timestamptz THEN 'closed_before_split'
      ELSE 'closed_after_split'
    END AS period
  FROM trades
  WHERE exit_ts IS NOT NULL
    AND exit_ts >= :'since_utc'::timestamptz
    AND (NULLIF(:'cfg','') IS NULL OR config_version = NULLIF(:'cfg',''))
)
SELECT
  period,
  COUNT(*) AS n_closed,
  ROUND(SUM(pnl_usd)::numeric, 2) AS sum_pnl_usd,
  ROUND(AVG(pnl_usd)::numeric, 2) AS avg_pnl_usd,
  ROUND(100.0 * COUNT(*) FILTER (WHERE pnl_usd > 0) / NULLIF(COUNT(*), 0), 1) AS winrate_pct,
  COUNT(*) FILTER (WHERE exit_reason = 'SL') AS n_sl,
  COUNT(*) FILTER (WHERE exit_reason = 'TP') AS n_tp,
  COUNT(*) FILTER (WHERE exit_reason = 'TIME_STOP') AS n_time_stop,
  COUNT(*) FILTER (WHERE pnl_usd <= -100) AS tail_n_le_100
FROM base
GROUP BY period
ORDER BY period;

-- =========================================================
-- 3) Пересечение: 2x2 (открыто до/после × закрыто до/после) — быстро увидеть «хвост» старых позиций
-- =========================================================
WITH base AS (
  SELECT
    pnl_usd,
    exit_reason,
    CASE WHEN entry_ts < :'split_utc'::timestamptz THEN 'open_before' ELSE 'open_after' END AS open_bucket,
    CASE WHEN exit_ts < :'split_utc'::timestamptz THEN 'close_before' ELSE 'close_after' END AS close_bucket
  FROM trades
  WHERE exit_ts IS NOT NULL
    AND entry_ts IS NOT NULL
    AND exit_ts >= :'since_utc'::timestamptz
    AND (NULLIF(:'cfg','') IS NULL OR config_version = NULLIF(:'cfg',''))
)
SELECT
  open_bucket,
  close_bucket,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usd)::numeric, 2) AS sum_pnl_usd,
  ROUND(AVG(pnl_usd)::numeric, 2) AS avg_pnl_usd
FROM base
GROUP BY open_bucket, close_bucket
ORDER BY open_bucket, close_bucket;

-- =========================================================
-- 4) Только opened_after_split: по exit_reason (оценка «нового» бота)
-- =========================================================
WITH base AS (
  SELECT
    pnl_usd,
    COALESCE(NULLIF(TRIM(exit_reason), ''), '(null)') AS exit_reason,
    entry_ts,
    exit_ts
  FROM trades
  WHERE exit_ts IS NOT NULL
    AND entry_ts IS NOT NULL
    AND exit_ts >= :'since_utc'::timestamptz
    AND entry_ts >= :'split_utc'::timestamptz
    AND (NULLIF(:'cfg','') IS NULL OR config_version = NULLIF(:'cfg',''))
)
SELECT
  exit_reason,
  COUNT(*) AS n_closed,
  ROUND(SUM(pnl_usd)::numeric, 2) AS sum_pnl_usd,
  ROUND(AVG(pnl_usd)::numeric, 2) AS avg_pnl_usd,
  ROUND(100.0 * COUNT(*) FILTER (WHERE pnl_usd > 0) / NULLIF(COUNT(*), 0), 1) AS winrate_pct,
  COUNT(*) FILTER (WHERE pnl_usd <= -100) AS tail_n_le_100
FROM base
GROUP BY exit_reason
ORDER BY n_closed DESC, exit_reason;

-- =========================================================
-- 5) Только opened_after_split: по дню закрытия (UTC)
-- =========================================================
WITH base AS (
  SELECT
    pnl_usd,
    exit_reason,
    (exit_ts AT TIME ZONE 'UTC')::date AS exit_day_utc,
    entry_ts,
    exit_ts
  FROM trades
  WHERE exit_ts IS NOT NULL
    AND entry_ts IS NOT NULL
    AND exit_ts >= :'since_utc'::timestamptz
    AND entry_ts >= :'split_utc'::timestamptz
    AND (NULLIF(:'cfg','') IS NULL OR config_version = NULLIF(:'cfg',''))
)
SELECT
  exit_day_utc,
  COUNT(*) AS n_closed,
  ROUND(SUM(pnl_usd)::numeric, 2) AS sum_pnl_usd,
  ROUND(AVG(pnl_usd)::numeric, 2) AS avg_pnl_usd,
  ROUND(100.0 * COUNT(*) FILTER (WHERE pnl_usd > 0) / NULLIF(COUNT(*), 0), 1) AS winrate_pct,
  COUNT(*) FILTER (WHERE exit_reason = 'SL') AS n_sl,
  COUNT(*) FILTER (WHERE exit_reason = 'TP') AS n_tp,
  COUNT(*) FILTER (WHERE exit_reason = 'TIME_STOP') AS n_time_stop
FROM base
GROUP BY exit_day_utc
ORDER BY exit_day_utc;
