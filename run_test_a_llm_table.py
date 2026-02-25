#!/usr/bin/env python3
"""
Запуск теста A с LLM, вывод сводной таблицы.
Требует OPENAI_API_KEY в .env.

  python run_test_a_llm_table.py         — полный прогон 30 и 90 дней (~4 ч, 2880 вызовов API)
  python run_test_a_llm_table.py --short — быстрый прогон 3 и 9 дней (~10–15 мин, 288 вызовов)
"""

import sys
import logging

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.WARNING)

from backtest import Backtester, TEST_PRESETS

def main():
    short = "--short" in sys.argv or "-s" in sys.argv
    no_llm = "--no-llm" in sys.argv  # быстрая таблица без API (простая стратегия)
    days_a, days_b = (3, 9) if short else (30, 90)
    preset = TEST_PRESETS["A"]
    use_llm = not no_llm

    print("Test A: EV=3%, Vol>$500k, Risk=3%")
    if no_llm:
        print("Режим --no-llm: простая стратегия (без вызовов API, секунды)")
    elif short:
        print("Режим --short: 3 и 9 дней с LLM (~10–15 мин)")
    else:
        print("С LLM: 30 и 90 дней (~4 ч)")
    print(f"Running {days_a} days...")
    bt1 = Backtester(
        days=days_a,
        n_markets=preset["n_markets"],
        min_ev_threshold=preset["min_ev_threshold"],
        min_volume_usd=preset["min_volume_usd"],
        max_single_market_pct=preset["max_single_market_pct"],
        use_llm=use_llm,
    )
    bt1.risk_mgr.config["max_category_pct"] = preset["max_category_pct"]
    bt1.risk_mgr.config["max_exposure_pct"] = preset["max_exposure_pct"]
    r1 = bt1.run()

    print(f"Running {days_b} days...")
    bt2 = Backtester(
        days=days_b,
        n_markets=preset["n_markets"],
        min_ev_threshold=preset["min_ev_threshold"],
        min_volume_usd=preset["min_volume_usd"],
        max_single_market_pct=preset["max_single_market_pct"],
        use_llm=use_llm,
    )
    bt2.risk_mgr.config["max_category_pct"] = preset["max_category_pct"]
    bt2.risk_mgr.config["max_exposure_pct"] = preset["max_exposure_pct"]
    r2 = bt2.run()

    col1, col2 = f"{days_a} дней", f"{days_b} дней"
    title = f"Тест A + LLM ({days_a} vs {days_b} дней)"
    lines = [
        "",
        "=" * 60,
        f"Сводная таблица: {title}",
        "=" * 60,
        f"{'Метрика':<22} | {col1:>12} | {col2:>12}",
        "-" * 60,
        f"{'Сделок (total_trades)':<22} | {r1['total_trades']:>12} | {r2['total_trades']:>12}",
        f"{'Sharpe':<22} | {r1['sharpe']:>12.4f} | {r2['sharpe']:>12.4f}",
        f"{'Winrate, %':<22} | {r1['winrate_pct']:>12.2f} | {r2['winrate_pct']:>12.2f}",
        f"{'Profit, %':<22} | {r1['profit_pct']:>12.2f} | {r2['profit_pct']:>12.2f}",
        f"{'Max Drawdown, %':<22} | {r1['max_drawdown_pct']:>12.2f} | {r2['max_drawdown_pct']:>12.2f}",
        f"{'Calmar':<22} | {r1['calmar']:>12.4f} | {r2['calmar']:>12.4f}",
        f"{'Финальный баланс':<22} | {r1['final_balance']:>12.2f} | {r2['final_balance']:>12.2f}",
        "=" * 60,
    ]
    text = "\n".join(lines)
    print(text)
    with open("backtest_test_a_llm_table.txt", "w", encoding="utf-8") as f:
        f.write("Test A: EV=3%, Vol>$500k, Risk=3%\n")
        f.write(("С LLM: нет" if no_llm else "С LLM: да") + (", --short (3 vs 9 дней)" if short else "") + "\n")
        f.write(text)
    print("\nТаблица сохранена в backtest_test_a_llm_table.txt")
    return 0

if __name__ == "__main__":
    sys.exit(main())
