-- Добавить колонки уровней SL/TP на момент закрытия (как пишет trade_logger).
-- Выполнить один раз, если в trades нет sl_at_exit / tp_at_exit.

ALTER TABLE trades ADD COLUMN IF NOT EXISTS sl_at_exit NUMERIC;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS tp_at_exit NUMERIC;
