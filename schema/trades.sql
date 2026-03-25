-- Таблица сделок для анализа и отчётов. Postgres; при необходимости адаптировать под другой SQL.
CREATE TABLE IF NOT EXISTS trades (
    trade_id           BIGSERIAL PRIMARY KEY,

    -- Идентификация
    market_id          TEXT        NOT NULL,
    condition_id       TEXT,
    yes_token_id       TEXT,
    category           TEXT,

    -- Время
    entry_ts           TIMESTAMPTZ NOT NULL,
    exit_ts            TIMESTAMPTZ,

    -- Направление и размер
    side               TEXT        NOT NULL,
    size_tokens        NUMERIC,
    size_usd           NUMERIC,

    -- Цены
    entry_price        NUMERIC NOT NULL,
    exit_price         NUMERIC,
    avg_entry_price    NUMERIC,
    avg_exit_price     NUMERIC,

    -- PnL и риск
    pnl_usd            NUMERIC,
    pnl_pct            NUMERIC,
    max_dd_pct         NUMERIC,
    mae_pct            NUMERIC,
    mfe_pct            NUMERIC,

    -- Книга и ликвидность
    book_ok_at_entry   BOOLEAN,
    hit_volume_cap     BOOLEAN,
    spread_at_entry    NUMERIC,
    mid_at_entry       NUMERIC,

    -- LLM и стратегия
    llm_score          NUMERIC,
    llm_raw_json       JSONB,
    strategy_id        TEXT,
    config_version     TEXT,
    pattern_id         TEXT,

    -- Выход
    exit_reason        TEXT,
    sl_price           NUMERIC,
    tp_price           NUMERIC,
    sl_at_exit         NUMERIC,
    tp_at_exit         NUMERIC,

    created_at         TIMESTAMPTZ DEFAULT now(),
    updated_at         TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trades_market_id ON trades (market_id);
CREATE INDEX IF NOT EXISTS idx_trades_entry_ts ON trades (entry_ts);
CREATE INDEX IF NOT EXISTS idx_trades_exit_ts ON trades (exit_ts) WHERE exit_ts IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_trades_category ON trades (category);
CREATE INDEX IF NOT EXISTS idx_trades_config_version ON trades (config_version);

-- Миграция: если таблица уже создана без sl_at_exit/tp_at_exit, выполнить:
-- ALTER TABLE trades ADD COLUMN IF NOT EXISTS sl_at_exit NUMERIC;
-- ALTER TABLE trades ADD COLUMN IF NOT EXISTS tp_at_exit NUMERIC;
