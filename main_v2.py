#!/usr/bin/env python3
"""
ClawBot v2.0 — цикл 60 сек, цены из Price Stream (REST/WebSocket), LLM раз в 5 мин, Telegram раз в час.
Запуск: python main_v2.py
"""

import asyncio
import logging
import os
import sys
import time

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
from datafeed import ClawBotDataFeed
from strategy import ClawBotStrategy
from riskmanager import RiskManager
from price_stream import PriceStream
from portfolio_state import load_state, save_state
from paper_trader import PaperTrader
from position_prices import get_position_prices_by_market
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

_log_file = os.path.join(_root, "clawbot_v2_run.log")
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


def check_stops(trader, prices: dict, cfg: dict) -> list:
    """По текущим ценам из stream: вернуть список {market_id, sell_price, reason} для закрытия."""
    to_close = []
    sl_pct = cfg.get("sl_pct", 0.04)
    tp_pct = cfg.get("tp_pct", 0.18)
    for mid, pos in list(trader.positions.items()):
        avg_price = float(pos.get("avg_price", 0))
        if avg_price <= 0:
            continue
        info = prices.get(mid, {})
        current_price = float(info.get("yes_price", 0))
        if current_price <= 0:
            continue
        sl = pos.get("stop_loss_price")
        tp = pos.get("take_profit_price")
        if sl is None or tp is None:
            sl = max(0.01, avg_price * (1 - sl_pct))
            tp = min(0.99, avg_price * (1 + tp_pct))
        else:
            sl, tp = float(sl), float(tp)
        if current_price <= sl:
            to_close.append({"market_id": mid, "sell_price": current_price, "reason": "SL"})
        elif current_price >= tp:
            to_close.append({"market_id": mid, "sell_price": current_price, "reason": "TP"})
    return to_close


async def main_loop() -> None:
    cfg = PROD_CONFIG
    level_name = cfg.get("log_level", "INFO").upper()
    logging.getLogger().setLevel(getattr(logging, level_name, logging.INFO))
    mode = (cfg.get("trading_mode") or "paper").strip().lower()
    logger.info("ClawBot v2.0: 60s loop, price stream, LLM every 5 min [%s]", mode.upper())

    saved_balance, saved_positions, cumulative_realized_pnl = load_state(_root)
    trader = get_trader(cfg)
    if saved_balance is not None and saved_positions is not None:
        trader.balance = saved_balance
        trader.positions = saved_positions
        logger.info("Restored portfolio: balance=%.2f, positions=%d", trader.balance, len(trader.positions))
    sl_cooldown = load_cooldown(_root)
    tp_cooldown = load_tp_cooldown(_root)

    stream = PriceStream()
    loop_sec = LOOP_INTERVAL_SEC
    llm_sec = LLM_INTERVAL_SEC
    telegram_sec = TELEGRAM_INTERVAL_SEC
    price_interval = PRICE_STREAM_INTERVAL_SEC
    elapsed = 0

    async with ClawBotDataFeed() as datafeed:
        category = cfg.get("markets_category", "both")
        active = [c for c in ACTIVE_CATEGORIES if c in MARKET_CATEGORIES]

        async def fetch_all_categories():
            all_markets = []
            for cat in active:
                if cat == "crypto":
                    markets_c = await datafeed.fetch_crypto_markets(
                        min_volume_usd=cfg.get("crypto_min_volume", 50_000),
                        min_liquidity_usd=cfg.get("crypto_min_liquidity", 0),
                        max_hours_to_resolution=cfg.get("crypto_resolution_hours_max", 12),
                    )
                    all_markets.extend(markets_c)
                else:
                    params = MARKET_CATEGORIES[cat]
                    min_vol = params.get("min_volume_usd", 100_000)
                    markets_cat = await datafeed.fetch_category_markets(
                        params.get("gamma_category", cat),
                        min_volume_usd=min_vol,
                    )
                    all_markets.extend(markets_cat)
            # Только активные категории: заменить пул, не смешивать со sports/culture
            datafeed.markets.clear()
            datafeed.markets.update({m.market_id: m for m in all_markets})
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
            return datafeed.markets

        await stream.start_rest(get_prices, market_ids, interval_sec=price_interval)

        try:
            while True:
                prices = stream.snapshot()
                # CLOB book: цены по открытым позициям каждые 10s (mid = (bid+ask)/2)
                if trader.positions:
                    token_ids_by_market = {}
                    for mid, pos in trader.positions.items():
                        tid = pos.get("yes_token_id")
                        if not tid and mid in datafeed.markets:
                            cids = getattr(datafeed.markets[mid], "clob_token_ids", None)
                            if cids and len(cids) >= 1:
                                tid = cids[0]
                        if tid:
                            token_ids_by_market[mid] = tid
                    if token_ids_by_market:
                        pos_prices = await get_position_prices_by_market(
                            list(trader.positions.keys()), token_ids_by_market
                        )
                        for mid, p in pos_prices.items():
                            prices[mid] = {"yes_price": p, "timestamp": time.time()}
                sl_cooldown = tick_cooldown(sl_cooldown)
                save_cooldown(_root, sl_cooldown)
                tp_cooldown = tick_cooldown(tp_cooldown)
                save_tp_cooldown(_root, tp_cooldown)

                to_close = check_stops(trader, prices, cfg)
                if to_close:
                    close_result = trader.close_positions(to_close)
                    closed = close_result.get("closed", [])
                    for c in closed:
                        cumulative_realized_pnl += float(c.get("pnl_usd", 0))
                    save_state(_root, trader.balance, trader.positions, cumulative_realized_pnl)
                    sl_closed = [c["market_id"] for c in closed if c.get("reason") == "SL"]
                    tp_closed = [c["market_id"] for c in closed if c.get("reason") == "TP"]
                    if sl_closed:
                        sl_cooldown = add_to_cooldown(sl_cooldown, sl_closed, runs=cfg.get("sl_cooldown_runs", DEFAULT_RUNS))
                        save_cooldown(_root, sl_cooldown)
                    if tp_closed:
                        tp_cooldown = add_to_cooldown(tp_cooldown, tp_closed, runs=cfg.get("tp_cooldown_runs", DEFAULT_TP_RUNS))
                        save_tp_cooldown(_root, tp_cooldown)
                    for c in closed:
                        logger.info("%s closed %s PnL $%.2f", c.get("reason"), c["market_id"][:12], c.get("pnl_usd", 0))
                    if send_telegram_message:
                        pnl_sum = sum(c.get("pnl_usd", 0) for c in closed)
                        await send_telegram_message(f"ClawBot v2 Exit\nClosed {len(closed)} | PnL ${pnl_sum:,.2f}")

                if elapsed > 0 and elapsed % llm_sec == 0:
                    # --- LLM Slot: 1) кандидаты по пригодности (YES в диапазоне), потом топ по спреду 2) strategy 3) risk 4) execute ---
                    await get_prices()  # свежие данные в datafeed
                    open_market_ids = set(trader.positions.keys())
                    sl_cooldown_ids = get_cooldown_set(sl_cooldown)
                    tp_cooldown_ids = get_cooldown_set(tp_cooldown)
                    exclude_ids = open_market_ids | sl_cooldown_ids | tp_cooldown_ids
                    logger.info("*** LLM SLOT STARTED exclude=%d ***", len(exclude_ids))
                    candidates = datafeed.get_tradeable_top(
                        cfg.get("n_markets", 25),
                        cfg["max_entry_price"],
                        cfg["min_entry_price"],
                        exclude_ids,
                    )

                    if len(candidates) == 0:
                        logger.warning("0 candidates — skip LLM")
                    else:
                        diag = datafeed.tradeable_diagnostic(
                            cfg["max_entry_price"], cfg["min_entry_price"], exclude_ids
                        )
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
                        for s in signals:
                            s["category"] = _market_category(s.get("market_id"), datafeed)
                        risk_mgr = RiskManager()
                        risk_mgr.config["max_single_market_pct"] = cfg["risk_per_trade"]
                        risk_mgr.config["max_category_pct"] = cfg["max_category_pct"]
                        risk_mgr.config["max_exposure_pct"] = cfg["max_exposure_pct"]
                        risk_mgr.config["min_reward_risk_ratio"] = cfg.get("min_reward_risk_ratio", 1.5)
                        risk_mgr.config["high_entry_ratio_exempt"] = cfg.get("high_entry_ratio_exempt", 0.95)
                        risk_mgr.config["max_entry_price"] = cfg.get("max_entry_price", 0.90)
                        risk_mgr.config["sl_pct"] = cfg.get("sl_pct", 0.04)
                        risk_mgr.config["tp_pct"] = cfg.get("tp_pct", 0.18)
                        risk_mgr.portfolio.balance_usd = trader.balance
                        risk_mgr.portfolio.positions = {
                            mid: float(p.get("size_tokens", 0)) * float(p.get("avg_price", 0))
                            for mid, p in trader.positions.items()
                        }
                        for mid, size in risk_mgr.portfolio.positions.items():
                            cat = _market_category(mid, datafeed)
                            risk_mgr.portfolio.exposure_by_category[cat] = risk_mgr.portfolio.exposure_by_category.get(cat, 0) + size
                        risk_result = risk_mgr.process_signals(signals)
                        approved_orders = risk_result["approved_orders"]
                        if risk_result.get("rejected_signals"):
                            for i, reason in enumerate(risk_result["rejected_signals"]):
                                logger.warning("Risk rejected signal %d: %s", i + 1, reason)
                        pct_vol = cfg.get("max_trade_pct_of_volume", 0.05)
                        pct_depth = cfg.get("max_trade_pct_of_depth", 0.10)
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
                                cids = getattr(datafeed.markets[mid], "clob_token_ids", None)
                                if cids and len(cids) >= 1:
                                    order["yes_token_id"] = cids[0]
                        if approved_orders:
                            execution_result = trader.execute_orders(approved_orders)
                            save_state(_root, trader.balance, trader.positions, cumulative_realized_pnl)
                            logger.info("LLM slot: %d approved, executed", len(approved_orders))
                            if send_telegram_message:
                                for order in approved_orders:
                                    mid = order.get("market_id", "")[:16]
                                    size_k = (order.get("final_size_usd") or order.get("target_size_usd") or 0) / 1000
                                    price = order.get("limit_price", 0)
                                    ev = order.get("expected_ev", 0)
                                    q = ""
                                    cat_label = ""
                                    if order.get("market_id"):
                                        oid = order["market_id"]
                                        if oid in datafeed.markets:
                                            q = (getattr(datafeed.markets[oid], "question", "") or "")[:80]
                                        cat_label = " [" + _market_category(oid, datafeed).capitalize() + "]"
                                    await send_telegram_message(
                                        f"ClawBot v2 LIVE{cat_label}\nBUY {mid}... ${size_k:.0f}k @{price:.3f}\n"
                                        + (f"Market: {q}\n" if q else "") + f"EV: {ev*100:.1f}%"
                                    )
                        else:
                            logger.info("LLM slot: %d signals, 0 approved", len(signals))

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
        finally:
            await stream.stop()
            logger.info("PriceStream stopped")


if __name__ == "__main__":
    asyncio.run(main_loop())
