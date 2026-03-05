-- Эквити-кривая по дате и дроудаун по эквити.
-- Заменить '2026-03-05_15m_conservative_v3' на свою config_version или в psql: \set config_version '...' и подставить :'config_version'.

-- 1) Эквити-кривая по времени (по дням)
WITH t AS (
    SELECT *
    FROM trades
    WHERE config_version = '2026-03-05_15m_conservative_v3'
      AND exit_ts IS NOT NULL
),
pnl AS (
    SELECT
        exit_ts::date AS d,
        SUM(pnl_usd)  AS day_pnl
    FROM t
    GROUP BY exit_ts::date
    ORDER BY d
)
SELECT
    d,
    day_pnl,
    SUM(day_pnl) OVER (ORDER BY d) AS equity_curve
FROM pnl
ORDER BY d;

-- 2) Дроудаун по эквити
WITH t AS (
    SELECT *
    FROM trades
    WHERE config_version = '2026-03-05_15m_conservative_v3'
      AND exit_ts IS NOT NULL
),
pnl AS (
    SELECT
        exit_ts::date AS d,
        SUM(pnl_usd)  AS day_pnl
    FROM t
    GROUP BY exit_ts::date
    ORDER BY d
),
equity AS (
    SELECT
        d,
        SUM(day_pnl) OVER (ORDER BY d) AS equity
    FROM pnl
)
SELECT
    d,
    equity,
    MAX(equity) OVER (ORDER BY d)             AS running_peak,
    equity - MAX(equity) OVER (ORDER BY d)    AS drawdown
FROM equity
ORDER BY d;
