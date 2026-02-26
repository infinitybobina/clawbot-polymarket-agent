#!/usr/bin/env python3
"""
Три теста с разным порогом импульса — подобраны так, чтобы видеть разные сделки и эффект фильтра.
  0.00 = строго: входим только если цена не упала за час
  0.02 = средний допуск (2% падения)
  0.05 = мягко: допуск 5% — больше входов
Сводная таблица: сделок, Sharpe, Winrate, Profit, Max Drawdown, Calmar, Финальный баланс.

  python run_three_momentum_tests.py       — 30 и 90 дней (~1–2 мин)
  python run_three_momentum_tests.py --quick — только 30 дней (~30 сек)
"""
import sys
import logging
logging.basicConfig(level=logging.WARNING)

from backtest import Backtester

# Разнесённые пороги, чтобы фильтр по-разному отсекал часы → разное кол-во и состав сделок
MOMENTUM_VALUES = [0.00, 0.02, 0.05]
METRICS = [
    ("total_trades", "Сделок", "{:>8}"),
    ("sharpe", "Sharpe", "{:>8.4f}"),
    ("winrate_pct", "Winrate %", "{:>8.2f}"),
    ("profit_pct", "Profit %", "{:>8.2f}"),
    ("max_drawdown_pct", "Max DD %", "{:>8.2f}"),
    ("calmar", "Calmar", "{:>8.4f}"),
    ("final_balance", "Фин. баланс", "{:>12.2f}"),
]

def run_tests(days: int):
    results = {}
    for i, mom in enumerate(MOMENTUM_VALUES, 1):
        print(f"  [{i}/3] momentum={mom:.2f} ... ", end="", flush=True)
        bt = Backtester(days=days, n_markets=12, min_yes_edge=0.08, momentum_threshold=mom)
        results[mom] = bt.run()
        print(f"сделок={results[mom]['total_trades']}", flush=True)
    return results

def build_table(results: dict, days: int) -> list:
    lines = [
        f"\n{'='*70}",
        f"  Сводная таблица: три теста по порогу импульса ({days} дней)",
        f"  Параметры: n_markets=12, min_yes_edge=0.08",
        f"{'='*70}",
        "",
    ]
    w0, w = 18, 14
    header = f"{'Метрика':<{w0}}" + "".join(f"| m={m:.2f}".rjust(w) for m in MOMENTUM_VALUES) + " |"
    sep = "-" * w0 + "|" + ("-" * w + "|") * len(MOMENTUM_VALUES)
    lines.append(header)
    lines.append(sep)
    for key, label, fmt in METRICS:
        row = f"{label:<{w0}}|"
        for m in MOMENTUM_VALUES:
            val = results[m][key]
            if key == "total_trades":
                row += f" {val:>{w-1}} |"
            else:
                row += f" {fmt.format(val):>{w-1}} |"
        lines.append(row)
    lines.append("=" * 70)
    return lines

def main():
    quick = "--quick" in sys.argv or "-q" in sys.argv
    print("Три теста: momentum 0.00, 0.02, 0.05 (n_markets=12, min_yes_edge=0.08)", flush=True)
    print("30 дней:", flush=True)
    r30 = run_tests(30)
    r90 = None
    if not quick:
        print("90 дней:", flush=True)
        r90 = run_tests(90)

    lines_30 = build_table(r30, 30)
    text = "\n".join(lines_30)
    if r90 is not None:
        lines_90 = build_table(r90, 90)
        text += "\n" + "\n".join(lines_90)
    print(text, flush=True)

    out_path = "backtest_three_momentum_table.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("Три теста: порог импульса 0.00, 0.02, 0.05 (разные сделки)\n")
        f.write("Параметры: n_markets=12, min_yes_edge=0.08\n\n")
        f.write(text)
    print(f"\nТаблица сохранена в {out_path}", flush=True)
    return 0

if __name__ == "__main__":
    exit(main())
