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
        "min_volume_usd": 15_000,    # временно 15k (было 30k) — оживление пула от Gamma
        "min_edge": 0.05,
        "gamma_category": "politics",
    },
    "world": {
        "markets_limit": 3,
        "min_volume_usd": 15_000,
        "min_edge": 0.05,
        "gamma_category": "world",  # Gamma tag slug
    },
    "geopolitics": {
        "markets_limit": 3,
        "min_volume_usd": 15_000,
        "min_edge": 0.05,
        "gamma_category": "geopolitics",  # Gamma tag slug
    },
    "finance": {
        "markets_limit": 3,
        "min_volume_usd": 15_000,
        "min_edge": 0.05,
        "gamma_category": "finance",  # Gamma tag slug
    },
    "sports": {
        "markets_limit": 3,
        "min_volume_usd": 10_000,    # временно 10k (было 15k) — оживление пула
        "min_edge": 0.05,
        "gamma_category": "sports",
    },
    "crypto": {
        "markets_limit": 4,
        "min_volume_usd": 2_000,     # порог входа снижен: $2k (было 5k)
        "min_liquidity_usd": 0,      # liq >= 0 — не резать по ликвидности (Crypto 0 иначе)
        "resolution_hours_max": 72.0,  # до 72h — больше крипто-рынков в пуле
        "min_edge": 0.05,
        "gamma_category": "crypto",
    },
    "culture": {
        "markets_limit": 3,
        "min_volume_usd": 10_000,    # низкая, но частые события (Oscar, Music)
        "min_edge": 0.05,
        "gamma_category": "culture",
    },
    "economy": {
        "markets_limit": 3,
        "min_volume_usd": 50_000,    # временно 50k (было 100k) — оживление пула от Gamma
        "min_edge": 0.05,
        "gamma_category": "economy",
    },
}
# Какие категории торгуем.
# Временно: sports выключены (в нашем окне закрытий основной минус пришёлся на sports+SL).
ACTIVE_CATEGORIES = ["politics", "world", "geopolitics", "finance", "culture", "crypto", "economy"]

# --- Профили стратегии 15M (эксперименты: консервативный / агрессивный) ---
STRATEGY_PROFILE_15M = "15m_conservative"

strategy_params_15m_conservative = {
    "max_spread": 0.04,
    "min_yes_price": 0.10,
    "max_yes_price": 0.90,
    "min_clob_volume_24h": 2000.0,
    "min_best_level_size": 100.0,
    "min_edge": 0.06,
    "min_time_to_expiry_sec": 7200,
    "max_time_to_expiry_sec": 780,
}

strategy_params_15m_aggressive = {
    "max_spread": 0.08,
    "min_yes_price": 0.03,
    "max_yes_price": 0.97,
    "min_clob_volume_24h": 500.0,
    "min_best_level_size": 30.0,
    "min_edge": 0.02,
    "min_time_to_expiry_sec": 7200,
    "max_time_to_expiry_sec": 900,
}

PROD_CONFIG = {
    # Gamma API: при медленной сети или перегрузке увеличить timeout / retries (меньше TimeoutError в логах).
    "gamma_http_timeout_sec": 60.0,
    "gamma_http_retries": 5,
    "n_markets": 80,            # кол-во market_id для price stream; пул кандидатов — до max_markets_to_enrich
    "max_markets_to_enrich": 180,  # боевой режим: шире вселенная рынков для статистики
    "enrich_yes_price_min": 0.10,  # для enrich брать только рынки с yes_price >= 0.10 (отсечь решённые 0.01)
    "enrich_yes_price_max": 0.90,  # и yes_price <= 0.90 (отсечь решённые 0.99) → больше двусторонних стаканов
    "fallback_yes_price_min": 0.10,  # fallback: Gamma yes_price в [0.10, 0.90] (как enrich pool)
    "fallback_yes_price_max": 0.90,  # 0.15/0.85 резало всех 17 кандидатов → расширено
    "min_yes_edge": 0.06,       # 6% глобальный минимум: меньше слабых входов, выше селективность
    "min_ev_threshold": 0.04,  # временно мягче для набора выборки: EV >= 4%
    # Edge-gate: минимальный llm_score/expected_ev для входа (0..1).
    # Временный системный фильтр: не брать слабые сигналы < 0.20.
    "entry_min_llm_score": 0.10,
    "momentum_threshold": 0.02, # для бэктеста: допуск падения цены за час 2%
    "min_volume": 30_000,    # политика: мин. объём рынка $30k. После теста 500_000
    "risk_per_trade": 0.0035,  # 0.35% баланса на сделку (снижение риска на период диагностики)
    # EV-aware sizing: линейно масштабировать размер позиции по expected_ev.
    # ev<=min_ev -> min_multiplier * base_size, ev>=max_ev -> max_multiplier * base_size.
    "size_scale_by_ev": True,
    "ev_size_min_multiplier": 0.5,
    "ev_size_max_multiplier": 1.25,
    "ev_size_min_ev": 0.05,
    "ev_size_max_ev": 0.12,
    # RR threshold tolerance: avoid float-pushed borderline rejects.
    "rr_tolerance_eps": 1e-3,
    "max_category_pct": 0.40,  # суже по категории — меньше кластерного риска
    "max_exposure_pct": 0.50,  # половина баланса в открытых позициях (номинал)
    # Потолок одновременных рынков с позицией. 0 = без лимита (временно для статистики SL/TP).
    # Для боя вернуть, например, 12–18.
    "max_open_positions": 0,
    # Сколько НОВЫХ рынков (ещё не в портфеле) открыть за один LLM-слот — защита от «залпа» сигналов
    "max_new_trades_per_llm_slot": 3,
    "initial_balance": 100_000,
    # SL/TP в долях от цены входа
    "sl_pct": 0.12,   # шире стоп: меньше выбивает шумом на best_bid
    "tp_pct": 0.18,   # дальше цель: компенсируем более широкий SL
    # Stop trigger buffer (для длинных YES):
    # срабатываем SL чуть раньше, чтобы из-за дискретности обновления цен
    # не закрываться сильно ниже sl_at_exit.
    "sl_trigger_buffer": 0.005,
    # Тайм-стоп: за N минут до resolution закрывать убыточные позиции, чтобы не ловить 0.01 на экспирации
    "time_stop_minutes": 10,
    # Тайм-стоп для «долго висящих минусов»: если позиция в минусе и возраст >= N часов — закрыть.
    # 0 = отключить. Полезно против хвоста SL, которые накапливаются сутки+.
    "loser_time_stop_hours": 18,
    # Минимальная просадка (доля от entry), чтобы считать позицию «в минусе» для тайм-стопа.
    # Пример 0.005 = -0.5% от entry. 0 = любой минус.
    "loser_time_stop_min_drawdown": 0.005,
    # Жёсткий лимит удержания: закрыть позицию при возрасте >= N часов независимо от PnL.
    # Нужен, чтобы не держать 12h+ в режиме деградации edge.
    "hard_max_hold_hours": 12,
    "max_entry_price": MAX_ENTRY_PRICE,
    "min_entry_price": MIN_ENTRY_PRICE,  # рынки с YES < min считаем мёртвыми
    "min_reward_risk_ratio": 1.5,  # ослаблено: reward:risk ≥ 1.5:1
    "high_entry_ratio_exempt": 0.95,  # при entry >= 0.95 не требуем min_rr (TP у потолка 0.99)
    "sl_cooldown_runs": 180,       # ~180 минут при тике 60 с — не лезть обратно в рынок сразу после SL
    # Усиленный cooldown после SL/TIME_STOP для кластерно-рисковых категорий (в тиках цикла).
    # Нужен, чтобы не «перезаходить» в один и тот же politics/geopolitics рынок серией SL.
    "sl_cooldown_runs_politics": 720,     # ~12 часов
    "sl_cooldown_runs_geopolitics": 720,  # ~12 часов
    "tp_cooldown_runs": 5,         # после закрытия по TP не входить в этот рынок 5 запусков
    # Повторный вход в тот же рынок после успешного BUY (тики = минуты при LOOP 60 с)
    "reentry_cooldown_minutes": 240,
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
    # Профиль 15M для экспериментов: "15m_conservative" | "15m_aggressive" (см. strategy_params_15m_* ниже)
    "strategy_profile_15m": STRATEGY_PROFILE_15M,
    # Hot-fix: не подавать эти market_id в LLM (фильтр в get_tradeable_top по exclude_ids)
    "excluded_markets": [],  # например ["0x9c1a953fe92c83...", "0x1fad72fae20414..."]
    # Ликвидность на входе (directional): пул 0.10–0.90 уже отсекает решённые; здесь только экстремум
    "entry_liquidity": {
        "max_spread": 0.55,           # 0.55 — 0.35 резало всех (CLOB часто bid/ask 0.01/0.99 → spread 0.98); 0.55 пропускает широкие, но не мёртвые
        "min_best_level_size": 1.0,   # 1 = только проверка что есть bid/ask; при None size проверка пропускается (см. datafeed)
        "min_book_levels": 1,         # хотя бы 1 уровень с каждой стороны (0 = пустой стакан)
    },
    # Дополнительный spread-гейт на вход: отсекаем "слишком узкие/залипшие" книги, где сигнал деградирует.
    # Важно: если сделок 0 (по логам spread_too_narrow доминирует), опускай порог до 0.01 или 0.0,
    # иначе статистику не собрать.
    "entry_spread_min": 0.0,
    # --- Copy-trading adapter (paper): внешний парсер пишет сигналы в JSON,
    # бот конвертирует их в BUY-YES ордера и прогоняет через текущий risk manager.
    "copy_trading": {
        "enabled": False,
        "signals_file": "copy_signals.json",
        "base_size_usd": 300.0,
        "min_expected_ev": 0.04,
        "max_entry_price": 0.50,
        "default_weight": 1.0,
        # True: copy-сигналы не подмешиваются в рынки с уже открытой позицией (как раньше).
        # False: открытые позиции НЕ режут copy на входе в адаптер — дальше действуют RiskManager и лимиты;
        #        PaperTrader усредняет допокупку. Cooldown SL/TP/re-entry по-прежнему учитываются.
        "exclude_open_positions": True,
    },
    # --- Market-making strategy (ветка Marketmaking-strategy); выключено — приоритет стабильной торговли ---
    "enable_mm": False,
    "mm_params": {
        "max_spread": 0.12,              # спред до 12 центов (0.06 давало liquid=0)
        "min_best_level_size": 1.0,     # 1 = по сути только проверка что есть bid/ask (размер часто None из CLOB)
        "min_book_levels": 1,           # хотя бы 1 уровень (0 по умолчанию в snapshot резал всех)
        "min_24h_volume_usd": 0,        # не фильтровать по объёму
        "min_seconds_to_resolution": 1800,  # 30 минут: не маркет-мейкить прямо перед резолвом (анти-EXPIRY)
        "epsilon_price": 0.005,          # смещение внутрь спреда (чуть лучше best_bid/ask)
        "delta_price": 0.005,            # защита от ухода слишком глубоко от mid (fallback без CLOB)
        "max_offset_from_mid": 0.02,    # лимитка не дальше 0.02 от mid
        "tp_ticks": 0.015,              # цель 1–2 тика (0.01–0.02)
        "sl_ticks": 0.02,               # стоп по цене
        "position_time_stop_seconds": 180,  # кэш-аут позиции через 3 мин
        "order_timeout_seconds": 180,   # должно быть > LOOP_INTERVAL_SEC (60), иначе лимитка истечёт до следующей проверки fill
        "risk_per_trade_pct": 0.005,    # 0.5% депо на одну сделку (по SL)
        "max_open_markets": 3,          # макс. рынков с открытыми MM-позициями
        "max_position_per_market_pct": 0.005,  # макс. 0.5% депо в одном рынке
        "daily_loss_limit_pct": 0.02,   # −2% депо — авто-стоп до конца дня
    },
}

# Профили стратегии 15M (подставляются в payload.strategy_params для LLM)
PROD_CONFIG["strategy_params_15m_conservative"] = strategy_params_15m_conservative
PROD_CONFIG["strategy_params_15m_aggressive"] = strategy_params_15m_aggressive

# v2.0: интервалы (main_v2.py)
LOOP_INTERVAL_SEC = 60       # основной цикл: каждые 60 сек
# LLM-слот реже тика цикла: иначе за ~15 мин накапливается десяток+ новых позиций при нескольких сигналах за вызов
LLM_INTERVAL_SEC = 300       # 5 мин (для диагностики «каждую минуту» временно поставьте 60)
TELEGRAM_INTERVAL_SEC = 3600 # отчёт в Telegram: раз в час
PRICE_STREAM_INTERVAL_SEC = 10  # REST fallback: опрос цен каждые 10 сек

# v2.0: n_markets = sum(MARKET_CATEGORIES[c]["markets_limit"] for c in ACTIVE_CATEGORIES)

PROD_CONFIG["copy_trading"]["enabled"] = True
# Paper / лидеры: иначе статичный copy_signals.json по тем же market_id даёт kept=0 при открытой позиции.
PROD_CONFIG["copy_trading"]["exclude_open_positions"] = False