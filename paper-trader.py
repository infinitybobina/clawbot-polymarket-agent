
"""
Устаревшее имя файла.

Python не может импортировать модуль с дефисом в имени (`paper-trader.py`).
Используй `paper_trader.py`.
"""

# Реэкспорт для совместимости (если где-то запускали/использовали этот файл напрямую)
from paper_trader import PaperTrader  # noqa: F401


class PaperTrader:
    def __init__(self, initial_balance: float = 100000):
        self.balance = initial_balance
        self.positions: Dict[str, Dict] = {}  # market_id → {size_usd, avg_price, outcome}
        self.closed_trades: List[Dict] = []   # история PnL
        self.total_pnl = 0
        
        total_value = self.balance + unrealized_pnl
        return {
            "cash_usd": round(self.balance, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "total_value": round(total_value, 2),
            "total_return_pct": round((total_value - 100000) / 100000 * 100, 2),
            "positions_count": len(self.positions),
            "open_exposure_usd": sum(pos["size_tokens"] * pos["avg_price"] for pos in self.positions.values())
        }

    def close_all_positions(self):
        """Закрыть всё для теста PnL"""
        final_pnl = self.get_portfolio_metrics()["unrealized_pnl"]
        self.closed_trades.append({"timestamp": "final", "pnl_usd": final_pnl})
        self.total_pnl += final_pnl
        logger.info(f"🏁 FINAL PnL: ${final_pnl:.2f} ({final_pnl/1000:.1f}%)")
        
    def execute_orders(self, approved_orders: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Имитирует исполнение на CLOB"""
        executions = []
        
        for order in approved_orders:
            market_id = order["market_id"]
            outcome = order["outcome"]
            size_usd = order["final_size_usd"]
            limit_price = order["limit_price"]
            
            # Симуляция: 90% fill по лимитной цене, 10% slippage
            fill_price = limit_price * random.uniform(0.99, 1.01) if random.random() > 0.1 else limit_price
            tokens_bought = size_usd / fill_price
            
            # Записываем позицию
            if market_id not in self.positions:
                self.positions[market_id] = {"outcome": outcome, "size_tokens": 0, "avg_price": 0}
            
            pos = self.positions[market_id]
            new_tokens = tokens_bought
            new_avg = (pos["size_tokens"] * pos["avg_price"] + tokens_bought) / (pos["size_tokens"] + tokens_bought)
            
            self.positions[market_id] = {
                "outcome": outcome,
                "size_tokens": pos["size_tokens"] + new_tokens,
                "avg_price": new_avg
            }
            self.balance -= size_usd
            
            executions.append({
                "market_id": market_id,
                "status": "FILLED",
                "fill_price": round(fill_price, 4),
                "tokens": round(tokens_bought, 2),
                "cost_usd": round(size_usd, 2)
            })
            
            logger.info(f"✅ FILLED: {market_id[:8]}... {outcome} {tokens_bought:.2f} tokens @ {fill_price:.4f}")
        
        return {
            "executions": executions,
            "portfolio": self.get_portfolio_metrics()
        }

    def simulate_market_move(self, market_id: str, new_price: float):
        """Имитирует изменение цены (для теста PnL)"""
        if market_id in self.positions:
            pos = self.positions[market_id]
            unrealized_pnl = pos["size_tokens"] * (new_price - pos["avg_price"])
            logger.info(f"📊 {market_id[:8]}... PnL: ${unrealized_pnl:.2f} (price {new_price:.4f})")

    def get_portfolio_metrics(self) -> Dict[str, float]:
        """Метрики портфеля"""
        unrealized_pnl = 0
        for market_id, pos in self.positions.items():
            # Симуляция текущей цены (random walk вокруг avg_price)
            current_price = pos["avg_price"] * random.uniform(0.95, 1.05)
            unrealized_pnl += pos["size_tokens"] * (current_price - pos["avg_price"])
        