#!/usr/bin/env python3
"""
Live-исполнение через Polymarket CLOB API.
Интерфейс совместим с PaperTrader (execute_orders, close_positions, balance, positions).

Перед использованием реализовать вызовы CLOB (см. PRODUCTION_READY.md).
"""

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_PROD_READY_MSG = (
    "Live trading not implemented. Implement CLOB auth, execute_orders and close_positions. See PRODUCTION_READY.md"
)


class LiveTrader:
    """Трейдер с реальным исполнением на Polymarket CLOB.
    Класс-заглушка; любые попытки вызвать методы исполнения должны приводить к NotImplementedError
    до момента отдельного, явного релиза live-версии."""

    def __init__(self, cfg: dict):
        initial = float(cfg.get("initial_balance", 100_000))
        self.initial_balance = initial
        self.balance = initial
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.closed_trades: List[Dict[str, Any]] = []

    def execute_orders(self, approved_orders: List[Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        raise NotImplementedError(_PROD_READY_MSG)

    def close_positions(self, to_close: List[Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        raise NotImplementedError(_PROD_READY_MSG)
