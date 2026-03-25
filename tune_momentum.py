#!/usr/bin/env python3
"""
Перебор momentum_threshold и min_yes_edge: цель Winrate >56%%, Sharpe >2.

Порог импульса (momentum_threshold):
  0.00 = входим только если цена не упала за час (строго)
  0.01–0.03 = допускаем падение 1–3%% → часто Sharpe >2 при min_yes_edge 0.08
  0.05+ = мягче фильтр, больше сделок

Запуск с порогом из CLI: python backtest.py 30 A m0.02  (пресет A, momentum=0.02)
Без пресета: python backtest.py 30  или  python backtest.py 30 m0.01
"""
import logging
logging.basicConfig(level=logging.WARNING)

from backtest import Backtester

def main():
    print("momentum | min_yes | сделок | Sharpe | Winrate% | Profit%")
    print("-" * 60)
    best = None
    for thresh in [0.0, 0.01, 0.02, 0.03, 0.05]:
        for edge in [0.07, 0.08, 0.09]:
            bt = Backtester(days=30, n_markets=12, momentum_threshold=thresh, min_yes_edge=edge)
            r = bt.run()
            wr, sh = r["winrate_pct"], r["sharpe"]
            ok = " ***" if (wr > 56 and sh > 2) else ""
            print(f"  {thresh:.2f}    |  {edge:.2f}   | {r['total_trades']:>6} | {sh:>6.3f} | {wr:>7.2f}  | {r['profit_pct']:>6.2f}{ok}")
            if ok and (best is None or (wr > best[0] or sh > best[1])):
                best = (wr, sh, thresh, edge)
    print("\n*** = Winrate>56% и Sharpe>2")
    if best:
        print(f"Лучший: Winrate={best[0]:.1f}% Sharpe={best[1]:.3f} при momentum={best[2]}, min_yes_edge={best[3]}")

if __name__ == "__main__":
    main()
