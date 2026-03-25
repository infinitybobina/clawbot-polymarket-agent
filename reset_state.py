#!/usr/bin/env python3
"""
Сброс состояния бота в исходное: депозит из PROD_CONFIG, позиций нет, PnL 0, все кулдауны пустые.
Запуск: python reset_state.py
После сброса следующий запуск main_v2.py начнёт с чистого листа.

Не трогает: PostgreSQL (таблица trades), CSV experiment_logger — только локальные JSON.
"""

import argparse
import os
import sys

_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _root)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_root, ".env"))
except ImportError:
    pass

from config import PROD_CONFIG
from portfolio_state import save_state
from reentry_cooldown import save_reentry_cooldown
from sl_cooldown import save_cooldown, save_tp_cooldown

try:
    from bot_runtime import SESSION_FILENAME
except ImportError:
    SESSION_FILENAME = "bot_session.json"


def main():
    parser = argparse.ArgumentParser(description="Сброс portfolio_state + cooldown JSON.")
    parser.add_argument(
        "--experiments",
        action="store_true",
        help="Удалить results_*_intervals.csv, results_*_summary.csv, results_*_params.json, results_profiles_summary.csv",
    )
    args = parser.parse_args()

    initial_balance = float(PROD_CONFIG.get("initial_balance", 100_000))
    save_state(_root, initial_balance, {}, 0.0)
    save_cooldown(_root, {})
    save_tp_cooldown(_root, {})
    save_reentry_cooldown(_root, {})

    sess_path = os.path.join(_root, SESSION_FILENAME)
    if os.path.isfile(sess_path):
        try:
            os.remove(sess_path)
        except OSError as e:
            print(f"Warning: could not remove {SESSION_FILENAME}: {e}", file=sys.stderr)

    removed_exp = []
    if args.experiments:
        for name in os.listdir(_root):
            if not name.startswith("results_"):
                continue
            if name.endswith("_intervals.csv") or name.endswith("_summary.csv") or name.endswith("_params.json"):
                p = os.path.join(_root, name)
                try:
                    os.remove(p)
                    removed_exp.append(name)
                except OSError as e:
                    print(f"Warning: could not remove {name}: {e}", file=sys.stderr)
        prof_sum = os.path.join(_root, "results_profiles_summary.csv")
        if os.path.isfile(prof_sum):
            try:
                os.remove(prof_sum)
                removed_exp.append("results_profiles_summary.csv")
            except OSError as e:
                print(f"Warning: could not remove results_profiles_summary.csv: {e}", file=sys.stderr)

    print(
        f"Reset done: balance=${initial_balance:,.0f}, positions=0, cumulative_realized_pnl=0, "
        "sl/tp/reentry cooldown cleared, bot_session.json removed if present."
    )
    if removed_exp:
        print(f"Experiment CSV/JSON removed: {', '.join(removed_exp)}")


if __name__ == "__main__":
    main()
