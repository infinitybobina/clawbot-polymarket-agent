#!/usr/bin/env python3
"""
Продакшен-конфиг ClawBot (из бэктестов и теста A).
Используется в main.py и при необходимости в backtest.

v2.0: мульти-категории — состав торгуемых рынков задаётся здесь.
"""

# --- Пороги цены YES для пула кандидатов ---
MIN_ENTRY_PRICE = 0.0001   # 41 рынок войдёт!
MAX_ENTRY_PRICE = 0.9999   # 3 рынка 1.0000 отсеются

# Мульти-категории (v2.0): пороги по Polymarket 2026. Состав торгуемых — в ACTIVE_CATEGORIES.
MARKET_CATEGORIES = {
    "politics": {
        "markets_limit": 8,
        "min_volume_usd": 30_000,    # мин. объём рынка $30k
        "min_edge": 0.08,
        "gamma_category": "politics",
    },
    "sports": {
        "markets_limit": 3,
        "min_volume_usd": 15_000,    # мин. объём $15k (было 200k)
        "min_edge": 0.08,
        "gamma_category": "sports",
    },
    "crypto": {
        "markets_limit": 4,
        "min_volume_usd": 2_000,     # порог входа снижен: $2k (было 5k)
        "min_liquidity_usd": 0,      # liq >= 0 — не резать по ликвидности (Crypto 0 иначе)
        "resolution_hours_max": 72.0,  # до 72h — больше крипто-рынков в пуле
        "min_edge": 0.08,
        "gamma_category": "crypto",
    },
    "culture": {
        "markets_limit": 3,
        "min_volume_usd": 10_000,    # низкая, но частые события (Oscar, Music)
        "min_edge": 0.08,
        "gamma_category": "culture",
    },
    "economy": {
        "markets_limit": 3,
        "min_volume_usd": 100_000,   # средняя, макро события (Fed Rate)
        "min_edge": 0.08,
        "gamma_category": "economy",
    },
}
# Какие категории торгуем. DEBUG: только politics
ACTIVE_CATEGORIES = ["politics"]

PROD_CONFIG = {
    "n_markets": 25,            # кандидатов для стратегии; макс. позиций ~25 при текущей экспозиции
    "min_yes_edge": 0.08,       # 8% — порог недооценки YES (0.50 - yes_price)
    "min_ev_threshold": 0.02,  # EV >= 2% (по бэктестам оптимально)
    "momentum_threshold": 0.02, # для бэктеста: допуск падения цены за час 2%
    "min_volume": 30_000,    # политика: мин. объём рынка $30k. После теста 500_000
    "risk_per_trade": 0.015,   # 1.5% баланса max на сделку → $100k → max $1.5k
    "max_category_pct": 0.80,  # 80% — ослаблено, чтобы сделки проходили
    "max_exposure_pct": 0.80,  # 80% суммарная экспозиция
    "initial_balance": 100_000,
    # SL/TP в долях от цены входа (4% SL — меньше убыток за один стоп)
    "sl_pct": 0.04,
    "tp_pct": 0.18,
    "max_entry_price": MAX_ENTRY_PRICE,
    "min_entry_price": MIN_ENTRY_PRICE,  # рынки с YES < min считаем мёртвыми
    "min_reward_risk_ratio": 1.5,  # ослаблено: reward:risk ≥ 1.5:1
    "high_entry_ratio_exempt": 0.95,  # при entry >= 0.95 не требуем min_rr (TP у потолка 0.99)
    "sl_cooldown_runs": 20,        # после закрытия по SL не входить в этот рынок 20 запусков
    "tp_cooldown_runs": 5,         # после закрытия по TP не входить в этот рынок 5 запусков
    # Режим рынков: "politics" | "crypto" | "both". v2.0: см. ACTIVE_CATEGORIES и MARKET_CATEGORIES.
    "markets_category": "both",
    # Crypto: порог входа снижен (vol >= 2k, окно до 72h)
    "crypto_resolution_hours_max": 72.0,
    "crypto_min_volume": 2_000,
    "crypto_min_liquidity": 0,      # 0 = не фильтровать по ликвидности (ликвидность всё равно ограничивает размер заявки)
    # Размер заявки: макс. доля от объёма рынка и от глубины (2% рынка → microcap $30k → max $600)
    "max_trade_pct_of_volume": 0.02,
    "max_trade_pct_of_depth": 0.10,
    # Логирование: "INFO" (по умолчанию) или "DEBUG" для детального отладочного вывода
    "log_level": "INFO",
    # Режим торговли: "paper" — симуляция; переключается на "live" для реала (см. PRODUCTION_READY.md)
    "trading_mode": "paper",
}

# v2.0: интервалы (main_v2.py)
LOOP_INTERVAL_SEC = 60       # основной цикл: каждые 60 сек
LLM_INTERVAL_SEC = 300       # полный LLM: каждые 5 мин (12/час)
TELEGRAM_INTERVAL_SEC = 3600 # отчёт в Telegram: раз в час
PRICE_STREAM_INTERVAL_SEC = 10  # REST fallback: опрос цен каждые 10 сек

# v2.0: n_markets = sum(MARKET_CATEGORIES[c]["markets_limit"] for c in ACTIVE_CATEGORIES)
