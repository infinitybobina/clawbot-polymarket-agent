#!/usr/bin/env python3
"""
ClawBot v2.0 — цикл 60 сек, цены из Price Stream (REST/WebSocket), LLM раз в 5 мин, Telegram раз в час.
Запуск: python main_v2.py
"""

import asyncio
import atexit
import logging
import os
import re
import signal
import shutil
import sys
import time
from datetime import datetime, timezone

_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _root)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_root, ".env"))
except ImportError:
    pass

from config import (
    PROD_CONFIG,
    LOOP_INTERVAL_SEC,
    LLM_INTERVAL_SEC,
    TELEGRAM_INTERVAL_SEC,
    PRICE_STREAM_INTERVAL_SEC,
    MARKET_CATEGORIES,
    ACTIVE_CATEGORIES,
)
from datafeed import ClawBotDataFeed, has_live_orderbook_for_market, get_yes_token_id, is_liquid, is_liquid_diagnostic
from strategy import ClawBotStrategy
from riskmanager import RiskManager
from price_stream import PriceStream
from portfolio_state import load_state, save_state
from bot_runtime import record_session_start
from paper_trader import PaperTrader
from position_prices import get_position_prices_by_market
from trade_logger import log_trade_open, log_trade_close
from sl_cooldown import (
    load_cooldown,
    save_cooldown,
    tick_cooldown,
    add_to_cooldown,
    get_cooldown_set,
    load_tp_cooldown,
    save_tp_cooldown,
    DEFAULT_RUNS,
    DEFAULT_TP_RUNS,
)
from reentry_cooldown import (
    load_reentry_cooldown,
    save_reentry_cooldown,
    tick_reentry_cooldown,
    add_to_reentry_cooldown,
    get_reentry_cooldown_set,
)
from experiment_logger import (
    ExperimentLogger,
    avg_spread_from_candidates,
    median_tte_sec_from_candidates,
    median_clob_vol_from_candidates,
)
from copy_trading import build_copy_signals

try:
    from telegram_notify import send_telegram_message
except ImportError:
    send_telegram_message = None

# Маппинг категории из Polymarket API → внутренний идентификатор (риск, Telegram, спорт/культура позже)
API_CATEGORY_TO_INTERNAL = {
    "crypto": "crypto",
    "politics": "politics",
    "sports": "sports",
    "culture": "culture",
    "economy": "economy",
}


def get_trader(cfg: dict):
    """Фабрика трейдера: paper — симуляция, live — реальные ордера (см. PRODUCTION_READY.md)."""
    mode = (cfg.get("trading_mode") or "paper").strip().lower()
    if mode == "live":
        from live_trader import LiveTrader
        return LiveTrader(cfg)
    return PaperTrader(cfg)


def _market_category(market_id: str, datafeed: ClawBotDataFeed) -> str:
    """Категория только из API Polymarket (поле category в снимке). Без эвристик по тексту."""
    if not market_id or market_id not in datafeed.markets:
        return "politics"  # fallback для старых позиций без снимка
    api_cat = (datafeed.markets[market_id].category or "").strip().lower()
    return API_CATEGORY_TO_INTERNAL.get(api_cat, api_cat or "politics")


def _market_display_title(market_id: str, datafeed: ClawBotDataFeed) -> str:
    """Краткое название рынка для логов/Telegram (group_item_title или question)."""
    if not market_id or market_id not in datafeed.markets:
        return ""
    snap = datafeed.markets[market_id]
    short = (getattr(snap, "group_item_title", "") or "").strip()
    long_q = (getattr(snap, "question", "") or "").strip()
    t = (short or long_q)[:120]
    return t


def _order_has_book(order: dict, datafeed: ClawBotDataFeed) -> bool:
    """Есть ли книга (yes_bid/yes_ask) для входа — не открываем позицию без стакана."""
    mid = order.get("market_id")
    if not mid or mid not in datafeed.markets:
        return False
    m = datafeed.markets[mid]
    return getattr(m, "yes_bid", None) is not None and getattr(m, "yes_ask", None) is not None


def _signal_is_copy(s: dict) -> bool:
    return str(s.get("source") or "").strip().lower() == "copy"


def _mid_short(mid: str, n: int = 16) -> str:
    if not mid:
        return ""
    return (mid[:n] + "..") if len(mid) > n else mid


def _copy_trace_drop(stage: str, mid: str, detail: str = "") -> None:
    """Диагностика: почему copy-сигнал не дошёл до исполнения (см. лог Copy-tracing)."""
    extra = f" | {detail}" if detail else ""
    logger.info("Copy-tracing: %s market=%s%s", stage, _mid_short(mid), extra)


_log_file = os.path.join(_root, "clawbot_v2_run.log")
# Ротация: если лог разросся, переименовать перед открытием (удобно для редактора/диска).
# Порог: CLAWBOT_LOG_MAX_BYTES (по умолчанию 50 МБ). Пример: CLAWBOT_LOG_MAX_BYTES=10485760
_log_max_bytes = int(os.environ.get("CLAWBOT_LOG_MAX_BYTES", str(50 * 1024 * 1024)))
if _log_max_bytes > 0 and os.path.isfile(_log_file):
    try:
        if os.path.getsize(_log_file) > _log_max_bytes:
            _bak = (
                _log_file
                + "."
                + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                + ".bak"
            )
            shutil.move(_log_file, _bak)
            print(f"Log rotated (>{_log_max_bytes} bytes): {_bak}", file=sys.stderr)
    except OSError as e:
        print(f"Log rotation skipped: {e}", file=sys.stderr)
# Файл с построчной записью (buffering=1), чтобы изменения сразу видны в редакторе
_log_file_stream = open(_log_file, mode="a", encoding="utf-8", buffering=1)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.StreamHandler(_log_file_stream),
    ],
)
logger = logging.getLogger(__name__)
logger.info("Log file: %s", _log_file)

# Одноразовая диагностика: разрешить 1 LLM-слот даже в tail-режиме,
# чтобы получить edge-логи и понять, что генерит LLM.
_tail_guard_force_llm_used = False


# --- Market-making strategy ---
def select_mm_markets(snapshots: list, cfg: dict) -> list:
    """Выбрать ликвидные бинарные рынки под маркет-мейкинг (только реальные CLOB bid/ask) + TTE >= min_seconds_to_resolution."""
    params = cfg.get("mm_params") or {}
    if not params:
        return []
    min_tte = float(params.get("min_seconds_to_resolution", 1800))
    liquid = []
    for s in snapshots:
        if getattr(s, "outcomes_count", 2) != 2:
            continue
        tte = getattr(s, "seconds_to_resolution", None)
        try:
            if tte is None or float(tte) < min_tte:
                continue
        except (TypeError, ValueError):
            continue
        if not is_liquid(s, params):
            continue
        liquid.append(s)
    return liquid


def build_mm_orders(
    positions: dict,
    snapshots: list,
    cfg: dict,
    pending_market_ids: set,
    balance: float,
) -> list:
    """Пассивные лимитки на покупку YES: одна сторона, buy_price внутри спреда, объём из risk_per_trade_pct."""
    params = cfg.get("mm_params") or {}
    if not params:
        return []
    epsilon = float(params.get("epsilon_price", 0.005))
    delta = float(params.get("delta_price", 0.005))
    max_offset = float(params.get("max_offset_from_mid", 0.02))
    sl_ticks = float(params.get("sl_ticks", 0.02))
    risk_pct = float(params.get("risk_per_trade_pct", 0.005))
    max_market_pct = float(params.get("max_position_per_market_pct", 0.015))
    max_open = int(params.get("max_open_markets", 3))
    if balance <= 0:
        return []
    slots = max(0, max_open - len(positions) - len(pending_market_ids))
    if slots <= 0:
        return []
    risk_usd = balance * risk_pct
    max_size_usd = balance * max_market_pct
    out = []
    for s in snapshots:
        if len(out) >= slots:
            break
        mid = getattr(s, "market_id", None)
        if not mid or mid in positions or mid in pending_market_ids:
            continue
        bid = getattr(s, "yes_bid", None)
        ask = getattr(s, "yes_ask", None)
        if bid is None or ask is None:
            continue
        try:
            bid_f, ask_f = float(bid), float(ask)
        except (TypeError, ValueError):
            continue
        if ask_f <= bid_f:
            continue
        mid_price = (bid_f + ask_f) * 0.5
        # buy_price = min(bid + epsilon, mid - delta); не уходить от mid дальше max_offset
        buy_price = min(bid_f + epsilon, mid_price - delta)
        buy_price = max(buy_price, mid_price - max_offset)
        buy_price = round(min(0.99, max(0.01, buy_price)), 2)
        if buy_price >= ask_f:
            continue
        # размер: при проигрыше sl_ticks убыток = risk_usd
        size_by_risk = risk_usd * buy_price / sl_ticks if sl_ticks > 0 else 0
        size_usd = min(size_by_risk, max_size_usd)
        size_usd = round(max(0, size_usd), 2)
        if size_usd < 1.0:
            continue
        sl_price = round(max(0.01, buy_price - sl_ticks), 4)
        tp_price = round(min(0.99, buy_price + float(params.get("tp_ticks", 0.015))), 4)
        yes_token_id = get_yes_token_id(s)
        out.append({
            "market_id": mid,
            "outcome": "YES",
            "side": "buy",
            "limit_price": buy_price,
            "final_size_usd": size_usd,
            "target_size_usd": size_usd,
            "yes_token_id": yes_token_id,
            "stop_loss_price": sl_price,
            "take_profit_price": tp_price,
            "mm": True,
        })
    return out


def check_stops(trader, prices: dict, cfg: dict) -> list:
    """По текущим ценам: вернуть список {market_id, sell_price, reason} для закрытия.
    Цены по позициям должны быть ликвидируемые (best_bid для YES). Нет цены (нет bids) → позицию не трогаем.
    """
    to_close = []
    sl_pct = cfg.get("sl_pct", 0.04)
    tp_pct = cfg.get("tp_pct", 0.18)
    # Тайм-стоп: если до резолва осталось меньше N минут и позиция не в профите — закрываем заранее
    time_stop_min = float(cfg.get("time_stop_minutes", 15))
    time_stop_sec = max(0.0, time_stop_min * 60.0)
    # Тайм-стоп для долго висящих минусов: закрывать, если позиция убыточна и ей >= N часов.
    loser_stop_hours = float(cfg.get("loser_time_stop_hours", 0) or 0)
    loser_stop_sec = max(0.0, loser_stop_hours * 3600.0)
    loser_min_dd = float(cfg.get("loser_time_stop_min_drawdown", 0) or 0)
    # Hard max-hold: закрывать позицию по времени независимо от PnL.
    hard_max_hold_hours = float(cfg.get("hard_max_hold_hours", 0) or 0)
    hard_max_hold_sec = max(0.0, hard_max_hold_hours * 3600.0)
    # EXPIRY is only valid when we're truly at resolution time.
    # Seeing best_bid<=0.02 with unknown/large TTE is almost always "no reliable price" or token mismatch,
    # and must NOT trigger a forced close.
    expiry_tte_sec = float(cfg.get("expiry_tte_seconds", 120))
    # Trigger SL slightly before best_bid crosses sl_at_exit to reduce stop overshoot.
    sl_trigger_buffer = float(cfg.get("sl_trigger_buffer", 0.0))
    skipped_no_price = False
    for mid, pos in list(trader.positions.items()):
        avg_price = float(pos.get("avg_price", 0))
        if avg_price <= 0:
            continue
        info = prices.get(mid, {})
        current_price = info.get("yes_price")
        if current_price is None:
            skipped_no_price = True
            continue
        current_price = float(current_price)
        if current_price <= 0:
            continue
        # Тайм-стоп «долго в минусе»: только если знаем opened_ts и есть ликвидируемая цена.
        opened_ts = pos.get("opened_ts")
        if hard_max_hold_sec > 0 and opened_ts is not None:
            try:
                age_sec_hard = time.time() - float(opened_ts)
            except (TypeError, ValueError):
                age_sec_hard = None
            if age_sec_hard is not None and age_sec_hard >= hard_max_hold_sec:
                to_close.append(
                    {
                        "market_id": mid,
                        "sell_price": current_price,
                        "reason": "TIME_STOP",
                        "trade_id": pos.get("trade_id"),
                    }
                )
                continue

        if loser_stop_sec > 0:
            if opened_ts is not None:
                try:
                    age_sec = time.time() - float(opened_ts)
                except (TypeError, ValueError):
                    age_sec = None
                if age_sec is not None and age_sec >= loser_stop_sec:
                    # Считаем «в минусе», если цена ниже entry хотя бы на loser_min_dd.
                    if current_price < (avg_price * (1.0 - loser_min_dd)):
                        to_close.append(
                            {
                                "market_id": mid,
                                "sell_price": current_price,
                                "reason": "TIME_STOP",
                                "trade_id": pos.get("trade_id"),
                            }
                        )
                        continue
        # Тайм-стоп (только для YES/long): ближе к резолву не держим убыточные позиции
        tte = info.get("seconds_to_resolution")
        try:
            tte_f = float(tte) if tte is not None else None
        except (TypeError, ValueError):
            tte_f = None
        outcome = (pos.get("outcome") or "YES").strip().upper()
        if outcome == "YES" and tte_f is not None and tte_f <= time_stop_sec:
            if current_price < avg_price:
                to_close.append({"market_id": mid, "sell_price": current_price, "reason": "TIME_STOP", "trade_id": pos.get("trade_id")})
                continue
        # MM: тайм-стоп позиции (кэш-аут через N секунд)
        if pos.get("mm"):
            mm_params = cfg.get("mm_params") or {}
            pos_time_stop = float(mm_params.get("position_time_stop_seconds", 180))
            opened_ts = pos.get("opened_ts")
            if opened_ts is not None and (time.time() - float(opened_ts)) >= pos_time_stop:
                to_close.append({"market_id": mid, "sell_price": current_price, "reason": "TIME_STOP", "trade_id": pos.get("trade_id")})
                continue
        sl = pos.get("stop_loss_price")
        tp = pos.get("take_profit_price")
        # Не доверять подозрительным значениям (0.01/0.99 — часто clamp по умолчанию; реальный SL для entry ~0.6 — ~0.57–0.59)
        if sl is not None:
            sl_f = float(sl)
            if sl_f < 0.02 or sl_f >= avg_price - 1e-6:
                sl = None
        if tp is not None:
            tp_f = float(tp)
            if tp_f > 0.98 or tp_f <= avg_price + 1e-6:
                tp = None
        if sl is None or tp is None:
            sl = max(0.01, avg_price * (1 - sl_pct))
            tp = min(0.99, avg_price * (1 + tp_pct))
        else:
            sl, tp = float(sl), float(tp)
        if current_price <= (sl + sl_trigger_buffer):
            # best_bid<=0.02 is NOT automatically expiry. Treat as expiry only if TTE is truly near zero.
            if current_price <= 0.02:
                if tte_f is not None and tte_f <= expiry_tte_sec:
                    logger.warning(
                        "Stops: closing as EXPIRY (best_bid=%.2f <= 0.02 and tte<=%.0fs). real SL was %.2f",
                        current_price, expiry_tte_sec, sl,
                    )
                    to_close.append({"market_id": mid, "sell_price": current_price, "reason": "EXPIRY", "trade_id": pos.get("trade_id")})
                else:
                    skipped_no_price = True
                    logger.warning(
                        "Stops: best_bid=%.2f (<=0.02) but tte=%s — treat as NO_PRICE, skip (avoid false EXPIRY). mid=%s.. sl=%.2f",
                        current_price, tte_f, mid[:16], sl,
                    )
                continue
            to_close.append({"market_id": mid, "sell_price": current_price, "reason": "SL", "trade_id": pos.get("trade_id")})
        elif current_price >= tp:
            to_close.append({"market_id": mid, "sell_price": current_price, "reason": "TP", "trade_id": pos.get("trade_id")})
    if skipped_no_price:
        logger.info("No price (no orderbook) -> skip SL/TP this tick")
    return to_close


async def main_loop() -> None:
    cfg = PROD_CONFIG
    level_name = cfg.get("log_level", "INFO").upper()
    logging.getLogger().setLevel(getattr(logging, level_name, logging.INFO))
    mode = (cfg.get("trading_mode") or "paper").strip().lower()
    if mode not in ("paper", "live"):
        raise ValueError(f"Unknown trading_mode={mode!r}")
    if mode == "live":
        raise RuntimeError(
            "LIVE trading mode is not implemented and is disabled for safety. "
            "Use trading_mode='paper' for now."
        )
    logger.info("ClawBot v2.0: PAPER MODE ONLY (no real orders will be sent).")
    logger.info("Config mm_params: %s", "present" if cfg.get("mm_params") else "MISSING (MM блок не будет выполняться)")
    base_runs = int(cfg.get("sl_cooldown_runs", DEFAULT_RUNS))
    pol_runs = int(cfg.get("sl_cooldown_runs_politics", base_runs * 4))
    geo_runs = int(cfg.get("sl_cooldown_runs_geopolitics", base_runs * 4))
    logger.info(
        "Cooldown config: SL/TIME_STOP/EXPIRY base=%dr politics=%dr geopolitics=%dr tp=%dr reentry=%smin",
        base_runs,
        pol_runs,
        geo_runs,
        int(cfg.get("tp_cooldown_runs", DEFAULT_TP_RUNS)),
        cfg.get("reentry_cooldown_minutes", 60),
    )

    # --- Shutdown diagnostics (Windows-friendly): helps explain "bot stopped while unattended" ---
    shutdown_diag: dict = {"signal": None, "ts_utc": None}

    def _install_shutdown_diagnostics() -> None:
        def _on_signal(signum: int, _frame=None) -> None:
            name = None
            try:
                name = signal.Signals(signum).name
            except Exception:
                name = str(signum)
            ts = datetime.now(timezone.utc).isoformat()
            shutdown_diag["signal"] = name
            shutdown_diag["ts_utc"] = ts
            logger.warning("SHUTDOWN signal received: %s at %s", name, ts)

        # SIGINT: Ctrl+C / console events; SIGTERM: Taskkill without /F; SIGBREAK: Ctrl+Break on Windows.
        for s in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, s, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, _on_signal)
            except Exception as e:
                logger.debug("Shutdown diag: cannot register %s: %s", s, e)

        def _on_exit() -> None:
            # Note: atexit is not called on hard kills (/F) or power loss.
            logger.warning(
                "SHUTDOWN atexit: last_signal=%s last_signal_ts_utc=%s",
                shutdown_diag.get("signal"),
                shutdown_diag.get("ts_utc"),
            )

        try:
            atexit.register(_on_exit)
        except Exception as e:
            logger.debug("Shutdown diag: cannot register atexit: %s", e)

    _install_shutdown_diagnostics()

    class _RedactTelegramTokenFilter(logging.Filter):
        """Не писать bot token в лог (httpx логирует полный URL)."""

        def filter(self, record: logging.LogRecord) -> bool:
            try:
                msg = record.getMessage()
            except Exception:
                return True
            if "api.telegram.org/bot" not in msg:
                return True
            record.msg = re.sub(
                r"https://api\.telegram\.org/bot[^/\s]+",
                "https://api.telegram.org/bot***",
                msg,
            )
            record.args = ()
            return True

    for _httpx_name in ("httpx", "httpcore"):
        logging.getLogger(_httpx_name).addFilter(_RedactTelegramTokenFilter())

    sess = record_session_start(_root)
    logger.info(
        "Session start: started_at_utc=%s pid=%s (bot_session.json — для скриптов и срезов «после рестарта»)",
        sess.get("started_at_utc"),
        sess.get("pid"),
    )

    saved_balance, saved_positions, cumulative_realized_pnl = load_state(_root)
    trader = get_trader(cfg)
    if saved_balance is not None and saved_positions is not None:
        trader.balance = saved_balance
        trader.positions = saved_positions
        logger.info("Restored portfolio: balance=%.2f, positions=%d", trader.balance, len(trader.positions))
    _cap = int(cfg.get("max_open_positions") or 0)
    if _cap > 0 and len(trader.positions) > _cap:
        logger.warning(
            "Open positions %d > max_open_positions=%d — новые входы заблокированы риском, пока не станет <%d рынков",
            len(trader.positions),
            _cap,
            _cap,
        )
    sl_cooldown = load_cooldown(_root)
    tp_cooldown = load_tp_cooldown(_root)
    reentry_cooldown = load_reentry_cooldown(_root)
    no_price_streak = {}  # mid -> consecutive ticks without price (для агрегата раз в минуту)
    last_no_price_agg_time = 0.0
    pending_mm_orders = {}  # market_id -> {limit_price, size_usd, side, yes_token_id, placed_ts, ...} для MM

    stream = PriceStream()
    loop_sec = LOOP_INTERVAL_SEC
    llm_sec = LLM_INTERVAL_SEC
    telegram_sec = TELEGRAM_INTERVAL_SEC
    price_interval = PRICE_STREAM_INTERVAL_SEC
    elapsed = 0
    tick_count = 0
    PORTFOLIO_SUMMARY_EVERY_TICKS = 5  # сводка портфеля в лог раз в N тиков

    profile_15m = cfg.get("strategy_profile_15m", "15m_conservative")
    exp_logger = ExperimentLogger(profile_15m, _root)
    logger.info("Experiment profile: %s", profile_15m)

    async with ClawBotDataFeed(
        gamma_timeout_sec=float(cfg.get("gamma_http_timeout_sec", 60.0)),
        gamma_retries=int(cfg.get("gamma_http_retries", 5)),
    ) as datafeed:
        category = cfg.get("markets_category", "both")
        active = [c for c in ACTIVE_CATEGORIES if c in MARKET_CATEGORIES]

        async def fetch_all_categories():
            all_markets = []
            per_cat = {}
            for cat in active:
                if cat == "crypto":
                    markets_c = await datafeed.fetch_crypto_markets(
                        min_volume_usd=cfg.get("crypto_min_volume", 50_000),
                        min_liquidity_usd=cfg.get("crypto_min_liquidity", 0),
                        max_hours_to_resolution=cfg.get("crypto_resolution_hours_max", 12),
                        skip_rebuild_and_enrich=True,
                    )
                    per_cat[cat] = len(markets_c)
                    all_markets.extend(markets_c)
                else:
                    params = MARKET_CATEGORIES[cat]
                    min_vol = params.get("min_volume_usd", 100_000)
                    markets_cat = await datafeed.fetch_category_markets(
                        params.get("gamma_category", cat),
                        min_volume_usd=min_vol,
                        skip_rebuild_and_enrich=True,
                    )
                    per_cat[cat] = len(markets_cat)
                    all_markets.extend(markets_cat)
            logger.info("Fetch per category: %s total=%d", " ".join(f"{k}={v}" for k, v in sorted(per_cat.items())), len(all_markets))
            if not all_markets:
                return datafeed.markets
            # Дешёвый prefilter по Gamma bestBid/bestAsk (если поля есть): выбросить явный "tail"
            # до CLOB-enrich/LLM, чтобы вообще получить живые книги.
            gbb_min = float(cfg.get("gamma_best_bid_min", 0.05))
            gba_max = float(cfg.get("gamma_best_ask_max", 0.95))
            before_g = len(all_markets)
            g_pass = []
            g_missing = 0
            g_tail = 0
            for m in all_markets:
                if m is None:
                    continue
                gbb = getattr(m, "gamma_best_bid", None)
                gba = getattr(m, "gamma_best_ask", None)
                if gbb is None or gba is None:
                    g_missing += 1
                    continue
                try:
                    if float(gbb) >= gbb_min and float(gba) <= gba_max:
                        g_pass.append(m)
                    else:
                        g_tail += 1
                except (TypeError, ValueError):
                    g_missing += 1
            if g_pass:
                all_markets = g_pass
                logger.info(
                    "Gamma bestBid/bestAsk prefilter: kept=%d dropped_tail=%d missing/invalid=%d (threshold bid>=%.2f ask<=%.2f)",
                    len(all_markets), g_tail, g_missing, gbb_min, gba_max,
                )
            else:
                logger.warning(
                    "Gamma bestBid/bestAsk prefilter: 0 kept (dropped_tail=%d missing/invalid=%d). Proceed without it.",
                    g_tail, g_missing,
                )
            # Только «живые» по цене: решённые (0.01/0.99) дают односторонний стакан → 0 кандидатов после dead-book фильтра
            enrich_yes_min = float(cfg.get("enrich_yes_price_min", 0.10))
            enrich_yes_max = float(cfg.get("enrich_yes_price_max", 0.90))
            tradeable_price = [m for m in all_markets if m is not None and (enrich_yes_min <= (getattr(m, "yes_price", None) or 0) <= enrich_yes_max)]
            if tradeable_price:
                if len(tradeable_price) < len(all_markets):
                    logger.info("Enrich pool: %d markets with yes_price in [%.2f, %.2f] (dropped %d resolved/extreme)",
                                len(tradeable_price), enrich_yes_min, enrich_yes_max, len(all_markets) - len(tradeable_price))
                all_markets = tradeable_price
            else:
                logger.warning("No markets with yes_price in [%.2f, %.2f] — using full pool (risk: many dead books)", enrich_yes_min, enrich_yes_max)
            # Ограничить пул до max_markets_to_enrich: при 1000+ CLOB enrich ~10 мин (2 QPS)
            cap = cfg.get("max_markets_to_enrich", 180)
            total_before_cap = len(all_markets)
            if total_before_cap > cap:
                all_markets = sorted(all_markets, key=lambda m: getattr(m, "volume_usd", 0) or 0, reverse=True)[:cap]
                logger.info("Capped to top %d markets by volume for CLOB enrich (was %d)", cap, total_before_cap)
            datafeed.markets.clear()
            datafeed.markets.update({m.market_id: m for m in all_markets})
            datafeed.clob_state.rebuild_from_snapshots(list(datafeed.markets.values()))
            enriched = await datafeed.enrich_snapshots_with_clob(list(datafeed.markets.values()))
            datafeed.markets.update({m.market_id: m for m in enriched})

            # AUTO AUDIT: Gamma bestBid/bestAsk vs CLOB yes_bid/yes_ask consistency.
            # Goal: determine if "no live markets" is market reality or token/outcome mapping issue.
            if bool(cfg.get("enable_gamma_clob_audit", True)):
                audit_gbb_min = float(cfg.get("gamma_best_bid_min", 0.05))
                audit_gba_max = float(cfg.get("gamma_best_ask_max", 0.95))
                live_bid_min = float(cfg.get("entry_bid_min", 0.05))
                live_ask_max = float(cfg.get("entry_ask_max", 0.95))
                live_spread_max = float(cfg.get("entry_spread_max", 0.25))

                gamma_live = []
                gamma_missing = 0
                for m in enriched:
                    gbb = getattr(m, "gamma_best_bid", None)
                    gba = getattr(m, "gamma_best_ask", None)
                    if gbb is None or gba is None:
                        gamma_missing += 1
                        continue
                    try:
                        if float(gbb) >= audit_gbb_min and float(gba) <= audit_gba_max:
                            gamma_live.append(m)
                    except (TypeError, ValueError):
                        gamma_missing += 1

                clob_has_both = 0
                clob_live = 0
                mismatch = 0
                examples = []
                for m in gamma_live:
                    b = getattr(m, "yes_bid", None)
                    a = getattr(m, "yes_ask", None)
                    if b is None or a is None:
                        continue
                    try:
                        bf = float(b)
                        af = float(a)
                    except (TypeError, ValueError):
                        continue
                    clob_has_both += 1
                    sp = af - bf
                    is_live = (bf >= live_bid_min and af <= live_ask_max and sp <= live_spread_max)
                    if is_live:
                        clob_live += 1
                    else:
                        mismatch += 1
                        if len(examples) < 5:
                            examples.append(
                                {
                                    "market_id": getattr(m, "market_id", "")[:18],
                                    "cat": _market_category(getattr(m, "market_id", ""), datafeed),
                                    "yes_price": getattr(m, "yes_price", None),
                                    "gamma_bb": getattr(m, "gamma_best_bid", None),
                                    "gamma_ba": getattr(m, "gamma_best_ask", None),
                                    "yes_bid": bf,
                                    "yes_ask": af,
                                    "spread": round(sp, 4),
                                    "tte": getattr(m, "seconds_to_resolution", None),
                                    "token_ids": (getattr(m, "clob_token_ids", None) or [])[:2],
                                    "outcomes_order": getattr(m, "outcomes_order", None),
                                }
                            )
                logger.info(
                    "AUDIT Gamma->CLOB: gamma_live=%d (missing_gamma=%d of %d) | clob_has_both=%d | clob_live=%d | mismatch=%d (live thresholds bid>=%.2f ask<=%.2f spread<=%.2f)",
                    len(gamma_live),
                    gamma_missing,
                    len(enriched),
                    clob_has_both,
                    clob_live,
                    mismatch,
                    live_bid_min,
                    live_ask_max,
                    live_spread_max,
                )
                for ex in examples:
                    logger.warning(
                        "AUDIT mismatch: mkt=%s cat=%s yes_price=%s gamma_bb=%s gamma_ba=%s clob_yes_bid=%.3f clob_yes_ask=%.3f spread=%.3f tte=%s token_ids=%s outcomes_order=%s",
                        ex["market_id"],
                        ex["cat"],
                        ex["yes_price"],
                        ex["gamma_bb"],
                        ex["gamma_ba"],
                        ex["yes_bid"],
                        ex["yes_ask"],
                        ex["spread"],
                        ex["tte"],
                        ex["token_ids"],
                        ex["outcomes_order"],
                    )

            # Вариант B: быстрый скрининг по фактической CLOB-книге.
            # Оставляем только рынки с НЕ-tail bid/ask, иначе LLM-слоты почти всегда бессмысленны.
            # NOTE: default thresholds intentionally mild to avoid "0 kept" in tail-heavy regimes
            clob_bid_min = float(cfg.get("clob_screen_bid_min", 0.02))
            clob_ask_max = float(cfg.get("clob_screen_ask_max", 0.98))
            screened = []
            screened_missing = 0
            screened_tail = 0
            for m in enriched:
                b = getattr(m, "yes_bid", None)
                a = getattr(m, "yes_ask", None)
                if b is None or a is None:
                    screened_missing += 1
                    continue
                try:
                    bf = float(b)
                    af = float(a)
                except (TypeError, ValueError):
                    screened_missing += 1
                    continue
                if bf >= clob_bid_min and af <= clob_ask_max:
                    screened.append(m)
                else:
                    screened_tail += 1
            if screened:
                datafeed.markets.clear()
                datafeed.markets.update({m.market_id: m for m in screened})
                datafeed.clob_state.rebuild_from_snapshots(list(datafeed.markets.values()))
                logger.info(
                    "CLOB screening: kept=%d dropped_tail=%d missing/invalid=%d (threshold bid>=%.2f ask<=%.2f)",
                    len(screened), screened_tail, screened_missing, clob_bid_min, clob_ask_max,
                )
            else:
                # Fallback: keep the best-looking slice by spread to still avoid "all 0.01/0.99".
                spread_cap = float(cfg.get("clob_screen_fallback_spread_max", 0.98))
                fallback_keep = int(cfg.get("clob_screen_fallback_keep", 60))
                fallback = []
                for m in enriched:
                    b = getattr(m, "yes_bid", None)
                    a = getattr(m, "yes_ask", None)
                    if b is None or a is None:
                        continue
                    try:
                        bf = float(b)
                        af = float(a)
                    except (TypeError, ValueError):
                        continue
                    if af < bf:
                        continue
                    sp = af - bf
                    if sp <= spread_cap:
                        fallback.append((sp, m))
                fallback_sorted = [m for _, m in sorted(fallback, key=lambda x: x[0])][:fallback_keep]
                if fallback_sorted:
                    datafeed.markets.clear()
                    datafeed.markets.update({m.market_id: m for m in fallback_sorted})
                    datafeed.clob_state.rebuild_from_snapshots(list(datafeed.markets.values()))
                    logger.warning(
                        "CLOB screening: 0 kept by hard thresholds; fallback kept=%d by spread<=%.2f (best spreads)",
                        len(fallback_sorted), spread_cap,
                    )
                else:
                    logger.warning(
                        "CLOB screening: 0 kept (dropped_tail=%d missing/invalid=%d) — keeping full enriched set",
                        screened_tail, screened_missing,
                    )
            return datafeed.markets

        if category == "both" and active:
            await fetch_all_categories()
        elif category == "crypto":
            await datafeed.fetch_crypto_markets(
                min_volume_usd=cfg.get("crypto_min_volume", 50_000),
                min_liquidity_usd=cfg.get("crypto_min_liquidity", 0),
                max_hours_to_resolution=cfg.get("crypto_resolution_hours_max", 12),
            )
        elif category in MARKET_CATEGORIES:
            params = MARKET_CATEGORIES[category]
            await datafeed.fetch_category_markets(
                params.get("gamma_category", category),
                min_volume_usd=params.get("min_volume_usd", 100_000),
            )
        else:
            await datafeed.fetch_politics_markets(min_volume_usd=cfg.get("min_volume", 500_000))
        market_ids = list(datafeed.markets.keys())[: cfg.get("n_markets", 25)]
        if not market_ids:
            logger.warning("No markets loaded; check ACTIVE_CATEGORIES and filters")

        async def get_prices():
            try:
                if category == "both" and active:
                    await fetch_all_categories()
                elif category == "crypto":
                    await datafeed.fetch_crypto_markets(
                        min_volume_usd=cfg.get("crypto_min_volume", 50_000),
                        min_liquidity_usd=cfg.get("crypto_min_liquidity", 0),
                        max_hours_to_resolution=cfg.get("crypto_resolution_hours_max", 12),
                    )
                elif category in MARKET_CATEGORIES:
                    params = MARKET_CATEGORIES[category]
                    await datafeed.fetch_category_markets(
                        params.get("gamma_category", category),
                        min_volume_usd=params.get("min_volume_usd", 100_000),
                    )
                else:
                    await datafeed.fetch_politics_markets(min_volume_usd=cfg.get("min_volume", 500_000))
            except asyncio.CancelledError:
                # Ctrl+C / asyncio.run shutdown cancel the running task with CancelledError.
                # Must re-raise — swallowing it made the main loop ignore shutdown until another await.
                raise
            except Exception as e:
                logger.warning("get_prices failed (stream): %s — keeping previous markets", e)
            return datafeed.markets

        await stream.start_rest(get_prices, market_ids, interval_sec=price_interval)

        try:
            while True:
                tick_count += 1
                if tick_count <= 3 or tick_count % 30 == 0:
                    logger.info("Loop tick %d", tick_count)
                prices = stream.snapshot()

                # CLOB book: цены по открытым позициям
                if trader.positions:
                    token_ids_by_market = {}
                    for mid, pos in trader.positions.items():
                        tid = pos.get("yes_token_id")
                        if mid in datafeed.markets:
                            resolved_tid = get_yes_token_id(datafeed.markets[mid])
                            # Восстановленные/старые позиции могут не хранить yes_token_id.
                            # Нельзя брать clobTokenIds[0] наугад (часто это NO) — иначе цены позиций будут None,
                            # и SL/TP никогда не сработают до резолва.
                            if not tid and resolved_tid:
                                tid = resolved_tid
                                pos["yes_token_id"] = tid
                            # Если позиция была сохранена с неправильным токеном — перепривяжем к актуальному YES.
                            elif tid and resolved_tid and str(tid) != str(resolved_tid):
                                pos["yes_token_id"] = resolved_tid
                                tid = resolved_tid
                        if tid:
                            token_ids_by_market[mid] = tid
                    if token_ids_by_market:
                        pos_prices = await get_position_prices_by_market(
                            list(trader.positions.keys()), token_ids_by_market, session=datafeed.session, clob_state=datafeed.clob_state
                        )
                        for mid, p in pos_prices.items():
                            # p can be None when token book has no bids or fetch failed.
                            # Don't overwrite prices from PriceStream with None; keep last known yes_price
                            # so SL/TP checks can still run and we avoid late overshoot.
                            if p is None:
                                continue
                            tte = None
                            if mid in datafeed.markets:
                                tte = getattr(datafeed.markets[mid], "seconds_to_resolution", None)
                            prices[mid] = {"yes_price": p, "timestamp": time.time(), "seconds_to_resolution": tte}
                    # Раз в N тиков — лог best_bid по каждой позиции (диагностика: видим, когда есть цена до резолва)
                    if tick_count > 0 and tick_count % PORTFOLIO_SUMMARY_EVERY_TICKS == 0:
                        for mid in trader.positions:
                            info = prices.get(mid, {})
                            bid = info.get("yes_price")
                            sl = (trader.positions[mid].get("stop_loss_price") or 0)
                            logger.info("Position price: mid=%s.. best_bid=%s sl=%.2f", mid[:16], bid, sl)
                    # Счётчик тиков подряд без цены по всем позициям; раз в минуту — агрегат в лог
                    for mid in trader.positions:
                        if prices.get(mid, {}).get("yes_price") is None:
                            no_price_streak[mid] = no_price_streak.get(mid, 0) + 1
                        else:
                            no_price_streak[mid] = 0
                    now = time.time()
                    if now - last_no_price_agg_time >= 60.0:
                        last_no_price_agg_time = now
                        with_streak = [(mid, n) for mid, n in no_price_streak.items() if n > 0]
                        if with_streak:
                            by_streak = {}
                            for mid, n in with_streak:
                                by_streak[n] = by_streak.get(n, []) + [mid[:12]]
                            msg = " ".join(f"{n}+ticks={len(mids)}" for n, mids in sorted(by_streak.items(), reverse=True))
                            logger.info("Position no-price streaks (ticks): %d positions without CLOB price: %s", len(with_streak), msg)

                # --- MM: пассивные лимитки (paper); выполняется только при enable_mm=True ---
                if cfg.get("enable_mm", False):
                    mm_params = cfg.get("mm_params")
                    if tick_count == 1 or tick_count % 5 == 0:
                        logger.info(
                            "MM gate: tick=%d has_mm_params=%s markets_count=%d",
                            tick_count, bool(mm_params), len(datafeed.markets),
                        )
                    if mm_params and datafeed.markets:
                        now_ts = time.time()
                        daily_limit_loss = abs(float(mm_params.get("daily_loss_limit_pct", 0.02))) * float(trader.initial_balance or trader.balance)
                        daily_stop = cumulative_realized_pnl <= -daily_limit_loss
                        if tick_count % 10 == 0:
                            logger.info(
                                "MM block: tick=%d daily_stop=%s (realized_pnl=%.2f limit=%.2f)",
                                tick_count, daily_stop, cumulative_realized_pnl, -daily_limit_loss,
                            )
                        order_timeout = float(mm_params.get("order_timeout_seconds", 30))
                        # Снять просроченные ордера
                        for mid in list(pending_mm_orders):
                            if (now_ts - pending_mm_orders[mid].get("placed_ts", 0)) >= order_timeout:
                                del pending_mm_orders[mid]
                                logger.info("MM order timeout: %s", mid[:16] + "..")
                        # Исполнение: ask <= limit_price -> fill
                        for mid in list(pending_mm_orders):
                            if mid in trader.positions:
                                del pending_mm_orders[mid]
                                continue
                            order = pending_mm_orders[mid]
                            snap = datafeed.markets.get(mid)
                            ask = getattr(snap, "yes_ask", None) if snap else None
                            if ask is None and mid in prices and isinstance(prices.get(mid), dict):
                                ask = prices[mid].get("yes_price")
                            if ask is not None and float(ask) <= float(order.get("limit_price", 1)):
                                o = {
                                    "market_id": mid,
                                    "outcome": "YES",
                                    "limit_price": order["limit_price"],
                                    "final_size_usd": order["size_usd"],
                                    "target_size_usd": order["size_usd"],
                                    "yes_token_id": order.get("yes_token_id"),
                                    "stop_loss_price": order.get("stop_loss_price"),
                                    "take_profit_price": order.get("take_profit_price"),
                                    "mm": True,
                                }
                                exec_res = trader.execute_orders([o])
                                for ex in exec_res.get("executions", []):
                                    if ex.get("market_id") != mid:
                                        continue
                                    pos = trader.positions.get(mid, {})
                                    position_state = dict(pos, market_id=mid, final_size_usd=ex.get("cost_usd"))
                                    llm_decision = {"limit_price": order["limit_price"], "final_size_usd": order["size_usd"]}
                                    market_diag = {
                                        "condition_id": mid,
                                        "category": _market_category(mid, datafeed),
                                        "yes_bid": getattr(snap, "yes_bid", None) if snap else None,
                                        "yes_ask": getattr(snap, "yes_ask", None) if snap else None,
                                        "book_ok_at_entry": True,
                                        "yes_token_id": order.get("yes_token_id"),
                                    }
                                    trade_id = log_trade_open(position_state, llm_decision, market_diag, strategy_id="mm")
                                    if trade_id is not None and mid in trader.positions:
                                        trader.positions[mid]["trade_id"] = trade_id
                                    if mid in trader.positions:
                                        _ttl = _market_display_title(mid, datafeed)
                                        if _ttl:
                                            trader.positions[mid]["market_title"] = _ttl
                                    logger.info("MM FILL: %s buy @ %.3f size_usd=%.2f", mid[:16], order["limit_price"], order["size_usd"])
                                del pending_mm_orders[mid]
                                save_state(_root, trader.balance, trader.positions, cumulative_realized_pnl)
                        # Ликвидные рынки и заявки (для диагностики — всегда; для размещения — только если не daily_stop)
                        mm_snapshots = select_mm_markets(list(datafeed.markets.values()), cfg)
                        new_orders = build_mm_orders(
                            trader.positions,
                            mm_snapshots,
                            cfg,
                            set(pending_mm_orders),
                            trader.balance,
                        ) if not daily_stop else []
                        # Диагностика каждые 10 тиков (всегда, чтобы видеть liquid/new_orders даже при daily_stop)
                        if tick_count > 0 and tick_count % 10 == 0:
                            logger.info(
                                "MM diag: markets=%d liquid=%d new_orders=%d pending=%d daily_stop=%s",
                                len(datafeed.markets), len(mm_snapshots), len(new_orders), len(pending_mm_orders), daily_stop,
                            )
                            if daily_stop:
                                logger.warning("MM: daily_stop=True — новые ордера не выставляются (realized_pnl достиг дневного лимита убытка)")
                            elif len(mm_snapshots) == 0:
                                diag = is_liquid_diagnostic(list(datafeed.markets.values()), mm_params)
                                logger.warning(
                                    "MM: 0 liquid — total=%d has_bid_ask=%d spread_ok=%d size_ok=%d levels_ok=%d | with_yes_price=%d with_yes_token_id=%d",
                                    diag["total"], diag["has_bid_ask"], diag["spread_ok"], diag["size_ok"], diag["levels_ok"],
                                    diag.get("with_yes_price", 0), diag.get("with_yes_token_id", 0),
                                )
                                if diag.get("has_bid_ask", 0) == 0 and diag.get("with_yes_token_id", 0) > 0:
                                    logger.warning("MM: CLOB enrich не вернул bid/ask (404? complementary?). Проверь логи CLOB enrich / orderbook.")
                            elif len(new_orders) == 0 and len(pending_mm_orders) == 0 and len(trader.positions) < max(1, mm_params.get("max_open_markets", 3)):
                                logger.warning(
                                    "MM: liquid=%d но new_orders=0 — возможно buy_price >= ask (узкий спред); попробуйте epsilon_price 0.01 или max_spread 0.06",
                                    len(mm_snapshots),
                                )
                        if not daily_stop:
                            for o in new_orders:
                                mid = o.get("market_id")
                                if not mid or mid in pending_mm_orders or mid in trader.positions:
                                    continue
                                pending_mm_orders[mid] = {
                                    "limit_price": o["limit_price"],
                                    "size_usd": o["final_size_usd"],
                                    "side": "buy",
                                    "yes_token_id": o.get("yes_token_id"),
                                    "stop_loss_price": o.get("stop_loss_price"),
                                    "take_profit_price": o.get("take_profit_price"),
                                    "placed_ts": now_ts,
                                }
                                logger.info("MM place: %s buy @ %.3f size=%.2f", mid[:16], o["limit_price"], o["final_size_usd"])

                sl_cooldown = tick_cooldown(sl_cooldown)
                save_cooldown(_root, sl_cooldown)
                tp_cooldown = tick_cooldown(tp_cooldown)
                save_tp_cooldown(_root, tp_cooldown)
                reentry_cooldown = tick_reentry_cooldown(reentry_cooldown)
                save_reentry_cooldown(_root, reentry_cooldown)

                if tick_count > 0 and tick_count % PORTFOLIO_SUMMARY_EVERY_TICKS == 0:
                    mark_to_market = {mid: prices.get(mid, {}).get("yes_price") for mid in trader.positions}
                    metrics = trader.get_portfolio_metrics(mark_to_market if all(v is not None for v in mark_to_market.values()) else None)
                    logger.info(
                        "Portfolio summary: balance=%.2f positions=%d realized_pnl=%.2f unrealized_pnl=%.2f",
                        trader.balance, len(trader.positions), cumulative_realized_pnl, metrics.get("unrealized_pnl", 0),
                    )

                to_close = check_stops(trader, prices, cfg)
                if to_close:
                    close_result = trader.close_positions(to_close)
                    closed = close_result.get("closed", [])
                    sl_pct = cfg.get("sl_pct", 0.04)
                    tp_pct = cfg.get("tp_pct", 0.18)
                    for c in closed:
                        cumulative_realized_pnl += float(c.get("pnl_usd", 0))
                        tid = c.get("trade_id")
                        if tid is not None:
                            size_usd = float(c.get("size_tokens", 0) or 0) * float(c.get("avg_price", 0) or 0)
                            pnl_usd = float(c.get("pnl_usd", 0) or 0)
                            pnl_pct = (pnl_usd / size_usd * 100) if size_usd else None
                            avg_price = float(c.get("avg_price", 0) or 0)
                            sl_at_exit = c.get("sl_at_exit")
                            tp_at_exit = c.get("tp_at_exit")
                            # Подозрительные значения (0.01/0.99 или None) — подставляем из avg_price
                            if sl_at_exit is None or (isinstance(sl_at_exit, (int, float)) and float(sl_at_exit) < 0.02):
                                sl_at_exit = round(max(0.01, avg_price * (1 - sl_pct)), 4) if avg_price > 0 else None
                            else:
                                sl_at_exit = round(float(sl_at_exit), 4)
                            if tp_at_exit is None or (isinstance(tp_at_exit, (int, float)) and float(tp_at_exit) > 0.98):
                                tp_at_exit = round(min(0.99, avg_price * (1 + tp_pct)), 4) if avg_price > 0 else None
                            else:
                                tp_at_exit = round(float(tp_at_exit), 4)
                            log_trade_close(
                                tid,
                                {
                                    "exit_ts": None,
                                    "exit_price": c.get("sell_price"),
                                    "exit_reason": c.get("reason"),
                                    "sl_at_exit": sl_at_exit,
                                    "tp_at_exit": tp_at_exit,
                                },
                                {"pnl_usd": pnl_usd, "pnl_pct": pnl_pct},
                            )
                    save_state(_root, trader.balance, trader.positions, cumulative_realized_pnl)
                    sl_closed = [c["market_id"] for c in closed if c.get("reason") == "SL"]
                    tp_closed = [c["market_id"] for c in closed if c.get("reason") == "TP"]
                    expiry_closed = [c["market_id"] for c in closed if c.get("reason") == "EXPIRY"]
                    time_stop_closed = [c["market_id"] for c in closed if c.get("reason") == "TIME_STOP"]
                    if sl_closed:
                        base_runs = int(cfg.get("sl_cooldown_runs", DEFAULT_RUNS))
                        pol_runs = int(cfg.get("sl_cooldown_runs_politics", base_runs * 4))
                        geo_runs = int(cfg.get("sl_cooldown_runs_geopolitics", base_runs * 4))
                        pol = []
                        geo = []
                        other = []
                        for mid in sl_closed:
                            cat = _market_category(mid, datafeed)
                            if cat == "politics":
                                pol.append(mid)
                            elif cat == "geopolitics":
                                geo.append(mid)
                            else:
                                other.append(mid)
                        if other:
                            sl_cooldown = add_to_cooldown(sl_cooldown, other, runs=base_runs)
                        if pol:
                            sl_cooldown = add_to_cooldown(sl_cooldown, pol, runs=pol_runs)
                        if geo:
                            sl_cooldown = add_to_cooldown(sl_cooldown, geo, runs=geo_runs)
                        save_cooldown(_root, sl_cooldown)
                        logger.info(
                            "SL cooldown applied: base=%dr (other=%d) politics=%dr (n=%d) geopolitics=%dr (n=%d)",
                            base_runs,
                            len(other),
                            pol_runs,
                            len(pol),
                            geo_runs,
                            len(geo),
                        )
                    if tp_closed:
                        tp_cooldown = add_to_cooldown(tp_cooldown, tp_closed, runs=cfg.get("tp_cooldown_runs", DEFAULT_TP_RUNS))
                        save_tp_cooldown(_root, tp_cooldown)
                    if expiry_closed:
                        base_runs = int(cfg.get("sl_cooldown_runs", DEFAULT_RUNS))
                        pol_runs = int(cfg.get("sl_cooldown_runs_politics", base_runs * 4))
                        geo_runs = int(cfg.get("sl_cooldown_runs_geopolitics", base_runs * 4))
                        pol = []
                        geo = []
                        other = []
                        for mid in expiry_closed:
                            cat = _market_category(mid, datafeed)
                            if cat == "politics":
                                pol.append(mid)
                            elif cat == "geopolitics":
                                geo.append(mid)
                            else:
                                other.append(mid)
                        if other:
                            sl_cooldown = add_to_cooldown(sl_cooldown, other, runs=base_runs)
                        if pol:
                            sl_cooldown = add_to_cooldown(sl_cooldown, pol, runs=pol_runs)
                        if geo:
                            sl_cooldown = add_to_cooldown(sl_cooldown, geo, runs=geo_runs)
                        save_cooldown(_root, sl_cooldown)
                        logger.info(
                            "EXPIRY cooldown applied: base=%dr (other=%d) politics=%dr (n=%d) geopolitics=%dr (n=%d)",
                            base_runs,
                            len(other),
                            pol_runs,
                            len(pol),
                            geo_runs,
                            len(geo),
                        )
                    if time_stop_closed:
                        # TIME_STOP = управляемый выход из минуса → тоже блокируем ре-энтри как после SL.
                        base_runs = int(cfg.get("sl_cooldown_runs", DEFAULT_RUNS))
                        pol_runs = int(cfg.get("sl_cooldown_runs_politics", base_runs * 4))
                        geo_runs = int(cfg.get("sl_cooldown_runs_geopolitics", base_runs * 4))
                        pol = []
                        geo = []
                        other = []
                        for mid in time_stop_closed:
                            cat = _market_category(mid, datafeed)
                            if cat == "politics":
                                pol.append(mid)
                            elif cat == "geopolitics":
                                geo.append(mid)
                            else:
                                other.append(mid)
                        if other:
                            sl_cooldown = add_to_cooldown(sl_cooldown, other, runs=base_runs)
                        if pol:
                            sl_cooldown = add_to_cooldown(sl_cooldown, pol, runs=pol_runs)
                        if geo:
                            sl_cooldown = add_to_cooldown(sl_cooldown, geo, runs=geo_runs)
                        save_cooldown(_root, sl_cooldown)
                        logger.info(
                            "TIME_STOP cooldown: %d market(s) added for %d runs (no re-entry)",
                            len(time_stop_closed),
                            base_runs,
                        )
                        logger.info(
                            "TIME_STOP cooldown applied: base=%dr (other=%d) politics=%dr (n=%d) geopolitics=%dr (n=%d)",
                            base_runs,
                            len(other),
                            pol_runs,
                            len(pol),
                            geo_runs,
                            len(geo),
                        )
                    for c in closed:
                        logger.info("PAPER CLOSE market=%s %s PnL $%.2f", c["market_id"][:12], c.get("level_msg", c.get("reason")), c.get("pnl_usd", 0))
                    if send_telegram_message:
                        lines = []
                        pnl_sum = 0.0
                        for c in closed:
                            mid = c.get("market_id") or ""
                            pnl = float(c.get("pnl_usd") or 0)
                            pnl_sum += pnl
                            reason = str(c.get("reason") or "?")
                            title = (c.get("market_title") or "").strip()
                            if not title:
                                title = _market_display_title(mid, datafeed)
                            if not title:
                                title = f"{mid[:20]}.." if len(mid) > 22 else mid or "?"
                            cat = ""
                            if mid and mid in datafeed.markets:
                                cat = _market_category(mid, datafeed).capitalize()
                            lvl = (c.get("level_msg") or "").strip()
                            is_copy = str(c.get("source") or c.get("strategy_id") or "").strip().lower() == "copy"
                            copy_bit = "[COPY] " if is_copy else ""
                            cat_bit = f"[{cat}] " if cat else ""
                            # Одна строка на сделку: причина, категория, название, деталь выхода, PnL
                            lines.append(
                                f"{copy_bit}{cat_bit}[{reason}] {title}\n  {lvl} | PnL ${pnl:,.2f}"
                            )
                        # If many closes at once, keep the message readable.
                        head = lines[:6]
                        more = len(lines) - len(head)
                        body = "\n".join(head) + (f"\n... +{more} more" if more > 0 else "")
                        await send_telegram_message(
                            "ClawBot v2 Exit\n"
                            + body
                            + f"\nClosed {len(closed)} | Sum PnL ${pnl_sum:,.2f} | Balance: ${trader.balance:,.0f}"
                        )

                if elapsed > 0 and elapsed % llm_sec == 0:
                    # --- LLM Slot: 1) кандидаты по пригодности (YES в диапазоне), потом топ по спреду 2) strategy 3) risk 4) execute ---
                    try:
                        await get_prices()  # свежие данные в datafeed
                    except Exception as e:
                        logger.warning("get_prices failed (network/transient): %s — skip this LLM slot", e)
                        continue
                    open_market_ids = set(trader.positions.keys())
                    sl_cooldown_ids = get_cooldown_set(sl_cooldown)
                    tp_cooldown_ids = get_cooldown_set(tp_cooldown)
                    reentry_cooldown_ids = get_reentry_cooldown_set(reentry_cooldown)
                    excluded_markets = set(cfg.get("excluded_markets") or [])
                    exclude_ids = open_market_ids | sl_cooldown_ids | tp_cooldown_ids | reentry_cooldown_ids | excluded_markets
                    logger.info("*** LLM SLOT STARTED exclude=%d (held+cooldown+excluded_markets=%d) ***", len(exclude_ids), len(excluded_markets))

                    # Хардкод для гарантии (вместо cfg). min_yes=0.00001 — пусть 0.0000 войдут. Ждём движения цен → LLM заработает!
                    min_yes_h = 0.00001
                    max_entry_h = 0.9999
                    logger.info("DEBUG HARDCODE: min_yes=%s max_entry=%s", min_yes_h, max_entry_h)
                    # DEBUG categories: politics=X sports=Y culture=Z total=N
                    cat_counts: dict = {}
                    for mid in datafeed.markets:
                        c = _market_category(mid, datafeed)
                        cat_counts[c] = cat_counts.get(c, 0) + 1
                    parts = " ".join(f"{k}={v}" for k, v in sorted(cat_counts.items()))
                    logger.info("DEBUG categories: %s total=%d", parts, len(datafeed.markets))
                    # LLM slot: состояние CLOB и отбор кандидатов (один источник истины — has_live_orderbook_for_market)
                    logger.info("LLM slot: CLOB state (datafeed) — next line")
                    _markets_with_live_book = sum(
                        1 for mid in datafeed.markets
                        if has_live_orderbook_for_market(mid, datafeed.clob_state)
                    )
                    logger.info(
                        "CLOB state (datafeed): markets_with_live_book=%d total=%d",
                        _markets_with_live_book, len(datafeed.markets),
                    )

                    # Tail-guard (быстрый, до кандидатов/LLM): если по пулу bid_max <= 0.05 и ask_min >= 0.95,
                    # считаем это режимом «хвоста» и пропускаем слот целиком (экономим токены).
                    _bids_all = []
                    _asks_all = []
                    for mid in datafeed.markets:
                        m = datafeed.markets.get(mid)
                        if not m:
                            continue
                        b = getattr(m, "yes_bid", None)
                        a = getattr(m, "yes_ask", None)
                        if b is not None:
                            _bids_all.append(float(b))
                        if a is not None:
                            _asks_all.append(float(a))
                    bid_max_all = max(_bids_all) if _bids_all else None
                    ask_min_all = min(_asks_all) if _asks_all else None
                    if bid_max_all is not None and ask_min_all is not None and bid_max_all <= 0.05 and ask_min_all >= 0.95:
                        global _tail_guard_force_llm_used
                        # По умолчанию включено: 1 раз за запуск пропускаем guard, чтобы увидеть edge-логи.
                        force_once = bool(cfg.get("force_llm_once_in_tail", True))
                        if force_once and not _tail_guard_force_llm_used:
                            _tail_guard_force_llm_used = True
                            logger.warning(
                                "Tail guard (pool): WOULD skip (bid_max=%.3f ask_min=%.3f) but forcing ONE LLM call for diagnostics (force_llm_once_in_tail=true)",
                                bid_max_all, ask_min_all,
                            )
                        else:
                            logger.warning(
                                "Tail guard (pool): skipping LLM slot. bid_max=%.3f ask_min=%.3f (threshold bid_max<=0.05 & ask_min>=0.95)",
                                bid_max_all, ask_min_all,
                            )
                            continue
                    if bid_max_all is not None and ask_min_all is not None:
                        logger.info("Tail guard (pool): bid_max=%.3f ask_min=%.3f", bid_max_all, ask_min_all)

                    liquidity_params = cfg.get("entry_liquidity")
                    candidates = datafeed.get_tradeable_top(
                        25, max_entry_h, min_yes_h, exclude_ids,
                        liquidity_params=liquidity_params,
                    )
                    if len(candidates) == 0 and liquidity_params:
                        logger.warning(
                            "0 candidates with entry_liquidity filter (spread/size/levels); retry without liquidity filter to get candidates (risk: wider spread / EXPIRY)"
                        )
                        candidates = datafeed.get_tradeable_top(
                            25, max_entry_h, min_yes_h, exclude_ids,
                            liquidity_params=None,
                        )
                        if candidates:
                            # Отсечь только явно мёртвые (bid 0.01 / ask 0.999); в fallback допускаем 0.02/0.99 — иначе при CLOB spread 0.97–1.0 всегда 0 кандидатов.
                            before = len(candidates)
                            candidates = [
                                c for c in candidates
                                if (getattr(c, "yes_bid", None) is None or float(getattr(c, "yes_bid", 0)) > 0.01)
                                and (getattr(c, "yes_ask", None) is None or float(getattr(c, "yes_ask", 1)) <= 0.99)
                            ]
                            if before > len(candidates):
                                logger.info("Fallback: dropped %d candidates with dead book (bid<=0.01 or ask>0.99), %d left", before - len(candidates), len(candidates))
                            # Fallback: только рынки, где Gamma yes_price не «почти решён» — снижает риск мгновенного EXPIRY.
                            fallback_yes_min = float(cfg.get("fallback_yes_price_min", 0.15))
                            fallback_yes_max = float(cfg.get("fallback_yes_price_max", 0.85))
                            before_gamma = len(candidates)
                            candidates = [
                                c for c in candidates
                                if fallback_yes_min <= (getattr(c, "yes_price", None) or 0) <= fallback_yes_max
                            ]
                            if before_gamma > len(candidates):
                                logger.info("Fallback: dropped %d candidates (yes_price outside [%.2f, %.2f]), %d left", before_gamma - len(candidates), fallback_yes_min, fallback_yes_max, len(candidates))
                            if candidates:
                                logger.info("Fallback: %d candidates without liquidity filter (dead-book + Gamma band applied)", len(candidates))
                    binary_count = len(candidates)
                    # Отладка: по первому кандидату — что в snapshot vs что в clob_state
                    if binary_count > 0:
                        c0 = candidates[0]
                        mid0 = getattr(c0, "market_id", None) or ""
                        token_ids = getattr(datafeed.clob_state, "market_tokens", None) and datafeed.clob_state.market_tokens.get(mid0)
                        first_tid = token_ids[0] if token_ids else None
                        st0 = (getattr(datafeed.clob_state, "tokens", None) or {}).get(first_tid) if first_tid else None
                        logger.info(
                            "LLM CLOB debug: market_id=%s yes_bid=%s yes_ask=%s | clob_state: token_ids_len=%s first_token_has_bid=%s first_token_has_ask=%s",
                            mid0[:20] + ".." if len(mid0) > 20 else mid0,
                            getattr(c0, "yes_bid", None),
                            getattr(c0, "yes_ask", None),
                            len(token_ids) if token_ids else 0,
                            getattr(st0, "has_bid", None) if st0 else None,
                            getattr(st0, "has_ask", None) if st0 else None,
                        )
                    # Вариант B1 (минимально): источник истины — фактические yes_bid/yes_ask в snapshot,
                    # а не clob_state.has_bid/has_ask (который может лагать/не совпадать).
                    before_book = len(candidates)
                    candidates = [
                        c for c in candidates
                        if getattr(c, "yes_bid", None) is not None and getattr(c, "yes_ask", None) is not None
                    ]
                    rejected = before_book - len(candidates)
                    logger.info(
                        "LLM candidates: after SNAPSHOT book filter FINAL=%d (rejected missing_yes_bid_or_ask=%d from %d)",
                        len(candidates), rejected, before_book,
                    )

                    # Pre-LLM screening: avoid spending LLM calls on tail/wide markets.
                    # Same thresholds as entry-guard, but applied earlier (candidate stage).
                    pre_llm_bid_min = float(cfg.get("entry_bid_min", 0.05))
                    pre_llm_ask_max = float(cfg.get("entry_ask_max", 0.95))
                    pre_llm_spread_min = float(cfg.get("entry_spread_min", 0.0))
                    pre_llm_spread_max = float(cfg.get("entry_spread_max", 0.25))
                    pre_llm_tte_min_sec = float(cfg.get("min_time_to_expiry_sec", 7200))
                    if candidates:
                        before_pre_llm = len(candidates)
                        kept = []
                        rej: dict[str, int] = {}
                        for c in candidates:
                            b = getattr(c, "yes_bid", None)
                            a = getattr(c, "yes_ask", None)
                            if b is None or a is None:
                                rej["missing_bid_ask"] = rej.get("missing_bid_ask", 0) + 1
                                continue
                            try:
                                bf = float(b)
                                af = float(a)
                            except (TypeError, ValueError):
                                rej["bad_bid_ask"] = rej.get("bad_bid_ask", 0) + 1
                                continue
                            spread = af - bf
                            if spread < pre_llm_spread_min:
                                rej["spread_too_narrow"] = rej.get("spread_too_narrow", 0) + 1
                                continue
                            if bf < pre_llm_bid_min or af > pre_llm_ask_max or spread > pre_llm_spread_max:
                                rej["tail_or_wide"] = rej.get("tail_or_wide", 0) + 1
                                continue
                            tte = getattr(c, "seconds_to_resolution", None)
                            if tte is not None:
                                try:
                                    if float(tte) < pre_llm_tte_min_sec:
                                        rej["tte_too_low"] = rej.get("tte_too_low", 0) + 1
                                        continue
                                except (TypeError, ValueError):
                                    rej["bad_tte"] = rej.get("bad_tte", 0) + 1
                                    continue
                            kept.append(c)
                        candidates = kept
                        dropped = before_pre_llm - len(candidates)
                        if dropped > 0:
                            parts = " ".join(f"{k}={v}" for k, v in sorted(rej.items(), key=lambda kv: (-kv[1], kv[0])))
                            logger.info(
                                "Pre-LLM screening: %d -> %d candidates (bid>=%.2f ask<=%.2f %.2f<=spread<=%.2f tte>=%ds if present). dropped=%d reasons: %s",
                                before_pre_llm,
                                len(candidates),
                                pre_llm_bid_min,
                                pre_llm_ask_max,
                                pre_llm_spread_min,
                                pre_llm_spread_max,
                                int(pre_llm_tte_min_sec),
                                dropped,
                                parts or "-",
                            )

                    if len(candidates) == 0:
                        logger.warning("0 candidates — skip LLM")
                    else:
                        pass

                    if len(candidates) == 0:
                        logger.warning("0 candidates — skip LLM")
                    else:
                        pnl_before_interval = cumulative_realized_pnl
                        # Опционально: LLM-батч через llm_adapter.build_llm_batch_payload(candidates, portfolio, mode="15M_CRYPTO_BATCH")
                        # -> запрос к модели -> llm_adapter.llm_decisions_to_orders(decisions_json, datafeed, portfolio) -> approved_orders
                        diag = datafeed.tradeable_diagnostic(max_entry_h, min_yes_h, exclude_ids)
                        logger.info(
                            "LLM: binary %d | диапазон: %d → top %d",
                            diag["binary_total"], diag["in_yes_range"], len(candidates),
                        )
                        strategy = ClawBotStrategy(
                            use_llm=True,
                            base_balance_usd=cfg.get("initial_balance", 100_000),
                            min_ev_threshold=cfg.get("min_ev_threshold", 0.02),
                            min_volume_usd=cfg["min_volume"],
                            min_yes_edge=cfg["min_yes_edge"],
                            sl_pct=cfg.get("sl_pct", 0.04),
                            tp_pct=cfg.get("tp_pct", 0.18),
                        )
                        signals = strategy.generate_signals(candidates)
                        copy_cfg = cfg.get("copy_trading") or {}
                        if bool(copy_cfg.get("enabled", False)):
                            copy_exclude: set = set(sl_cooldown_ids | tp_cooldown_ids | reentry_cooldown_ids | excluded_markets)
                            if bool(copy_cfg.get("exclude_open_positions", True)):
                                copy_exclude |= open_market_ids
                            copy_signals, copy_stats = build_copy_signals(
                                root_dir=_root,
                                copy_cfg=copy_cfg,
                                markets_by_id=datafeed.markets,
                                exclude_ids=copy_exclude,
                                sl_pct=cfg.get("sl_pct", 0.04),
                                tp_pct=cfg.get("tp_pct", 0.18),
                            )
                            if copy_stats.get("raw", 0) > 0:
                                logger.info(
                                    "Copy-trading adapter: raw=%d kept=%d excluded=%d missing_market=%d entry_too_high=%d bad_record=%d file=%s",
                                    copy_stats.get("raw", 0),
                                    copy_stats.get("kept", 0),
                                    copy_stats.get("excluded", 0),
                                    copy_stats.get("missing_market", 0),
                                    copy_stats.get("entry_too_high", 0),
                                    copy_stats.get("bad_record", 0),
                                    copy_cfg.get("signals_file", "copy_signals.json"),
                                )
                            if copy_signals:
                                signals.extend(copy_signals)
                                # Deduplicate by market_id: keep the strongest expected_ev.
                                by_market: dict[str, dict] = {}
                                for s in signals:
                                    mid = str(s.get("market_id") or "")
                                    if not mid:
                                        continue
                                    try:
                                        ev = float(s.get("expected_ev") or 0.0)
                                    except (TypeError, ValueError):
                                        ev = 0.0
                                    prev = by_market.get(mid)
                                    if prev is None:
                                        by_market[mid] = s
                                        continue
                                    try:
                                        prev_ev = float(prev.get("expected_ev") or 0.0)
                                    except (TypeError, ValueError):
                                        prev_ev = 0.0
                                    if ev >= prev_ev:
                                        if _signal_is_copy(prev):
                                            ws = str(s.get("source") or "").strip().lower() or "llm"
                                            _copy_trace_drop(
                                                "dedupe_lost_to_higher_ev",
                                                mid,
                                                f"dropped_copy_ev={prev_ev:.4f} kept_ev={ev:.4f} kept_source={ws}",
                                            )
                                        by_market[mid] = s
                                    else:
                                        if _signal_is_copy(s):
                                            ws = str(prev.get("source") or "").strip().lower() or "llm"
                                            _copy_trace_drop(
                                                "dedupe_lost_to_higher_ev",
                                                mid,
                                                f"dropped_copy_ev={ev:.4f} kept_ev={prev_ev:.4f} kept_source={ws}",
                                            )
                                signals = list(by_market.values())
                                logger.info(
                                    "Signals merged (LLM/simple + copy): total=%d",
                                    len(signals),
                                )
                        for s in signals:
                            s["category"] = _market_category(s.get("market_id"), datafeed)
                        # Sanity-filter: for BUY_YES we require limit_price <= 0.50 (otherwise edge <= 0).
                        # Prevent wasting slots on invalid/contradicting LLM outputs.
                        before_sanity = len(signals)
                        kept = []
                        dropped_examples = []
                        for s in signals:
                            try:
                                lp = float(s.get("limit_price") or 0.5)
                            except (TypeError, ValueError):
                                lp = 0.5
                            side = str(s.get("side") or s.get("action") or "BUY_YES").upper()
                            if "BUY" in side and "YES" in side and lp > 0.50:
                                if _signal_is_copy(s):
                                    _copy_trace_drop("sanity_buy_yes_limit_gt_50", str(s.get("market_id") or ""), f"limit_price={lp:.4f}")
                                if len(dropped_examples) < 3:
                                    dropped_examples.append((s.get("market_id") or "", lp, s.get("category") or ""))
                                continue
                            kept.append(s)
                        signals = kept
                        if before_sanity > len(signals):
                            logger.warning(
                                "LLM sanity filter: dropped %d/%d BUY_YES signals with limit_price>0.50 (edge<=0). examples=%s",
                                before_sanity - len(signals),
                                before_sanity,
                                [(m[:12], round(lp, 4), c) for (m, lp, c) in dropped_examples],
                            )
                        # Лог edge перед фильтром по категориям (чтобы понять, насколько не дотягиваем)
                        # edge = 0.50 - limit_price (для BUY_YES ниже 0.5; чем ниже, тем больше edge)
                        for i, s in enumerate(signals):
                            cat = s.get("category") or ""
                            lp = float(s.get("limit_price") or 0.5)
                            edge = 0.50 - lp
                            min_edge = float(MARKET_CATEGORIES.get(cat, {}).get("min_edge", 0.05))
                            mid = s.get("market_id") or ""
                            logger.info(
                                "Edge pre-filter #%d: cat=%s edge=%.4f min_edge=%.4f limit_price=%.4f market=%s",
                                i + 1, cat, edge, min_edge, lp, (mid[:16] + "..") if len(mid) > 16 else mid,
                            )

                        # Фильтр по min_edge категории
                        n_before = len(signals)
                        kept_me: list = []
                        for s in signals:
                            lp = float(s.get("limit_price") or 0.5)
                            edge = 0.50 - lp
                            cat = s.get("category") or ""
                            min_edge = float(MARKET_CATEGORIES.get(cat, {}).get("min_edge", 0.05))
                            if edge >= min_edge:
                                kept_me.append(s)
                            elif _signal_is_copy(s):
                                _copy_trace_drop(
                                    "category_min_edge",
                                    str(s.get("market_id") or ""),
                                    f"edge={edge:.4f} min_edge={min_edge:.4f} cat={cat}",
                                )
                        signals = kept_me
                        if n_before > len(signals):
                            logger.info("Category min_edge filter: %d -> %d signals", n_before, len(signals))
                        # Entry score floor: системный гейт против слабых LLM сигналов.
                        entry_min_llm_score = float(cfg.get("entry_min_llm_score", 0.0) or 0.0)
                        if entry_min_llm_score > 0 and signals:
                            before_score = len(signals)
                            score_reject = 0
                            filtered_signals = []
                            for s in signals:
                                src = str(s.get("source") or "").strip().lower()
                                # Copy-trading signals already passed external filter; don't double-gate by llm_score.
                                if src == "copy":
                                    filtered_signals.append(s)
                                    continue
                                raw_score = s.get("expected_ev", s.get("llm_score"))
                                try:
                                    score = float(raw_score if raw_score is not None else 0.0)
                                except (TypeError, ValueError):
                                    score = 0.0
                                if score < entry_min_llm_score:
                                    score_reject += 1
                                    continue
                                filtered_signals.append(s)
                            signals = filtered_signals
                            if score_reject > 0:
                                logger.info(
                                    "Entry llm_score floor: dropped %d/%d signals (required >= %.2f)",
                                    score_reject,
                                    before_score,
                                    entry_min_llm_score,
                                )
                        risk_mgr = RiskManager()
                        risk_mgr.config["max_single_market_pct"] = cfg["risk_per_trade"]
                        risk_mgr.config["max_category_pct"] = cfg["max_category_pct"]
                        risk_mgr.config["max_exposure_pct"] = cfg["max_exposure_pct"]
                        risk_mgr.config["min_reward_risk_ratio"] = cfg.get("min_reward_risk_ratio", 1.5)
                        risk_mgr.config["high_entry_ratio_exempt"] = cfg.get("high_entry_ratio_exempt", 0.95)
                        risk_mgr.config["max_entry_price"] = cfg.get("max_entry_price", 0.90)
                        risk_mgr.config["sl_pct"] = cfg.get("sl_pct", 0.04)
                        risk_mgr.config["tp_pct"] = cfg.get("tp_pct", 0.18)
                        risk_mgr.config["size_scale_by_ev"] = cfg.get("size_scale_by_ev", False)
                        risk_mgr.config["ev_size_min_multiplier"] = cfg.get("ev_size_min_multiplier", 0.5)
                        risk_mgr.config["ev_size_max_multiplier"] = cfg.get("ev_size_max_multiplier", 1.25)
                        risk_mgr.config["ev_size_min_ev"] = cfg.get("ev_size_min_ev", cfg.get("min_ev_threshold", 0.04))
                        risk_mgr.config["ev_size_max_ev"] = cfg.get("ev_size_max_ev", 0.12)
                        risk_mgr.config["max_open_positions"] = int(cfg.get("max_open_positions") or 0)
                        risk_mgr.portfolio.balance_usd = trader.balance
                        risk_mgr.portfolio.positions = {
                            mid: float(p.get("size_tokens", 0)) * float(p.get("avg_price", 0))
                            for mid, p in trader.positions.items()
                        }
                        for mid, size in risk_mgr.portfolio.positions.items():
                            cat = _market_category(mid, datafeed)
                            risk_mgr.portfolio.exposure_by_category[cat] = risk_mgr.portfolio.exposure_by_category.get(cat, 0) + size
                        copy_mids_pre_risk = {
                            str(s.get("market_id") or "")
                            for s in signals
                            if _signal_is_copy(s) and str(s.get("market_id") or "")
                        }
                        risk_result = risk_mgr.process_signals(signals)
                        approved_orders = risk_result["approved_orders"]
                        if risk_result.get("rejected_signals"):
                            for i, reason in enumerate(risk_result["rejected_signals"]):
                                logger.warning("Risk rejected signal %d: %s", i + 1, reason)
                        approved_mids_risk = {o.get("market_id") for o in approved_orders if o.get("market_id")}
                        for mid in copy_mids_pre_risk:
                            if mid not in approved_mids_risk:
                                _copy_trace_drop("risk_not_approved", mid, "check Risk rejected lines above")
                        pct_vol = cfg.get("max_trade_pct_of_volume", 0.05)
                        pct_depth = cfg.get("max_trade_pct_of_depth", 0.10)
                        copy_mids_pre_book = {
                            o.get("market_id")
                            for o in approved_orders
                            if o.get("market_id") and str(o.get("source") or "").strip().lower() == "copy"
                        }
                        approved_orders = [o for o in approved_orders if _order_has_book(o, datafeed)]
                        approved_mids_book = {o.get("market_id") for o in approved_orders if o.get("market_id")}
                        for mid in copy_mids_pre_book:
                            if mid not in approved_mids_book:
                                _copy_trace_drop("no_yes_book_after_risk", mid, "dropped by _order_has_book")
                        if len(risk_result["approved_orders"]) != len(approved_orders):
                            logger.info("Dropped %d orders (no yes_bid/yes_ask); executing %d", len(risk_result["approved_orders"]) - len(approved_orders), len(approved_orders))
                        for order in approved_orders:
                            mid = order.get("market_id")
                            if not mid or mid not in datafeed.markets:
                                continue
                            m = datafeed.markets[mid]
                            vol_cap = (m.volume_usd or 0) * pct_vol
                            depth_cap = (getattr(m, "liquidity_usd", 0) or 0) * pct_depth
                            max_size = vol_cap if vol_cap > 0 else 0
                            if depth_cap > 0:
                                max_size = min(max_size, depth_cap) if max_size > 0 else depth_cap
                            if max_size > 0:
                                current = float(order.get("final_size_usd") or order.get("target_size_usd") or 0)
                                if current > max_size:
                                    order["final_size_usd"] = round(max_size, 2)
                                    logger.info("Volume cap %s: size -> %.0f", mid[:12], order["final_size_usd"])
                        for order in approved_orders:
                            mid = order.get("market_id")
                            lp = float(order.get("limit_price") or 0)
                            if lp <= 0.02 and mid and mid in datafeed.markets:
                                order["limit_price"] = max(0.02, float(datafeed.markets[mid].yes_price))
                            elif lp <= 0.02:
                                order["limit_price"] = 0.50
                            if mid and mid in datafeed.markets:
                                # Always use resolved YES token (post-enrich swap), never clob_token_ids[0] blindly.
                                order["yes_token_id"] = get_yes_token_id(datafeed.markets[mid])
                        # Hard entry-guard: skip tail-ish books even if LLM/risk approved.
                        # Goal: collect 5–10 trades only in "live" markets (non 0.01/0.99 regime) with TTE >= 2h.
                        entry_bid_min = float(cfg.get("entry_bid_min", 0.05))
                        entry_ask_max = float(cfg.get("entry_ask_max", 0.95))
                        entry_spread_min = float(cfg.get("entry_spread_min", 0.0))
                        entry_spread_max = float(cfg.get("entry_spread_max", 0.25))
                        entry_tte_min_sec = float(cfg.get("min_time_to_expiry_sec", 7200))
                        if approved_orders:
                            before_entry_guard = len(approved_orders)
                            copy_mids_pre_entry_guard = {
                                o.get("market_id")
                                for o in approved_orders
                                if o.get("market_id") and str(o.get("source") or "").strip().lower() == "copy"
                            }
                            kept = []
                            rejected_reasons: dict[str, int] = {}
                            for o in approved_orders:
                                mid = o.get("market_id")
                                snap = datafeed.markets.get(mid) if mid else None
                                if not snap:
                                    rejected_reasons["missing_snapshot"] = rejected_reasons.get("missing_snapshot", 0) + 1
                                    continue
                                b = getattr(snap, "yes_bid", None)
                                a = getattr(snap, "yes_ask", None)
                                tte = getattr(snap, "seconds_to_resolution", None)
                                if b is None or a is None:
                                    rejected_reasons["missing_bid_ask"] = rejected_reasons.get("missing_bid_ask", 0) + 1
                                    continue
                                try:
                                    bf = float(b)
                                    af = float(a)
                                except (TypeError, ValueError):
                                    rejected_reasons["bad_bid_ask"] = rejected_reasons.get("bad_bid_ask", 0) + 1
                                    continue
                                spread = af - bf
                                # TTE guard: enforce only when present (Gamma sometimes omits endDate/endDateIso)
                                if tte is None:
                                    rejected_reasons["missing_tte_allowed"] = rejected_reasons.get("missing_tte_allowed", 0) + 1
                                else:
                                    try:
                                        tte_f = float(tte)
                                    except (TypeError, ValueError):
                                        rejected_reasons["bad_tte"] = rejected_reasons.get("bad_tte", 0) + 1
                                        continue
                                    if tte_f < entry_tte_min_sec:
                                        rejected_reasons["tte_too_low"] = rejected_reasons.get("tte_too_low", 0) + 1
                                        continue
                                if spread < entry_spread_min:
                                    rejected_reasons["spread_too_narrow"] = rejected_reasons.get("spread_too_narrow", 0) + 1
                                    continue
                                if bf < entry_bid_min or af > entry_ask_max or spread > entry_spread_max:
                                    rejected_reasons["tail_or_wide"] = rejected_reasons.get("tail_or_wide", 0) + 1
                                    continue
                                kept.append(o)
                            approved_orders = kept
                            dropped = before_entry_guard - len(approved_orders)
                            copy_mids_post_entry_guard = {
                                o.get("market_id")
                                for o in approved_orders
                                if o.get("market_id") and str(o.get("source") or "").strip().lower() == "copy"
                            }
                            for mid in copy_mids_pre_entry_guard:
                                if mid not in copy_mids_post_entry_guard:
                                    _copy_trace_drop("entry_guard_rejected", mid, "see Entry guard WARNING reasons above")
                            if dropped > 0:
                                parts = " ".join(f"{k}={v}" for k, v in sorted(rejected_reasons.items(), key=lambda kv: (-kv[1], kv[0])))
                                logger.warning(
                                    "Entry guard: dropped %d/%d orders (bid>=%.2f ask<=%.2f %.2f<=spread<=%.2f tte>=%ds). reasons: %s",
                                    dropped,
                                    before_entry_guard,
                                    entry_bid_min,
                                    entry_ask_max,
                                    entry_spread_min,
                                    entry_spread_max,
                                    int(entry_tte_min_sec),
                                    parts or "-",
                                )
                        max_new_slot = int(cfg.get("max_new_trades_per_llm_slot") or 0)
                        if approved_orders and max_new_slot > 0:
                            copy_mids_pre_slot = {
                                o.get("market_id")
                                for o in approved_orders
                                if o.get("market_id") and str(o.get("source") or "").strip().lower() == "copy"
                            }
                            filtered_slot: list = []
                            new_in_slot = 0
                            for o in approved_orders:
                                mid = o.get("market_id")
                                is_new = bool(mid and mid not in trader.positions)
                                if is_new and new_in_slot >= max_new_slot:
                                    continue
                                if is_new:
                                    new_in_slot += 1
                                filtered_slot.append(o)
                            dropped_slot = len(approved_orders) - len(filtered_slot)
                            copy_mids_post_slot = {
                                o.get("market_id")
                                for o in filtered_slot
                                if o.get("market_id") and str(o.get("source") or "").strip().lower() == "copy"
                            }
                            for mid in copy_mids_pre_slot:
                                if mid not in copy_mids_post_slot:
                                    _copy_trace_drop(
                                        "max_new_trades_per_llm_slot",
                                        mid,
                                        f"cap={max_new_slot}",
                                    )
                            if dropped_slot:
                                logger.warning(
                                    "max_new_trades_per_llm_slot=%d: dropped %d new-market order(s); executing %d",
                                    max_new_slot,
                                    dropped_slot,
                                    len(filtered_slot),
                                )
                            approved_orders = filtered_slot
                        if approved_orders:
                            for o in approved_orders:
                                if str(o.get("source") or "").strip().lower() == "copy":
                                    logger.info(
                                        "Copy-tracing: executing market=%s final_size_usd=%s",
                                        _mid_short(str(o.get("market_id") or "")),
                                        o.get("final_size_usd") or o.get("target_size_usd"),
                                    )
                            execution_result = trader.execute_orders(approved_orders)
                            ex_cost_by_mid = {
                                (ex.get("market_id") or ""): float(ex.get("cost_usd") or 0)
                                for ex in (execution_result.get("executions") or [])
                                if ex.get("market_id")
                            }
                            # Prevent immediate re-entry into the same market for a time window.
                            reentry_min = float(cfg.get("reentry_cooldown_minutes", 60))
                            reentry_ticks = int(max(0.0, reentry_min * 60.0) / max(1.0, float(loop_sec)))
                            entered_mids = [o.get("market_id") for o in approved_orders if o.get("market_id")]
                            if entered_mids and reentry_ticks > 0:
                                reentry_cooldown = add_to_reentry_cooldown(reentry_cooldown, entered_mids, ticks=reentry_ticks)
                                save_reentry_cooldown(_root, reentry_cooldown)
                            orders_by_mid = {o["market_id"]: o for o in approved_orders if o.get("market_id")}
                            for ex in execution_result.get("executions", []):
                                mid = ex.get("market_id")
                                if not mid or mid not in trader.positions:
                                    continue
                                pos = trader.positions[mid]
                                if pos.get("opened_ts") is None:
                                    pos["opened_ts"] = time.time()
                                order = orders_by_mid.get(mid, {})
                                snap = datafeed.markets.get(mid)
                                yes_tid = (get_yes_token_id(snap) if snap else None) or pos.get("yes_token_id")
                                book_ok_at_entry = getattr(snap, "yes_bid", None) is not None if snap else False
                                logger.info(
                                    "Position opened: market_id=%s conditionId=%s yes_token_id=%s book_ok_at_entry=%s",
                                    mid[:16] + ".." if len(mid) > 16 else mid,
                                    (mid[:16] + ".." if len(mid) > 16 else mid),
                                    (yes_tid[:16] + ".." if yes_tid and len(str(yes_tid)) > 16 else (yes_tid or "")),
                                    book_ok_at_entry,
                                )
                                # Entry diagnostics: quickly spot "live" (non-tail) markets
                                if snap:
                                    b = getattr(snap, "yes_bid", None)
                                    a = getattr(snap, "yes_ask", None)
                                    spr = None
                                    try:
                                        if b is not None and a is not None:
                                            spr = float(a) - float(b)
                                    except (TypeError, ValueError):
                                        spr = None
                                    logger.info(
                                        "Entry book: market=%s cat=%s tte_sec=%s yes_bid=%s yes_ask=%s spread=%s best_bid_sz=%s best_ask_sz=%s levels(b/a)=%s/%s",
                                        mid[:16] + ".." if len(mid) > 16 else mid,
                                        _market_category(mid, datafeed),
                                        getattr(snap, "seconds_to_resolution", None),
                                        b,
                                        a,
                                        round(spr, 4) if isinstance(spr, (int, float)) else None,
                                        getattr(snap, "best_bid_size", None),
                                        getattr(snap, "best_ask_size", None),
                                        getattr(snap, "book_bids_count", None),
                                        getattr(snap, "book_asks_count", None),
                                    )
                                position_state = dict(pos, market_id=mid, final_size_usd=ex.get("cost_usd"))
                                llm_decision = dict(order, limit_price=order.get("limit_price") or ex.get("fill_price"))
                                market_diag = {
                                    "condition_id": mid,
                                    "category": _market_category(mid, datafeed),
                                    "yes_bid": getattr(snap, "yes_bid", None) if snap else None,
                                    "yes_ask": getattr(snap, "yes_ask", None) if snap else None,
                                    "spread": getattr(snap, "spread", None) if snap else None,
                                    "book_ok_at_entry": book_ok_at_entry,
                                    "yes_token_id": yes_tid,
                                }
                                # Persist copy-trading origin in trades.strategy_id for analytics.
                                # Existing flow keeps profile_15m for non-copy entries.
                                order_source = str(order.get("source") or "").strip().lower()
                                trade_strategy_id = "copy" if order_source == "copy" else profile_15m
                                trade_id = log_trade_open(position_state, llm_decision, market_diag, strategy_id=trade_strategy_id)
                                if trade_id is not None:
                                    trader.positions[mid]["trade_id"] = trade_id
                                # Remember source on position for downstream telemetry (e.g., Telegram, analytics).
                                if order_source:
                                    trader.positions[mid]["source"] = order_source
                                ttl = _market_display_title(mid, datafeed)
                                if ttl:
                                    trader.positions[mid]["market_title"] = ttl
                            save_state(_root, trader.balance, trader.positions, cumulative_realized_pnl)
                            logger.info("LLM slot: %d approved, executed", len(approved_orders))
                            if send_telegram_message:
                                for order in approved_orders:
                                    oid = order.get("market_id") or ""
                                    mid_preview = (oid[:24] + "..") if len(oid) > 24 else oid
                                    spent_usd = ex_cost_by_mid.get(oid)
                                    if spent_usd is None or spent_usd <= 0:
                                        spent_usd = float(order.get("final_size_usd") or order.get("target_size_usd") or 0)
                                    price = float(order.get("limit_price") or 0)
                                    try:
                                        ev_f = float(order.get("expected_ev") or 0)
                                    except (TypeError, ValueError):
                                        ev_f = 0.0
                                    edge_vs_half_pct = max(0.0, 0.5 - price) * 100.0
                                    market_label = ""
                                    cat_label = ""
                                    book_line = ""
                                    if oid and oid in datafeed.markets:
                                        snap = datafeed.markets[oid]
                                        if getattr(snap, "market_id", None) == oid:
                                            short = (getattr(snap, "group_item_title", "") or "").strip()
                                            long_q = (getattr(snap, "question", "") or "").strip()
                                            market_label = (short or long_q)[:80]
                                        else:
                                            market_label = (getattr(snap, "question", "") or "")[:80]
                                        cat_label = " [" + _market_category(oid, datafeed).capitalize() + "]"
                                        b = getattr(snap, "yes_bid", None)
                                        a = getattr(snap, "yes_ask", None)
                                        try:
                                            if b is not None and a is not None:
                                                book_line = f"Book: bid={float(b):.3f} ask={float(a):.3f} spr={(float(a)-float(b)):.3f}\n"
                                        except (TypeError, ValueError):
                                            book_line = ""
                            is_copy = str(order.get("source") or "").strip().lower() == "copy"
                            copy_tag = " [COPY]" if is_copy else ""
                            await send_telegram_message(
                                f"ClawBot v2 LIVE{cat_label}{copy_tag}\n"
                                f"BUY {mid_preview} ${spent_usd:,.0f} @{price:.3f}\n"
                                + (f"Market: {market_label}\n" if market_label else "")
                                + book_line
                                + f"LLM EV (заявл.): {ev_f * 100:.1f}%\n"
                                + f"0.5-entry: {edge_vs_half_pct:.1f}% (эвристика, не PnL)"
                            )
                        else:
                            logger.info("LLM slot: %d signals, 0 approved", len(signals))

                        n_decisions = len(signals)
                        n_trades_slot = len(approved_orders) if approved_orders else 0
                        sizes = [float(o.get("final_size_usd") or o.get("target_size_usd") or 0) for o in approved_orders] if approved_orders else []
                        avg_size_slot = (sum(sizes) / len(sizes)) if sizes else 0.0
                        exp_logger.log_interval(
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            n_markets=len(candidates),
                            n_decisions=n_decisions,
                            n_trades=n_trades_slot,
                            n_skipped=max(0, n_decisions - n_trades_slot),
                            avg_spread=avg_spread_from_candidates(candidates),
                            median_tte_sec=median_tte_sec_from_candidates(candidates),
                            avg_size=avg_size_slot if avg_size_slot > 0 else None,
                            pnl_interval=cumulative_realized_pnl - pnl_before_interval,
                            median_clob_vol_24h=median_clob_vol_from_candidates(candidates),
                            cumulative_pnl_after=cumulative_realized_pnl,
                        )
                        if approved_orders:
                            exp_logger.add_markets_traded([o.get("market_id") for o in approved_orders if o.get("market_id")])

                if elapsed > 0 and elapsed % telegram_sec == 0:
                    if send_telegram_message:
                        total_val = trader.balance + sum(
                            float(p.get("size_tokens", 0)) * prices.get(mid, {}).get("yes_price", 0)
                            for mid, p in trader.positions.items()
                        )
                        await send_telegram_message(
                            f"ClawBot v2 Report\nBalance: ${trader.balance:,.0f} | Positions: {len(trader.positions)}\n"
                            f"Total value: ${total_val:,.0f} | Realized PnL: ${cumulative_realized_pnl:,.2f}"
                        )

                await asyncio.sleep(loop_sec)
                elapsed += loop_sec
        except BaseException as shutdown_exc:
            # Диагностика: бесконечный цикл сам по себе не завершается — выход = исключение, отмена или Ctrl+C.
            if isinstance(shutdown_exc, KeyboardInterrupt):
                logger.warning(
                    "main_loop: остановка по KeyboardInterrupt (Ctrl+C / закрытие окна консоли / SIGINT)"
                )
            elif isinstance(shutdown_exc, asyncio.CancelledError):
                logger.warning(
                    "main_loop: asyncio.CancelledError — отмена задачи (завершение процесса, task.cancel, asyncio.run shutdown)"
                )
            elif isinstance(shutdown_exc, SystemExit):
                logger.warning("main_loop: SystemExit — code=%s", getattr(shutdown_exc, "code", shutdown_exc))
            else:
                logger.exception("main_loop: неперехваченное исключение — цикл прерван")
            raise
        finally:
            await stream.stop()
            strategy_params = cfg.get("strategy_params_15m_conservative") if profile_15m == "15m_conservative" else cfg.get("strategy_params_15m_aggressive")
            exp_logger.finish_session(trader, cumulative_realized_pnl, strategy_params=strategy_params)
            logger.info("PriceStream stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Штатное завершение (Ctrl+C / shutdown). Не печатать traceback пользователю.
        pass
