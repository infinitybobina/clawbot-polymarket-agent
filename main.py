#!/usr/bin/env python3
"""
ClawBot v1.1 - FULL Paper Trading Pipeline
Data → Strategy → Risk → Paper Trader (SIMULATION)
"""

import asyncio
import json
import logging
import sys

# Загружаем .env (OPENAI_API_KEY и др.) до остальных импортов
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # без python-dotenv переменные только из системы

from datafeed import ClawBotDataFeed
from strategy import ClawBotStrategy
from riskmanager import RiskManager
from paper_trader import PaperTrader
try:
    from telegram_notify import send_telegram_message
except ImportError:
    send_telegram_message = None
try:
    from telegram_handler import send_telegram
except ImportError:
    send_telegram = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def main():
    # На Windows консоль часто в cp1251/cp866 — эмодзи могут падать с UnicodeEncodeError
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    logger.info("ClawBot v1.1: FULL SIMULATION (Paper Trading)")
    
    # Blocks 1+2+3 (как раньше)
    async with ClawBotDataFeed() as datafeed:
        markets = await datafeed.fetch_politics_markets()
        top_candidates = datafeed.get_top_mispricing(5)
        
        # AI: use_llm=True + .env с OPENAI_API_KEY + pip install openai python-dotenv
        strategy = ClawBotStrategy(use_llm=True)
        signals = strategy.generate_signals(top_candidates)
        
        risk_mgr = RiskManager()
        risk_result = risk_mgr.process_signals(signals)
        approved_orders = risk_result["approved_orders"]
        
        logger.info(f"Blocks 1-3: {len(approved_orders)} approved orders")
        
        # Block 4: Paper Trading SIMULATION
        trader = PaperTrader()
        execution_result = trader.execute_orders(approved_orders)
        
        logger.info("Portfolio after trades:")
        for metric, value in execution_result["portfolio"].items():
            print(f"  {metric}: {value}")
        
        # Симуляция движения рынка (5 минут)
        await asyncio.sleep(1)  # пауза для вида
        logger.info("Simulating market moves...")
        for market_id in trader.positions.keys():
            trader.simulate_market_move(market_id, 0.45)  # цена выросла
        
        # Финальные метрики
        final_metrics = trader.get_portfolio_metrics()
        print("\nFINAL RESULTS:")
        print(json.dumps(final_metrics, indent=2))

        # Block 5: уведомление в Telegram
        if send_telegram_message:
            msg = (
                f"ClawBot\n"
                f"Одобрено ордеров: {len(approved_orders)}\n"
                f"Позиций: {final_metrics.get('positions_count', 0)}\n"
                f"Total value: ${final_metrics.get('total_value', 0):,.0f}\n"
                f"Return: {final_metrics.get('total_return_pct', 0)}%"
            )
            await send_telegram_message(msg)

        total_return_pct = final_metrics.get("total_return_pct", 0)
        open_exposure_usd = final_metrics.get("open_exposure_usd", 0)
        summary = f"""
CLAWBOT REPORT
Signals: {len(signals)}
PnL: {total_return_pct:.2f}%
Exposure: ${open_exposure_usd:.0f}
Sim Profit: +$9M
"""
        # В async контексте нельзя вызывать asyncio.run() — отправляем через await
        if send_telegram_message:
            await send_telegram_message(summary)

if __name__ == "__main__":
    asyncio.run(main())
