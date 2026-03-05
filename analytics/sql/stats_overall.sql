-- Винрейт и средний плюс/минус по config_version.
-- Заменить '2026-03-05_15m_conservative_v3' на свою версию.
WITH t AS (
    SELECT *
    FROM trades
    WHERE config_version = '2026-03-05_15m_conservative_v3'
      AND exit_ts IS NOT NULL
)
SELECT
    COUNT(*)                                           AS trades_total,
    SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)       AS wins,
    SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END)     AS losses,
    AVG(CASE WHEN pnl_usd > 0 THEN pnl_usd END)       AS avg_win_usd,
    AVG(CASE WHEN pnl_usd <= 0 THEN pnl_usd END)      AS avg_loss_usd,
    AVG(pnl_usd)                                      AS avg_trade_usd
FROM t;
