#!/usr/bin/env python3
"""
Сброс состояния бота в исходное: депозит 100 000$, позиций нет, PnL 0, кулдауны пустые.
Запуск: python reset_state.py
После сброса следующий запуск main_v2.py начнёт с чистого листа.
"""

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
from sl_cooldown import save_cooldown, save_tp_cooldown


def main():
    initial_balance = float(PROD_CONFIG.get("initial_balance", 100_000))
    save_state(_root, initial_balance, {}, 0.0)
    save_cooldown(_root, {})
    save_tp_cooldown(_root, {})
    print(
        f"Reset done: balance=${initial_balance:,.0f}, positions=0, cumulative_realized_pnl=0, "
        "sl_cooldown=0, tp_cooldown=0. Next run starts from a clean slate."
    )


if __name__ == "__main__":
    main()
