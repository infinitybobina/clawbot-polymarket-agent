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
    INVALID_SL_TP = "INVALID_SL_TP_LEVELS"
    RISK_PER_TRADE_EXCEEDED = "RISK_PER_TRADE_EXCEEDED"
    ENTRY_TOO_HIGH = "ENTRY_TOO_HIGH"
    MAX_OPEN_POSITIONS = "MAX_OPEN_POSITIONS"
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
            "max_daily_volume_pct": 0.10,      # 10%
            # Size scaling by expected EV (linear between ev_min..ev_max).
            "size_scale_by_ev": False,
            "ev_size_min_multiplier": 0.5,
            "ev_size_max_multiplier": 1.25,
            "ev_size_min_ev": 0.04,
            "ev_size_max_ev": 0.12,
            # Tolerance for reward/risk ratio check (float rounding can push ratio slightly below).
            # We accept RR that is very close to min_rr to avoid rejecting borderline signals.
            "rr_tolerance_eps": 1e-3,
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
        target_size = float(signal["target_size_usd"])
        market_id = signal["market_id"]
        category = signal.get("category") or self.config.get("category", "US-current-affairs")
        expected_ev = float(signal.get("expected_ev") or 0.0)
        entry = float(signal.get("limit_price") or 0)
        stop_loss_price = float(signal.get("stop_loss_price") or 0)
        take_profit_price = float(signal.get("take_profit_price") or 1)
        logger.info("Risk eval: entry=%.4f sl=%.4f tp=%.4f", entry, stop_loss_price, take_profit_price)

        max_open = int(self.config.get("max_open_positions") or 0)
        if max_open > 0:
            existing = {
                m for m, sz in self.portfolio.positions.items()
                if float(sz or 0) > 1e-6
            }
            if market_id not in existing and len(existing) >= max_open:
                logger.warning(
                    "Risk: MAX_OPEN_POSITIONS — already %d markets (cap=%d), skip new market %s..",
                    len(existing), max_open, market_id[:16],
                )
                return {"status": "rejected", "reason": RejectReason.MAX_OPEN_POSITIONS.value}

        # Optional EV-aware size scaling: weak EV gets smaller size, strong EV gets larger size.
        if bool(self.config.get("size_scale_by_ev", False)):
            ev_min = float(self.config.get("ev_size_min_ev", 0.04))
            ev_max = float(self.config.get("ev_size_max_ev", 0.12))
            mul_min = float(self.config.get("ev_size_min_multiplier", 0.5))
            mul_max = float(self.config.get("ev_size_max_multiplier", 1.25))
            if ev_max <= ev_min:
                ev_max = ev_min + 1e-6
            if expected_ev <= ev_min:
                ev_mul = mul_min
            elif expected_ev >= ev_max:
                ev_mul = mul_max
            else:
                k = (expected_ev - ev_min) / (ev_max - ev_min)
                ev_mul = mul_min + k * (mul_max - mul_min)
            before = target_size
            target_size = max(0.0, target_size * ev_mul)
            logger.info(
                "Risk EV size scale: ev=%.4f mul=%.3f size %.2f -> %.2f",
                expected_ev,
                ev_mul,
                before,
                target_size,
            )

        max_entry = self.config.get("max_entry_price", 0.90)
        if entry > max_entry:
            logger.warning("Risk: ENTRY_TOO_HIGH entry=%.2f > %.2f (мало апсайда, большой даунсайд)", entry, max_entry)
            return {"status": "rejected", "reason": RejectReason.ENTRY_TOO_HIGH.value}

        # 0. SL/TP: при невалидных уровнях или плохом ratio — пересчитываем по конфигу
        eps = 1e-6
        sl_pct = self.config.get("sl_pct", 0.07)
        tp_pct = self.config.get("tp_pct", 0.18)
        min_rr = self.config.get("min_reward_risk_ratio", 1.5)

        def _recompute_sl_tp():
            nonlocal stop_loss_price, take_profit_price
            stop_loss_price = max(0.01, entry * (1 - sl_pct))
            if stop_loss_price >= entry:
                stop_loss_price = round(entry - 0.01, 4)
            take_profit_price = min(0.99, entry * (1 + tp_pct))
            if take_profit_price <= entry:
                take_profit_price = min(0.99, round(entry + 0.01, 4))
            logger.info("Risk: SL/TP пересчитаны по конфигу: entry=%.4f sl=%.4f tp=%.4f", entry, stop_loss_price, take_profit_price)

        if entry <= 0:
            entry = 0.5
            logger.info("Risk: entry был 0, подставлен default 0.5")
            _recompute_sl_tp()
        elif stop_loss_price >= entry - eps or take_profit_price <= entry + eps:
            _recompute_sl_tp()
        reward = take_profit_price - entry
        risk_dist = entry - stop_loss_price
        if risk_dist <= 0:
            logger.warning("Risk: INVALID_SL_TP — risk_dist<=0 (entry=%.4f sl=%.4f)", entry, stop_loss_price)
            return {"status": "rejected", "reason": RejectReason.INVALID_SL_TP.value}
        ratio = (reward / risk_dist) if risk_dist else 0
        high_entry_threshold = self.config.get("high_entry_ratio_exempt", 0.95)
        require_rr = entry < high_entry_threshold  # при entry >= 0.95 не требуем min_rr (TP у потолка)
        rr_eps = float(self.config.get("rr_tolerance_eps", 1e-4))
        if reward > 0 and (ratio + rr_eps) < min_rr and require_rr:
            _recompute_sl_tp()
            reward = take_profit_price - entry
            risk_dist = entry - stop_loss_price
            ratio = (reward / risk_dist) if risk_dist else 0
            if risk_dist <= 0:
                logger.warning("Risk: INVALID_SL_TP — после пересчёта risk_dist<=0")
                return {"status": "rejected", "reason": RejectReason.INVALID_SL_TP.value}
            if reward > 0 and (ratio + rr_eps) < min_rr and entry < high_entry_threshold:
                logger.warning("Risk: INVALID_SL_TP — ratio=%.2f < min_rr=%.1f (entry=%.4f sl=%.4f tp=%.4f)", ratio, min_rr, entry, stop_loss_price, take_profit_price)
                return {"status": "rejected", "reason": RejectReason.INVALID_SL_TP.value}
        if reward > 0 and (ratio + rr_eps) < min_rr and entry >= high_entry_threshold:
            logger.info("Risk: entry=%.2f >= %.2f — ratio не проверяем (TP у потолка), пропускаем", entry, high_entry_threshold)
        # Денежный риск при срабатывании SL
        risk_usd = target_size * (entry - stop_loss_price) / entry if entry > 0 else 0
        max_risk_usd = self.portfolio.balance_usd * self.config["max_single_market_pct"]
        if risk_usd > max_risk_usd:
            # Уменьшаем размер так, чтобы risk_usd = max_risk_usd
            target_size = max_risk_usd * entry / (entry - stop_loss_price) if risk_dist > 0 else 0
            if target_size < 50:  # минимум $50 на сделку (ослаблено)
                return {"status": "rejected", "reason": RejectReason.RISK_PER_TRADE_EXCEEDED.value}
            risk_usd = max_risk_usd

        # 1. SINGLE_POSITION — подрезаем размер до лимита вместо отклонения
        max_single = self.portfolio.balance_usd * self.config["max_single_market_pct"]
        current_in_market = self.portfolio.positions.get(market_id, 0)
        room = max(0, max_single - current_in_market)
        if room < 50:
            return {"status": "rejected", "reason": RejectReason.EXCEEDED_SINGLE_POSITION.value}
        if target_size > room:
            target_size = room
            logger.info("Risk: размер подрезан до лимита по рынку: %.0f USD", target_size)
        new_size = current_in_market + target_size

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
                "limit_price": entry,
                "final_size_usd": target_size,
                "stop_loss_price": stop_loss_price,
                "take_profit_price": take_profit_price,
                "impact": "low",
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