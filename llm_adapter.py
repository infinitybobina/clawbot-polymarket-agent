"""
Адаптер для LLM-батча: сбор payload для модели и перевод решений в approved_orders.
Мостик: candidates/diag -> build_llm_batch_payload -> LLM -> llm_decisions_to_orders -> approved_orders.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from datafeed import MarketSnapshot, get_yes_token_id


# Жёсткий лимит рынков в одном батче для LLM
LLM_BATCH_MAX_MARKETS = 10

# Пороги для экспериментов/бэктеста: подставляются в payload.strategy_params. Вызывающий может передать
# strategy_params=cfg.get("strategy_params_15m", DEFAULT_STRATEGY_PARAMS).
DEFAULT_STRATEGY_PARAMS = {
    "max_spread": 0.06,                 # максимум (ask - bid) по YES
    "min_yes_price": 0.05,              # не брать почти нулевые хвосты
    "max_yes_price": 0.95,               # не брать почти зафиксированные исходы
    "min_clob_volume_24h": 500.0,       # минимальный CLOB-объём за 24 часа
    "min_best_level_size": 50.0,        # минимальный объём на best bid/ask (USDC)
    "min_edge": 0.03,                   # минимальное мат.ожидание/преимущество
    "min_time_to_expiry_sec": 180,      # не заходить, если осталось < 3 минут
    "max_time_to_expiry_sec": 900,      # игнорировать рынки, где ещё далеко до экспирации
}

# Системный промпт (draft для PROD). Использовать с response_format={"type": "json_object"}.
LLM_SYSTEM_PROMPT = """You are an automated trading signal generator for short-term Polymarket prediction markets.
You receive a JSON payload describing several markets and portfolio risk limits, and you must respond with valid JSON only.

Your task: for each market, decide whether to open a new position (BUY_YES or BUY_NO) or skip it.

Rules:

Always output a single JSON object with the top-level key "decisions".

"decisions" must be an array of objects with fields:

"market_id": string, must match one of the input markets.

"action": one of "BUY_YES", "BUY_NO", "SKIP".

"size_abs": non-negative number (absolute notional size in USDC).

Do not include markets in "decisions" if you are certain they should be skipped.

Obey the risk limits:

Do not exceed max_positions.

Do not suggest trades whose size_abs is greater than max_notional_per_trade.

Total suggested size per market_id must not exceed max_notional_per_market.

Prefer no trade over a bad trade: if there is not enough liquidity, the spread is too wide, or the edge is unclear, use "action": "SKIP".

You also receive an object "strategy_params" that contains numeric thresholds for this experiment (for example: "max_spread", "min_yes_price", "max_yes_price", "min_clob_volume_24h", "min_best_level_size", "min_edge", "min_time_to_expiry_sec", "max_time_to_expiry_sec").
You MUST:
- Treat these values as hard constraints.
- Only suggest trades on markets that satisfy all relevant thresholds.
- Never invent your own thresholds or ignore the provided ones.

These are 15-minute markets: favor small, conservative position sizes and clear edges.

Output format:

The response MUST be valid JSON.

No explanations, no comments, no extra keys, no trailing commas.

Do not wrap JSON in markdown.

Example response:

{
  "decisions": [
    {
      "market_id": "0xabc...",
      "action": "BUY_YES",
      "size_abs": 120.0
    }
  ]
}
"""

# Инструкция для user-сообщения (каркас)
USER_INSTRUCTION = (
    "Given the following markets and risk limits, decide which trades to take in the next 15 minutes. "
    "Return ONLY JSON with a 'decisions' array as described in the system message."
)


def build_llm_user_message(payload: Dict[str, Any], *, instruction: Optional[str] = None) -> Dict[str, Any]:
    """
    Каркас user-сообщения: instruction + payload.
    payload — результат build_llm_batch_payload(...).
    Для отправки в chat-completion: content = json.dumps(user_obj, ensure_ascii=False).
    """
    return {
        "instruction": instruction or USER_INSTRUCTION,
        "payload": payload,
    }


def build_llm_messages(
    payload: Dict[str, Any],
    *,
    instruction: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Собирает messages для chat-completion API:
      [{"role": "system", "content": LLM_SYSTEM_PROMPT}, {"role": "user", "content": json.dumps(user_obj)}]
    Использовать вместе с response_format={"type": "json_object"}.
    """
    user_obj = build_llm_user_message(payload, instruction=instruction)
    return [
        {"role": "system", "content": LLM_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user_obj, ensure_ascii=False)},
    ]


# Для передачи в chat-completion: response_format=LLM_RESPONSE_FORMAT
LLM_RESPONSE_FORMAT = {"type": "json_object"}


def build_llm_batch_payload(
    markets_diag: List[MarketSnapshot],
    portfolio: Union[Dict[str, Any], Any],
    *,
    mode: str = "15M_CRYPTO_BATCH",
    strategy_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Собирает компактный dict для JSON-сериализации и отправки в LLM.

    markets_diag: список кандидатов (MarketSnapshot), то что сейчас идёт в LLM.
    portfolio: текущее состояние портфеля. Может быть dict с ключами "positions", "risk_limits"
              или объект с атрибутами .positions, .risk_limits (max_positions, max_notional_per_trade, max_notional_per_market).
    strategy_params: пороги для модели (см. DEFAULT_STRATEGY_PARAMS). Передавать из конфига, например
                     strategy_params=cfg.get("strategy_params_15m", DEFAULT_STRATEGY_PARAMS).
    """
    params = dict(DEFAULT_STRATEGY_PARAMS)
    if strategy_params:
        params.update(strategy_params)

    positions = portfolio.get("positions", {}) if isinstance(portfolio, dict) else getattr(portfolio, "positions", {})
    risk_limits = portfolio.get("risk_limits", {}) if isinstance(portfolio, dict) else getattr(portfolio, "risk_limits", {})
    if not isinstance(risk_limits, dict):
        risk_limits = {}
    max_positions = risk_limits.get("max_positions", 20)
    max_notional_per_trade = risk_limits.get("max_notional_per_trade", 5000.0)
    max_notional_per_market = risk_limits.get("max_notional_per_market", 2000.0)

    markets_payload: List[Dict[str, Any]] = []
    for m in markets_diag[:LLM_BATCH_MAX_MARKETS]:
        mid = getattr(m, "market_id", None) or (m if isinstance(m, dict) else {}).get("market_id")
        if not mid:
            continue
        yes_price = getattr(m, "yes_price", None)
        token_yes = get_yes_token_id(m)
        question_short = (getattr(m, "question", None) or "")[:200] if getattr(m, "question", None) else ""
        pos = positions.get(mid) if isinstance(positions, dict) else {}
        position_side = "FLAT"
        position_size_abs = 0.0
        if pos:
            outcome = (pos.get("outcome") or "YES").upper()
            size_tokens = float(pos.get("size_tokens", 0) or 0)
            avg_price = float(pos.get("avg_price", 0) or 0)
            position_size_abs = size_tokens * avg_price
            if position_size_abs > 0 and "YES" in outcome:
                position_side = "LONG_YES"
            elif position_size_abs > 0 and "NO" in outcome:
                position_side = "LONG_NO"
            else:
                position_side = "FLAT"

        markets_payload.append({
            "market_id": mid,
            "token_yes": token_yes,
            "question": question_short,
            "category": getattr(m, "category", None) or "",
            "yes_price": yes_price,
            "no_price": (1.0 - yes_price) if yes_price is not None else None,
            "spread": getattr(m, "spread", None),
            "yes_best_bid": getattr(m, "yes_bid", None),
            "yes_best_ask": getattr(m, "yes_ask", None),
            "volume_24h": getattr(m, "volume_usd", None),
            "clob_volume_24h": getattr(m, "clob_volume_24h", None),
            "time_to_expiry_sec": getattr(m, "seconds_to_resolution", None),
            "position_side": position_side,
            "position_size": position_size_abs,
        })

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "strategy_params": params,
        "markets": markets_payload,
        "risk_limits": {
            "max_positions": max_positions,
            "max_notional_per_trade": max_notional_per_trade,
            "max_notional_per_market": max_notional_per_market,
        },
    }
    return payload


def _portfolio_can_open_notional(
    portfolio: Union[Dict[str, Any], Any],
    market_id: str,
    size_abs: float,
) -> bool:
    """Базовая проверка: не превышаем лимиты по размеру и числу позиций."""
    positions = portfolio.get("positions", {}) if isinstance(portfolio, dict) else getattr(portfolio, "positions", {})
    risk_limits = portfolio.get("risk_limits", {}) if isinstance(portfolio, dict) else getattr(portfolio, "risk_limits", {})
    if not isinstance(risk_limits, dict):
        risk_limits = {}
    max_positions = risk_limits.get("max_positions", 20)
    max_notional_per_trade = risk_limits.get("max_notional_per_trade", 5000.0)
    max_notional_per_market = risk_limits.get("max_notional_per_market", 2000.0)

    if size_abs <= 0 or size_abs > max_notional_per_trade:
        return False
    if market_id in positions:
        current = float((positions[market_id].get("size_tokens") or 0) * (positions[market_id].get("avg_price") or 0))
        if current + size_abs > max_notional_per_market:
            return False
    else:
        if len(positions) >= max_positions:
            return False
        if size_abs > max_notional_per_market:
            return False
    return True


def llm_decisions_to_orders(
    decisions_json: Dict[str, Any],
    datafeed: Any,
    portfolio: Union[Dict[str, Any], Any],
    *,
    sl_pct: float = 0.04,
    tp_pct: float = 0.18,
) -> List[Dict[str, Any]]:
    """
    Переводит JSON-ответ LLM в список approved_orders во внутреннем формате бота.

    decisions_json: dict ответа LLM вида {"decisions": [{"action": "BUY_YES"|"BUY_NO"|"CLOSE"|"SKIP", "market_id": "...", "size_abs": float}, ...]}.
    datafeed: ClawBotDataFeed (для limit_price, yes_token_id и проверки книги в main).
    portfolio: тот же объект/dict что и для build_llm_batch_payload (проверка can_open_notional).

    Возвращает список ордеров с полями: market_id, outcome, final_size_usd, limit_price,
    stop_loss_price, take_profit_price; yes_token_id дополняется в main из datafeed.
    Дальше в main_v2 ордера прогоняются через _order_has_book и существующую ветку исполнения.
    """
    decisions = decisions_json.get("decisions", [])
    if not isinstance(decisions, list):
        return []
    orders: List[Dict[str, Any]] = []

    for d in decisions:
        action = d.get("action")
        if action is None or str(action).upper() == "SKIP":
            continue
        action = str(action).upper()
        market_id = d.get("market_id")
        if not market_id:
            continue
        size_abs = float(d.get("size_abs", 0.0) or 0.0)
        if size_abs <= 0:
            continue
        if not _portfolio_can_open_notional(portfolio, market_id, size_abs):
            continue

        outcome = "YES"
        if action == "BUY_YES":
            outcome = "YES"
        elif action == "BUY_NO":
            outcome = "NO"
        elif action == "CLOSE":
            # CLOSE в текущей механике обрабатывается отдельно (check_stops / закрытие позиции)
            continue
        else:
            continue

        # limit_price, yes_token_id: из datafeed (в main дополняют yes_token_id и _order_has_book)
        limit_price: Optional[float] = None
        yes_token_id: Optional[str] = None
        if hasattr(datafeed, "markets") and market_id in datafeed.markets:
            m = datafeed.markets[market_id]
            limit_price = getattr(m, "yes_ask", None) or getattr(m, "yes_price", None)
            cids = getattr(m, "clob_token_ids", None)
            if cids and len(cids) >= 1:
                yes_token_id = get_yes_token_id(m) or cids[0]

        if limit_price is None or limit_price <= 0:
            limit_price = 0.50
        entry = max(0.02, min(0.99, limit_price))
        sl = max(0.01, entry * (1 - sl_pct))
        tp = min(0.99, entry * (1 + tp_pct))

        order: Dict[str, Any] = {
            "market_id": market_id,
            "outcome": outcome,
            "final_size_usd": round(size_abs, 2),
            "target_size_usd": round(size_abs, 2),
            "limit_price": round(entry, 4),
            "stop_loss_price": round(sl, 4),
            "take_profit_price": round(tp, 4),
        }
        if yes_token_id:
            order["yes_token_id"] = yes_token_id
        orders.append(order)

    return orders
