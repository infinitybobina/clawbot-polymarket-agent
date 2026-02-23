#!/usr/bin/env python3
"""
Block 4: Paper Trading Simulator
Имитирует CLOB: исполняет ордера, обновляет PnL, показывает метрики.

Важно:
- Имя файла без дефиса (paper_trader.py), чтобы его можно было импортировать.
"""

import logging
import random
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


class PaperTrader:
    def __init__(self, initial_balance: float = 100000):
        self.initial_balance = float(initial_balance)
        self.balance = float(initial_balance)
        # market_id → {outcome, size_tokens, avg_price}
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.closed_trades: List[Dict[str, Any]] = []  # история PnL (на будущее)
        self.total_pnl = 0.0

    def execute_orders(self, approved_orders: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Имитирует исполнение ордеров на CLOB."""
        executions = []

        for order in approved_orders:
            market_id = order["market_id"]
            outcome = order.get("outcome", "YES")
            size_usd = float(order.get("final_size_usd") or order.get("approved_size_usd") or order.get("target_size_usd") or 0)
            limit_price = float(order.get("limit_price") or 0)

            if size_usd <= 0:
                continue

            # Защита: если лимитная цена 0/неадекватная — ставим минимальную,
            # чтобы не поймать деление на ноль в симуляции.
            safe_price = max(limit_price, 0.0001)

            # Симуляция: чаще всего fill около лимитной цены, иногда со слиппеджем
            if random.random() > 0.1:
                fill_price = safe_price * random.uniform(0.99, 1.01)
            else:
                fill_price = safe_price * random.uniform(1.00, 1.03)

            tokens_bought = size_usd / fill_price

            # Записываем / усредняем позицию
            if market_id not in self.positions:
                self.positions[market_id] = {"outcome": outcome, "size_tokens": 0.0, "avg_price": 0.0}

            pos = self.positions[market_id]
            prev_tokens = float(pos.get("size_tokens", 0.0))
            prev_avg = float(pos.get("avg_price", 0.0))
            new_tokens = prev_tokens + tokens_bought
            new_avg = (prev_tokens * prev_avg + tokens_bought * fill_price) / new_tokens if new_tokens > 0 else fill_price

            self.positions[market_id] = {"outcome": outcome, "size_tokens": new_tokens, "avg_price": new_avg}
            self.balance -= size_usd

            executions.append(
                {
                    "market_id": market_id,
                    "status": "FILLED",
                    "fill_price": round(fill_price, 4),
                    "tokens": round(tokens_bought, 2),
                    "cost_usd": round(size_usd, 2),
                }
            )

            logger.info(f"FILLED: {market_id[:10]}... {outcome} {tokens_bought:.2f} tokens @ {fill_price:.4f}")

        return {"executions": executions, "portfolio": self.get_portfolio_metrics()}

    def simulate_market_move(self, market_id: str, new_price: float) -> None:
        """Имитирует изменение цены (для теста PnL)."""
        if market_id not in self.positions:
            return
        pos = self.positions[market_id]
        unrealized_pnl = float(pos["size_tokens"]) * (float(new_price) - float(pos["avg_price"]))
        logger.info(f"{market_id[:10]}... PnL: ${unrealized_pnl:.2f} (price {new_price:.4f})")

    def get_portfolio_metrics(self) -> Dict[str, Any]:
        """Метрики портфеля."""
        unrealized_pnl = 0.0

        for _market_id, pos in self.positions.items():
            # Симуляция текущей цены (random walk вокруг avg_price)
            avg = float(pos["avg_price"])
            current_price = avg * random.uniform(0.95, 1.05)
            unrealized_pnl += float(pos["size_tokens"]) * (current_price - avg)

        total_value = self.balance + unrealized_pnl
        base = self.initial_balance if self.initial_balance else 1.0
        return {
            "cash_usd": round(self.balance, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "total_value": round(total_value, 2),
            "total_return_pct": round((total_value - base) / base * 100, 4),
            "positions_count": len(self.positions),
            "open_exposure_usd": round(sum(float(p["size_tokens"]) * float(p["avg_price"]) for p in self.positions.values()), 2),
        }

    def close_all_positions(self) -> None:
        """Закрыть всё для теста PnL."""
        final_pnl = float(self.get_portfolio_metrics()["unrealized_pnl"])
        self.closed_trades.append({"timestamp": "final", "pnl_usd": final_pnl})
        self.total_pnl += final_pnl
        logger.info(f"FINAL PnL: ${final_pnl:.2f}")

