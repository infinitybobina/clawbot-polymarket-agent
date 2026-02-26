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

    logger.info("ClawBot v1.1: FULL SIMULATION (Paper Trading)")
    logger.info("CWD: %s | .env from: %s", os.getcwd(), os.path.join(_root, ".env"))
    logger.info("TELEGRAM_TOKEN set: %s | TELEGRAM_CHAT_ID set: %s", bool(os.getenv("TELEGRAM_TOKEN")), bool(os.getenv("TELEGRAM_CHAT_ID")))
    cfg = PROD_CONFIG

    # Blocks 1+2+3 с параметрами из config
    async with ClawBotDataFeed() as datafeed:
        markets = await datafeed.fetch_politics_markets()
        top_candidates = datafeed.get_top_mispricing(cfg["n_markets"])
        logger.info(f"Fetched {len(markets)} markets, top {len(top_candidates)} candidates")

        # Состояние портфеля между запусками — чтобы не слать один и тот же сигнал каждый час
        saved_balance, saved_positions = load_state(_root)
        initial_balance = cfg.get("initial_balance", 100_000)
        trader = PaperTrader(initial_balance=initial_balance)
        if saved_balance is not None and saved_positions is not None:
            trader.balance = saved_balance
            trader.positions = saved_positions
            logger.info("Restored portfolio: balance=%.2f, positions=%d", trader.balance, len(trader.positions))

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
            save_state(_root, trader.balance, trader.positions)
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

        # Не предлагать рынки, по которым уже есть позиция или действует cooldown (SL/TP)
        open_market_ids = set(trader.positions.keys())
        sl_cooldown_ids = get_cooldown_set(sl_cooldown)
        tp_cooldown_ids = get_cooldown_set(tp_cooldown)
        exclude_ids = open_market_ids | sl_cooldown_ids | tp_cooldown_ids
        if exclude_ids:
            top_candidates = [m for m in top_candidates if m.market_id not in exclude_ids]
            logger.info("Excluded %d held + %d SL + %d TP cooldown, candidates left: %d", len(open_market_ids), len(sl_cooldown_ids), len(tp_cooldown_ids), len(top_candidates))

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

        risk_mgr = RiskManager()
        risk_mgr.config["max_single_market_pct"] = cfg["risk_per_trade"]
        risk_mgr.config["max_category_pct"] = cfg["max_category_pct"]
        risk_mgr.config["max_exposure_pct"] = cfg["max_exposure_pct"]
        risk_mgr.config["min_reward_risk_ratio"] = cfg.get("min_reward_risk_ratio", 1.5)
        risk_mgr.config["high_entry_ratio_exempt"] = cfg.get("high_entry_ratio_exempt", 0.95)
        risk_mgr.config["sl_pct"] = cfg.get("sl_pct", 0.07)
        risk_mgr.config["tp_pct"] = cfg.get("tp_pct", 0.18)
        risk_mgr.portfolio.balance_usd = trader.balance
        risk_mgr.portfolio.positions = {
            mid: float(p.get("size_tokens", 0)) * float(p.get("avg_price", 0))
            for mid, p in trader.positions.items()
        }
        risk_mgr.portfolio.exposure_by_category["US-current-affairs"] = sum(risk_mgr.portfolio.positions.values())
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
        for order in approved_orders:
            logger.info(f"Risk approved: BUY {order.get('market_id', '')[:12]}... ${order.get('final_size_usd', order.get('target_size_usd', 0)):,.0f}")

        # Block 4: Paper Trading SIMULATION
        execution_result = trader.execute_orders(approved_orders)
        save_state(_root, trader.balance, trader.positions)
        
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
                    mid = order.get("market_id", "")[:16]
                    side = order.get("outcome", "YES")
                    size_k = (order.get("final_size_usd") or order.get("target_size_usd") or 0) / 1000
                    price = order.get("limit_price", 0)
                    ev = order.get("expected_ev", 0)
                    live_msg = (
                        "CLAWBOT LIVE\n"
                        f"BUY {mid}@{side} ${size_k:.0f}k @{price:.3f}\n"
                        f"EV: {ev*100:.1f}% | Exposure: {open_exposure_pct:.0f}/{cfg['max_exposure_pct']*100:.0f}%"
                    )
                    await send_telegram_message(live_msg)
            realized_pnl = sum(float(c.get("pnl_usd", 0)) for c in trader.closed_trades)
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
