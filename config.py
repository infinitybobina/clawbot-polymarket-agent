#!/usr/bin/env python3
"""
Продакшен-конфиг ClawBot (из бэктестов и теста A).
Используется в main.py и при необходимости в backtest.
"""

PROD_CONFIG = {
    "n_markets": 25,            # кандидатов для стратегии; макс. позиций ~25 при текущей экспозиции
    "min_yes_edge": 0.08,       # 8% — порог недооценки YES (0.50 - yes_price)
    "min_ev_threshold": 0.02,  # EV >= 2% (по бэктестам оптимально)
    "momentum_threshold": 0.02, # для бэктеста: допуск падения цены за час 2%
    "min_volume": 500_000,     # $500k минимальный объём рынка
    "risk_per_trade": 0.024,   # 2.4% на сделку
    "max_category_pct": 0.80,  # 80% — ослаблено, чтобы сделки проходили
    "max_exposure_pct": 0.80,  # 80% суммарная экспозиция
    "initial_balance": 100_000,
    # SL/TP в долях от цены входа
    "sl_pct": 0.07,
    "tp_pct": 0.18,
    "min_reward_risk_ratio": 1.5,  # ослаблено: reward:risk ≥ 1.5:1
    "high_entry_ratio_exempt": 0.95,  # при entry >= 0.95 не требуем min_rr (TP у потолка 0.99)
    "sl_cooldown_runs": 20,        # после закрытия по SL не входить в этот рынок 20 запусков
    "tp_cooldown_runs": 5,         # после закрытия по TP не входить в этот рынок 5 запусков
}
