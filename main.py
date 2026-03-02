#!/usr/bin/env python3
"""
ClawBot v1.1 - FULL Paper Trading Pipeline
Data → Strategy → Risk → Paper Trader (SIMULATION)
"""

import asyncio
import json
import logging
import os
import sys

# .env — из папки, где лежит main.py (иначе при запуске из Планировщика задач cwd может быть другой)
_root = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_root, ".env"))
except ImportError:
    pass

from config import PROD_CONFIG
from datafeed import ClawBotDataFeed
from strategy import ClawBotStrategy
from riskmanager import RiskManager
from paper_trader import PaperTrader
from portfolio_state import load_state, save_state
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
try:
    from telegram_handler import send_telegram
except ImportError:
    send_telegram = None

# Лог в файл (при автоматическом запуске из Планировщика консоль недоступна — смотрите clawbot_run.log)
_log_file = os.path.join(_root, "clawbot_run.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_log_file, mode="a", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)
logger.info("Log file: %s", _log_file)

async def main():
    # На Windows консоль часто в cp1251/cp866 — эмодзи могут падать с UnicodeEncodeError
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    cfg = PROD_CONFIG
    level_name = cfg.get("log_level", "INFO").upper()
    logging.getLogger().setLevel(getattr(logging, level_name, logging.INFO))
    logger.info("ClawBot v1.1: FULL SIMULATION (Paper Trading)")
    logger.info("CWD: %s | .env from: %s", os.getcwd(), os.path.join(_root, ".env"))
    logger.info("TELEGRAM_TOKEN set: %s | TELEGRAM_CHAT_ID set: %s", bool(os.getenv("TELEGRAM_TOKEN")), bool(os.getenv("TELEGRAM_CHAT_ID")))

    # Blocks 1+2+3 с параметрами из config
    async with ClawBotDataFeed() as datafeed:
        category = cfg.get("markets_category", "politics")
        markets = []
        if category == "crypto":
            min_vol = cfg.get("crypto_min_volume", 10_000)
            min_liq = cfg.get("crypto_min_liquidity", 1_000)
            max_hours = cfg.get("crypto_resolution_hours_max", 1.0)
            markets = await datafeed.fetch_crypto_markets(
                min_volume_usd=min_vol,
                min_liquidity_usd=min_liq,
                max_hours_to_resolution=max_hours,
            )
        elif category == "both":
            min_vol_p = cfg.get("min_volume", 500_000)
            markets_p = await datafeed.fetch_politics_markets(min_volume_usd=min_vol_p)
            min_vol_c = cfg.get("crypto_min_volume", 10_000)
            min_liq = cfg.get("crypto_min_liquidity", 0)
            max_hours = cfg.get("crypto_resolution_hours_max", 1.0)
            markets_c = await datafeed.fetch_crypto_markets(
                min_volume_usd=min_vol_c,
                min_liquidity_usd=min_liq,
                max_hours_to_resolution=max_hours,
            )
            markets = markets_p + markets_c
            logger.info("Fetched %d politics + %d crypto, total %d markets", len(markets_p), len(markets_c), len(markets))
        else:
            min_vol = cfg.get("min_volume", 500_000)
            markets = await datafeed.fetch_politics_markets(min_volume_usd=min_vol)
        if category != "both":
            logger.info("Fetched %d %s markets", len(markets), category)
        else:
            logger.info("Fetched %d politics + %d crypto, total %d markets", len(markets_p), len(markets_c), len(markets))

        # Состояние портфеля между запусками — чтобы не слать один и тот же сигнал каждый час
        saved_balance, saved_positions, cumulative_realized_pnl = load_state(_root)
        trader = PaperTrader(cfg)
        if saved_balance is not None and saved_positions is not None:
            trader.balance = saved_balance
            trader.positions = saved_positions
            logger.info("Restored portfolio: balance=%.2f, positions=%d, realized_pnl=%.2f", trader.balance, len(trader.positions), cumulative_realized_pnl)

        # Cooldown после SL и TP: каждый запуск уменьшаем счётчик
        sl_cooldown = load_cooldown(_root)
        sl_cooldown = tick_cooldown(sl_cooldown)
        save_cooldown(_root, sl_cooldown)
        tp_cooldown = load_tp_cooldown(_root)
        tp_cooldown = tick_cooldown(tp_cooldown)
        save_tp_cooldown(_root, tp_cooldown)

        # Проверка SL/TP по текущей цене: price <= SL → закрыть (StopLoss), price >= TP → закрыть (TakeProfit)
        sl_pct = cfg.get("sl_pct", 0.07)
        tp_pct = cfg.get("tp_pct", 0.18)
        to_close = []
        closed_by_exit = []
        for mid, pos in list(trader.positions.items()):
            if mid not in datafeed.markets:
                continue
            avg_price = float(pos.get("avg_price", 0))
            current_price = datafeed.markets[mid].yes_price
            if avg_price <= 0:
                continue
            sl = pos.get("stop_loss_price")
            tp = pos.get("take_profit_price")
            if sl is None or tp is None:
                sl = max(0.01, avg_price * (1 - sl_pct))
                tp = min(0.99, avg_price * (1 + tp_pct))
            else:
                sl = float(sl)
                tp = float(tp)
            if current_price <= sl:
                to_close.append({"market_id": mid, "sell_price": current_price, "reason": "SL"})
            elif current_price >= tp:
                to_close.append({"market_id": mid, "sell_price": current_price, "reason": "TP"})
        if to_close:
            close_result = trader.close_positions(to_close)
            closed_by_exit = close_result.get("closed", [])
            cumulative_realized_pnl += sum(float(c.get("pnl_usd", 0)) for c in closed_by_exit)
            save_state(_root, trader.balance, trader.positions, cumulative_realized_pnl)
            sl_closed = [c["market_id"] for c in closed_by_exit if c.get("reason") == "SL"]
            tp_closed = [c["market_id"] for c in closed_by_exit if c.get("reason") == "TP"]
            if sl_closed:
                sl_cooldown = add_to_cooldown(sl_cooldown, sl_closed, runs=cfg.get("sl_cooldown_runs", DEFAULT_RUNS))
                save_cooldown(_root, sl_cooldown)
                logger.info("SL cooldown %d runs for: %s", cfg.get("sl_cooldown_runs", DEFAULT_RUNS), [m[:12] for m in sl_closed])
            if tp_closed:
                tp_cooldown = add_to_cooldown(tp_cooldown, tp_closed, runs=cfg.get("tp_cooldown_runs", DEFAULT_TP_RUNS))
                save_tp_cooldown(_root, tp_cooldown)
                logger.info("TP cooldown %d runs for: %s", cfg.get("tp_cooldown_runs", DEFAULT_TP_RUNS), [m[:12] for m in tp_closed])
            for c in closed_by_exit:
                logger.info("%s: closed %s, PnL $%.2f", c.get("reason", "exit"), c["market_id"][:12], c.get("pnl_usd", 0))

        # Кандидаты: сначала пригодность (YES в диапазоне), потом топ по спреду; исключаем held и cooldown
        open_market_ids = set(trader.positions.keys())
        sl_cooldown_ids = get_cooldown_set(sl_cooldown)
        tp_cooldown_ids = get_cooldown_set(tp_cooldown)
        exclude_ids = open_market_ids | sl_cooldown_ids | tp_cooldown_ids
        max_entry = cfg.get("max_entry_price", 0.90)
        min_yes_price = cfg.get("min_entry_price", 0.02)
        top_candidates = datafeed.get_tradeable_top(
            cfg["n_markets"],
            max_entry=max_entry,
            min_yes=min_yes_price,
            exclude_ids=exclude_ids,
        )
        tradeable_total = len([m for m in datafeed.markets.values() if min_yes_price <= m.yes_price < max_entry])
        logger.info("Tradeable pool %d (YES %.2f–%.2f), excluded %d held+cooldown, top %d candidates", tradeable_total, min_yes_price, max_entry, len(exclude_ids), len(top_candidates))

        strategy = ClawBotStrategy(
            use_llm=True,
            base_balance_usd=cfg.get("initial_balance", 100_000),
            min_ev_threshold=cfg.get("min_ev_threshold", 0.02),
            min_volume_usd=cfg["min_volume"],
            min_yes_edge=cfg["min_yes_edge"],
            sl_pct=cfg.get("sl_pct", 0.07),
            tp_pct=cfg.get("tp_pct", 0.18),
        )
        signals = strategy.generate_signals(top_candidates)
        logger.info(f"Generated {len(signals)} signals")
        # Категория по каждому сигналу (для риска и Telegram)
        for s in signals:
            mid = s.get("market_id")
            if mid and mid in datafeed.markets:
                cat = (datafeed.markets[mid].category or "").strip().lower()
                s["category"] = "crypto" if cat == "crypto" else "US-current-affairs"
            else:
                s["category"] = "US-current-affairs"

        risk_mgr = RiskManager()
        risk_mgr.config["max_single_market_pct"] = cfg["risk_per_trade"]
        risk_mgr.config["max_category_pct"] = cfg["max_category_pct"]
        risk_mgr.config["max_exposure_pct"] = cfg["max_exposure_pct"]
        risk_mgr.config["min_reward_risk_ratio"] = cfg.get("min_reward_risk_ratio", 1.5)
        risk_mgr.config["high_entry_ratio_exempt"] = cfg.get("high_entry_ratio_exempt", 0.95)
        risk_mgr.config["max_entry_price"] = cfg.get("max_entry_price", 0.90)
        risk_mgr.config["sl_pct"] = cfg.get("sl_pct", 0.07)
        risk_mgr.config["tp_pct"] = cfg.get("tp_pct", 0.18)
        risk_mgr.portfolio.balance_usd = trader.balance
        risk_mgr.portfolio.positions = {
            mid: float(p.get("size_tokens", 0)) * float(p.get("avg_price", 0))
            for mid, p in trader.positions.items()
        }
        # Экспозиция по категориям (для режима "both" — лимит на каждую категорию отдельно)
        for mid, size in risk_mgr.portfolio.positions.items():
            cat = "US-current-affairs"
            if mid in datafeed.markets:
                c = (datafeed.markets[mid].category or "").strip().lower()
                cat = "crypto" if c == "crypto" else "US-current-affairs"
            risk_mgr.portfolio.exposure_by_category[cat] = risk_mgr.portfolio.exposure_by_category.get(cat, 0) + size
        if signals:
            s0 = signals[0]
            logger.info(
                "Signal to risk: entry=%.4f sl=%.4f tp=%.4f target_size=%.0f",
                float(s0.get("limit_price") or 0),
                float(s0.get("stop_loss_price") or 0),
                float(s0.get("take_profit_price") or 1),
                float(s0.get("target_size_usd") or 0),
            )
        risk_result = risk_mgr.process_signals(signals)
        approved_orders = risk_result["approved_orders"]
        rejected = risk_result.get("rejected_signals", [])
        if rejected:
            for i, reason in enumerate(rejected):
                logger.warning("Risk rejected signal %d: %s", i + 1, reason)

        logger.info(f"Blocks 1-3: {len(approved_orders)} approved orders")
        # Лимит размера по объёму/глубине рынка: политика и крипто (на малых рынках не двигать цену)
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
                    cat = (getattr(m, "category", None) or "").strip().lower()
                    logger.info("Volume cap %s: %s size %.0f -> %.0f (vol=%.0f liq=%.0f)", cat or "market", mid[:12], current, order["final_size_usd"], m.volume_usd, getattr(m, "liquidity_usd", 0))
        for order in approved_orders:
            logger.info(f"Risk approved: BUY {order.get('market_id', '')[:12]}... ${order.get('final_size_usd', order.get('target_size_usd', 0)):,.0f}")

        # Нормализация limit_price: если 0 или не задан — брать yes_price из datafeed, иначе не исполнять мусор
        for order in approved_orders:
            mid = order.get("market_id")
            lp = float(order.get("limit_price") or 0)
            if lp <= 0.02 and mid and mid in datafeed.markets:
                order["limit_price"] = max(0.02, float(datafeed.markets[mid].yes_price))
                logger.info("Order %s: limit_price подставлен из datafeed: %.4f", mid[:12], order["limit_price"])
            elif lp <= 0.02:
                order["limit_price"] = 0.50
                logger.warning("Order %s: limit_price был %.4f, подставлен 0.50", mid[:12] if mid else "?", lp)

        # Block 4: Paper Trading SIMULATION
        execution_result = trader.execute_orders(approved_orders)
        save_state(_root, trader.balance, trader.positions, cumulative_realized_pnl)
        
        logger.info("Portfolio after trades:")
        for metric, value in execution_result["portfolio"].items():
            print(f"  {metric}: {value}")
        
        # Симуляция движения рынка (5 минут)
        await asyncio.sleep(1)  # пауза для вида
        logger.info("Simulating market moves...")
        for market_id in trader.positions.keys():
            trader.simulate_market_move(market_id, 0.45)  # цена выросла

        # Переоценка позиций по текущим ценам API — Total Value отражает реальную стоимость
        mark_to_market = {
            mid: datafeed.markets[mid].yes_price
            for mid in trader.positions
            if mid in datafeed.markets
        }
        final_metrics = trader.get_portfolio_metrics(mark_to_market_prices=mark_to_market if mark_to_market else None)
        print("\nFINAL RESULTS:")
        print(json.dumps(final_metrics, indent=2))

        # Block 5: Telegram — алерт в формате LIVE TRADE
        total_return_pct = final_metrics.get("total_return_pct", 0)
        open_exposure_pct = (final_metrics.get("open_exposure_usd", 0) / cfg.get("initial_balance", 100_000)) * 100
        if not send_telegram_message:
            logger.warning("Telegram не отправлен: модуль telegram_notify не загружен")
        elif not (os.getenv("TELEGRAM_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")):
            logger.warning("Telegram не отправлен: в .env нет TELEGRAM_TOKEN или TELEGRAM_CHAT_ID (проверьте путь к .env при запуске из Планировщика)")
        if send_telegram_message:
            if closed_by_exit:
                pnl_sum = sum(c.get("pnl_usd", 0) for c in closed_by_exit)
                by_reason = {}
                for c in closed_by_exit:
                    r = c.get("reason", "exit")
                    by_reason[r] = by_reason.get(r, 0) + 1
                parts = [f"{n} {r}" for r, n in sorted(by_reason.items())]
                exit_msg = (
                    "CLAWBOT Exit (SL/TP)\n"
                    f"Closed {len(closed_by_exit)} ({', '.join(parts)}) | PnL: ${pnl_sum:,.2f}"
                )
                await send_telegram_message(exit_msg)
            if approved_orders:
                for i, order in enumerate(approved_orders, 1):
                    order_mid = order.get("market_id", "")
                    mid = order_mid[:16]
                    side = order.get("outcome", "YES")
                    size_k = (order.get("final_size_usd") or order.get("target_size_usd") or 0) / 1000
                    price = order.get("limit_price", 0)
                    ev = order.get("expected_ev", 0)
                    market_label = ""
                    if order_mid and order_mid in datafeed.markets:
                        q = getattr(datafeed.markets[order_mid], "question", "") or ""
                        market_label = (q[:120] + "…") if len(q) > 120 else q
                    live_msg = (
                        "CLAWBOT LIVE\n"
                        f"BUY {mid}@{side} ${size_k:.0f}k @{price:.3f}\n"
                        + (f"Market: {market_label}\n" if market_label else "")
                        + f"Category: {category} | EV: {ev*100:.1f}% | Exposure: {open_exposure_pct:.0f}/{cfg['max_exposure_pct']*100:.0f}%"
                    )
                    await send_telegram_message(live_msg)
            realized_pnl = cumulative_realized_pnl  # кумулятивный реализованный PnL (сохраняется между запусками)
            unrealized_pnl = final_metrics.get("unrealized_pnl", 0)
            summary = (
                f"ClawBot Report\n"
                f"Signals: {len(signals)} | Approved: {len(approved_orders)}\n"
                f"Total value: ${final_metrics.get('total_value', 0):,.0f} | Return: {total_return_pct:.2f}%\n"
                f"Realized PnL: ${realized_pnl:,.2f} | Unrealized PnL: ${unrealized_pnl:,.2f}"
            )
            await send_telegram_message(summary)

if __name__ == "__main__":
    asyncio.run(main())
