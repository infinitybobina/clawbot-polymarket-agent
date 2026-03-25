#!/usr/bin/env python3
"""
Мини-API логирования сделок в таблицу trades (Postgres).
Разделяет слой «как торгуем» и «как считаем/анализируем».

Использование:
  - При открытии: trade_id = log_trade_open(position_state, llm_decision, market_diag)
  - При закрытии: log_trade_close(trade_id, close_state, pnl_stats)

Если DATABASE_URL не задан или psycopg2 недоступен — функции не падают, возвращают None/False.
"""

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_conn = None


def _get_config_version() -> str:
    """
    config_version must NEVER be NULL/None.
    Otherwise analytics queries filtered by config_version become blind.
    """
    cv = os.environ.get("CONFIG_VERSION")
    if cv:
        cv = cv.strip()
        if cv:
            return cv
    ch = os.environ.get("CONFIG_HASH")
    if ch:
        ch = ch.strip()
        if ch:
            return ch
    logger.warning("trade_logger: CONFIG_VERSION/CONFIG_HASH not set — using MISSING_CONFIG_VERSION")
    return "MISSING_CONFIG_VERSION"


def _get_connection():
    """Ленивое подключение. Возвращает None, если DB не настроена."""
    global _conn
    if _conn is not None:
        return _conn
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        _conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
        return _conn
    except ImportError:
        logger.debug("trade_logger: psycopg2 not installed, trades DB disabled")
        return None
    except Exception as e:
        logger.warning("trade_logger: DB connect failed: %s", e)
        return None


def _n(val: Any):
    """Привести к NUMERIC-friendly (Decimal или None)."""
    if val is None:
        return None
    try:
        return Decimal(str(val))
    except Exception:
        return None


def log_trade_open(
    position_state: Dict[str, Any],
    llm_decision: Dict[str, Any],
    market_diag: Dict[str, Any],
    *,
    strategy_id: Optional[str] = None,
    entry_ts: Optional[datetime] = None,
) -> Optional[int]:
    """
    В момент открытия позиции: INSERT в trades, все поля до exit_* и PnL.
    Возвращает trade_id или None при ошибке/отключённом DB.

    position_state: market_id, size_tokens, avg_price, outcome, yes_token_id, stop_loss_price, take_profit_price
    llm_decision: limit_price, final_size_usd, expected_ev, (optional: raw JSON)
    market_diag: condition_id, category, yes_bid, yes_ask, spread, book_ok_at_entry, hit_volume_cap
    """
    conn = _get_connection()
    if not conn:
        return None
    mid = position_state.get("market_id")
    if not mid:
        return None
    entry_ts = entry_ts or datetime.now(timezone.utc)
    side = (position_state.get("outcome") or "YES").upper()
    size_tokens = _n(position_state.get("size_tokens"))
    size_usd = _n(position_state.get("final_size_usd") or llm_decision.get("final_size_usd") or (float(position_state.get("size_tokens") or 0) * float(position_state.get("avg_price") or 0)))
    entry_price = _n(llm_decision.get("limit_price") or position_state.get("avg_price"))
    if entry_price is None:
        entry_price = _n(position_state.get("avg_price"))
    if entry_price is None:
        entry_price = Decimal("0")
    avg_entry_price = _n(position_state.get("avg_price")) or entry_price
    yes_token_id = position_state.get("yes_token_id") or market_diag.get("yes_token_id")
    condition_id = market_diag.get("condition_id") or mid
    category = market_diag.get("category")
    yes_bid = _n(market_diag.get("yes_bid"))
    yes_ask = _n(market_diag.get("yes_ask"))
    # mid и spread только из стакана по торгуемому (YES) токену; никогда не брать market_diag["spread"] (yes_p - no_p)
    spread_at_entry = None
    mid_at_entry = None
    if yes_bid is not None and yes_ask is not None:
        mid_at_entry = (yes_bid + yes_ask) / 2
        spread_at_entry = yes_ask - yes_bid
        # Не записывать фиктивный mid 0.5, когда контракт торгуется далеко от 0.5
        if mid_at_entry is not None and entry_price is not None:
            try:
                ep = float(entry_price)
                if abs(mid_at_entry - 0.5) < 0.05 and abs(ep - 0.5) > 0.2:
                    mid_at_entry = None
                    spread_at_entry = None
            except (TypeError, ValueError):
                pass
    if spread_at_entry is None and (yes_bid is None or yes_ask is None):
        spread_at_entry = _n(market_diag.get("spread"))
    book_ok_at_entry = market_diag.get("book_ok_at_entry")
    if book_ok_at_entry is None:
        book_ok_at_entry = yes_bid is not None or yes_ask is not None
    hit_volume_cap = llm_decision.get("hit_volume_cap") or market_diag.get("hit_volume_cap")
    llm_score = _n(llm_decision.get("expected_ev") or llm_decision.get("llm_score"))
    llm_raw_json = llm_decision.get("llm_raw_json")
    if llm_raw_json is not None and not isinstance(llm_raw_json, str):
        try:
            import json
            llm_raw_json = json.dumps(llm_raw_json) if isinstance(llm_raw_json, dict) else str(llm_raw_json)
        except Exception:
            llm_raw_json = None
    sl_price = _n(position_state.get("stop_loss_price") or llm_decision.get("stop_loss_price"))
    tp_price = _n(position_state.get("take_profit_price") or llm_decision.get("take_profit_price"))

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO trades (
                    market_id, condition_id, yes_token_id, category,
                    entry_ts, side, size_tokens, size_usd,
                    entry_price, avg_entry_price,
                    book_ok_at_entry, hit_volume_cap, spread_at_entry, mid_at_entry,
                    llm_score, llm_raw_json, strategy_id, config_version, sl_price, tp_price
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING trade_id
                """,
                (
                    mid, condition_id, yes_token_id, category,
                    entry_ts, side, size_tokens, size_usd,
                    entry_price, avg_entry_price,
                    book_ok_at_entry, hit_volume_cap, spread_at_entry, mid_at_entry,
                    llm_score,
                    llm_raw_json,
                    strategy_id or os.environ.get("STRATEGY_ID", "LLM_CRYPTO_15M"),
                    _get_config_version(),
                    sl_price, tp_price,
                ),
            )
            row = cur.fetchone()
            conn.commit()
            tid = row["trade_id"] if row else None
            if tid:
                logger.info("trade_logger: INSERT trade_id=%s market_id=%s", tid, mid[:16])
            return int(tid) if tid is not None else None
    except Exception as e:
        logger.warning("trade_logger: INSERT failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def log_trade_close(
    trade_id: int,
    close_state: Dict[str, Any],
    pnl_stats: Dict[str, Any],
) -> bool:
    """
    В момент закрытия: UPDATE trades по trade_id.
    close_state: exit_ts, exit_price, avg_exit_price, exit_reason
    pnl_stats: pnl_usd, pnl_pct, max_dd_pct, mae_pct, mfe_pct (опционально)
    """
    if trade_id is None:
        return False
    conn = _get_connection()
    if not conn:
        return False
    exit_ts = close_state.get("exit_ts")
    if exit_ts is None:
        exit_ts = datetime.now(timezone.utc)
    exit_price = _n(close_state.get("exit_price") or close_state.get("sell_price"))
    avg_exit_price = _n(close_state.get("avg_exit_price")) or exit_price
    exit_reason = close_state.get("exit_reason") or close_state.get("reason") or "exit"
    sl_at_exit = _n(close_state.get("sl_at_exit"))
    tp_at_exit = _n(close_state.get("tp_at_exit"))
    pnl_usd = _n(pnl_stats.get("pnl_usd"))
    pnl_pct = _n(pnl_stats.get("pnl_pct"))
    if pnl_pct is None and pnl_usd is not None:
        # можно вычислить по size_usd из строки, но проще передать снаружи
        pass
    max_dd_pct = _n(pnl_stats.get("max_dd_pct"))
    mae_pct = _n(pnl_stats.get("mae_pct"))
    mfe_pct = _n(pnl_stats.get("mfe_pct"))

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE trades
                SET exit_ts = %s, exit_price = %s, avg_exit_price = %s,
                    pnl_usd = %s, pnl_pct = %s, max_dd_pct = %s, mae_pct = %s, mfe_pct = %s,
                    exit_reason = %s, sl_at_exit = %s, tp_at_exit = %s, updated_at = now()
                WHERE trade_id = %s
                """,
                (exit_ts, exit_price, avg_exit_price, pnl_usd, pnl_pct, max_dd_pct, mae_pct, mfe_pct, exit_reason, sl_at_exit, tp_at_exit, trade_id),
            )
            conn.commit()
            if cur.rowcount:
                logger.info("trade_logger: UPDATE trade_id=%s exit_reason=%s pnl_usd=%s", trade_id, exit_reason, pnl_usd)
            return cur.rowcount > 0
    except Exception as e:
        logger.warning("trade_logger: UPDATE failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return False
