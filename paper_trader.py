#!/usr/bin/env python3
"""
Block 4: Paper Trading Simulator
Имитирует CLOB: исполняет ордера, обновляет PnL, показывает метрики.

Важно:
- Имя файла без дефиса (paper_trader.py), чтобы его можно было импортировать.
"""

import logging
import random
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class PaperTrader:
    def __init__(self, initial_balance: float = 100000):
        self.initial_balance = float(initial_balance)
        self.balance = float(initial_balance)
        # market_id → {outcome, size_tokens, avg_price}
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.closed_trades: List[Dict[str, Any]] = []  # история PnL (на будущее)
        self.total_pnl = 0.0

    def execute_orders(
        self,
        approved_orders: List[Dict[str, Any]],
        backtest_fill_prices: Optional[Dict[str, float]] = None,
        slippage_pct: float = 0.005,
        fee_pct: float = 0.001,
    ) -> Dict[str, Any]:
        """Имитирует исполнение ордеров на CLOB. Для бэктеста: backtest_fill_prices[market_id]=цена, slippage_pct, fee_pct."""
        executions = []
        for order in approved_orders:
            market_id = order["market_id"]
            outcome = order.get("outcome", "YES")
            size_usd = float(order.get("final_size_usd") or order.get("approved_size_usd") or order.get("target_size_usd") or 0)
            limit_price = float(order.get("limit_price") or 0)

            if size_usd <= 0:
                continue

            safe_price = max(limit_price, 0.0001)
            if backtest_fill_prices and market_id in backtest_fill_prices:
                fill_price = backtest_fill_prices[market_id] * (1 + slippage_pct)
                fill_price *= (1 + fee_pct)
            else:
                if random.random() > 0.1:
                    fill_price = safe_price * random.uniform(0.99, 1.01)
                else:
                    fill_price = safe_price * random.uniform(1.00, 1.03)

            tokens_bought = size_usd / fill_price
            sl = float(order.get("stop_loss_price") or 0)
            tp = float(order.get("take_profit_price") or 1)

            # Записываем / усредняем позицию; SL/TP задаём при открытии, при допокупке не меняем
            if market_id not in self.positions:
                self.positions[market_id] = {
                    "outcome": outcome,
                    "size_tokens": 0.0,
                    "avg_price": 0.0,
                    "stop_loss_price": sl if sl > 0 else None,
                    "take_profit_price": tp if tp > 0 else None,
                }

            pos = self.positions[market_id]
            prev_tokens = float(pos.get("size_tokens", 0.0))
            prev_avg = float(pos.get("avg_price", 0.0))
            new_tokens = prev_tokens + tokens_bought
            new_avg = (prev_tokens * prev_avg + tokens_bought * fill_price) / new_tokens if new_tokens > 0 else fill_price
            out = {"outcome": outcome, "size_tokens": new_tokens, "avg_price": new_avg}
            if pos.get("stop_loss_price") is not None:
                out["stop_loss_price"] = pos["stop_loss_price"]
                out["take_profit_price"] = pos["take_profit_price"]
            elif sl > 0 and tp > 0:
                out["stop_loss_price"] = sl
                out["take_profit_price"] = tp
            self.positions[market_id] = out
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

    def close_positions(
        self,
        to_close: List[Dict[str, Any]],
        fee_pct: float = 0.001,
    ) -> Dict[str, Any]:
        """Закрыть позиции (продажа по текущей цене). to_close = [{market_id, sell_price, reason?}, ...]."""
        closed = []
        for item in to_close:
            market_id = item.get("market_id")
            sell_price = float(item.get("sell_price", 0))
            reason = item.get("reason", "exit")
            if market_id not in self.positions or sell_price <= 0:
                continue
            pos = self.positions[market_id]
            tokens = float(pos.get("size_tokens", 0))
            avg_price = float(pos.get("avg_price", 0))
            if tokens <= 0:
                continue
            proceeds = tokens * sell_price * (1 - fee_pct)
            pnl = proceeds - tokens * avg_price
            self.balance += proceeds
            self.closed_trades.append({
                "market_id": market_id,
                "pnl_usd": round(pnl, 2),
                "tokens": tokens,
                "avg_price": avg_price,
                "sell_price": sell_price,
                "reason": reason,
            })
            del self.positions[market_id]
            closed.append({"market_id": market_id, "pnl_usd": round(pnl, 2), "proceeds": round(proceeds, 2), "reason": reason})
            logger.info("CLOSED %s (%s): %.2f tokens @ %.4f -> %.4f, PnL $%.2f", market_id[:10], reason, tokens, avg_price, sell_price, pnl)
        return {"closed": closed, "portfolio": self.get_portfolio_metrics()}

    def simulate_market_move(self, market_id: str, new_price: float) -> None:
        """Имитирует изменение цены (для теста PnL)."""
        if market_id not in self.positions:
            return
        pos = self.positions[market_id]
        unrealized_pnl = float(pos["size_tokens"]) * (float(new_price) - float(pos["avg_price"]))
        logger.info(f"{market_id[:10]}... PnL: ${unrealized_pnl:.2f} (price {new_price:.4f})")

    def get_portfolio_metrics(self, mark_to_market_prices: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """Метрики портфеля. Для бэктеста: mark_to_market_prices[market_id]=текущая цена."""
        unrealized_pnl = 0.0
        cost_basis = 0.0
        for mid, pos in self.positions.items():
            avg = float(pos["avg_price"])
            tok = float(pos["size_tokens"])
            cost_basis += tok * avg
            if mark_to_market_prices and mid in mark_to_market_prices:
                current_price = mark_to_market_prices[mid]
            else:
                # Без реальных цен — считаем по себестоимости, чтобы Total Value не прыгал от random между запусками
                current_price = avg
            unrealized_pnl += tok * (current_price - avg)

        # total_value = cash + позиции по текущим ценам = balance + cost_basis + unrealized_pnl
        total_value = self.balance + cost_basis + unrealized_pnl
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

