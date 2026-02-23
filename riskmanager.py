#!/usr/bin/env python3
"""
Block 3: Risk Manager
Approve/Reject signals → portfolio state
"""

import json
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from enum import Enum
from strategy import TradingSignal  # импорт из Block 2

logger = logging.getLogger(__name__)

class RejectReason(Enum):
    EXCEEDED_SINGLE_POSITION = "EXCEEDED_SINGLE_POSITION"
    EXCEEDED_CATEGORY_EXPOSURE = "EXCEEDED_CATEGORY_EXPOSURE"
    TOTAL_EXPOSURE_TOO_HIGH = "TOTAL_EXPOSURE_TOO_HIGH"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT_REACHED"
    OK = "OK"

@dataclass
class PortfolioState:
    balance_usd: float = 100000
    positions: Dict[str, float] = None  # market_id → size_usd
    daily_pnl: float = 0
    exposure_by_category: Dict[str, float] = None
    daily_volume_usd: float = 0

    def __post_init__(self):
        if self.positions is None:
            self.positions = {}
        if self.exposure_by_category is None:
            self.exposure_by_category = {}

class RiskManager:
    def __init__(self):
        self.config = {
            "max_single_market_pct": 0.05,    # 5%
            "max_category_pct": 0.15,         # 15%
            "max_exposure_pct": 0.25,         # 25%
            "max_daily_loss_usd": 5000,
            "max_daily_volume_pct": 0.10      # 10%
        }
        self.portfolio = PortfolioState()

    def process_signals(self, signals: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Главный метод: approve/reject"""
        approved = []
        rejected = []
        
        for signal in signals:
            decision = self._evaluate_signal(signal)
            if decision["status"] == "approved":
                approved.append(decision["order"])
            else:
                rejected.append(decision["reason"])
        
        return {
            "approved_orders": approved,
            "rejected_signals": rejected,
            "portfolio_summary": {
                "balance_usd": self.portfolio.balance_usd,
                "total_exposure_pct": sum(self.portfolio.positions.values()) / self.portfolio.balance_usd,
                "risk_violations_prevented": len(rejected)
            }
        }

    def _evaluate_signal(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        """Проверки по порядку приоритета"""
        target_size = signal["target_size_usd"]
        market_id = signal["market_id"]
        category = "US-current-affairs"  # TODO: из Gamma
        
        # 1. SINGLE_POSITION
        new_size = self.portfolio.positions.get(market_id, 0) + target_size
        if new_size > self.portfolio.balance_usd * self.config["max_single_market_pct"]:
            return {"status": "rejected", "reason": RejectReason.EXCEEDED_SINGLE_POSITION.value}
        
        # 2. CATEGORY_EXPOSURE
        cat_exposure = self.portfolio.exposure_by_category.get(category, 0) + target_size
        if cat_exposure > self.portfolio.balance_usd * self.config["max_category_pct"]:
            return {"status": "rejected", "reason": RejectReason.EXCEEDED_CATEGORY_EXPOSURE.value}
        
        # 3. TOTAL_EXPOSURE
        total_exposure = sum(self.portfolio.positions.values()) + target_size
        if total_exposure > self.portfolio.balance_usd * self.config["max_exposure_pct"]:
            return {"status": "rejected", "reason": RejectReason.TOTAL_EXPOSURE_TOO_HIGH.value}
        
        # 4. DAILY_LOSS_LIMIT
        if self.portfolio.daily_pnl < -self.config["max_daily_loss_usd"]:
            return {"status": "rejected", "reason": RejectReason.DAILY_LOSS_LIMIT.value}
        
        # OK: approve с теми же параметрами
        self.portfolio.positions[market_id] = new_size
        self.portfolio.exposure_by_category[category] = cat_exposure
        self.portfolio.daily_volume_usd += target_size
        
        return {
            "status": "approved",
            "order": {
                **signal,
                "final_size_usd": target_size,  # можно подрезать, если нужно
                "impact": "low"
            }
        }

# Тест с твоими данными
if __name__ == "__main__":
    # Загрузи твои signals из candidates.json
    with open("candidates.json", "r") as f:
        data = json.load(f)
    
    signals = data["signals"]  # твои 2 сигнала
    
    risk_mgr = RiskManager()
    result = risk_mgr.process_signals(signals)
    
    print("=== Risk Manager Results ===")
    print(json.dumps(result, indent=2))