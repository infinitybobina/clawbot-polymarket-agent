"""
Сохранение/загрузка состояния портфеля между запусками.
Чтобы при каждом часе не получать один и тот же сигнал (уже купленный рынок учитывается).
"""

import json
import logging
import os
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

STATE_FILENAME = "portfolio_state.json"


def _state_path(root: str) -> str:
    return os.path.join(root, STATE_FILENAME)


def load_state(root: str) -> Tuple[Optional[float], Optional[Dict[str, Dict[str, Any]]]]:
    """Загрузить balance и positions из portfolio_state.json. Возвращает (balance, positions) или (None, None)."""
    path = _state_path(root)
    if not os.path.isfile(path):
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        balance = float(data.get("balance", 0))
        positions = data.get("positions", {})
        logger.info("Portfolio state loaded: balance=%.2f, positions=%d", balance, len(positions))
        return balance, positions
    except Exception as e:
        logger.warning("Failed to load portfolio state: %s", e)
        return None, None


def save_state(root: str, balance: float, positions: Dict[str, Dict[str, Any]]) -> None:
    """Сохранить balance и positions в portfolio_state.json."""
    path = _state_path(root)
    try:
        # positions: только сериализуемые поля
        out = {
            "balance": round(balance, 2),
            "positions": {},
        }
        for mid, p in positions.items():
            rec = {
                "outcome": str(p.get("outcome", "YES")),
                "size_tokens": round(float(p.get("size_tokens", 0)), 4),
                "avg_price": round(float(p.get("avg_price", 0)), 6),
            }
            if p.get("stop_loss_price") is not None:
                rec["stop_loss_price"] = round(float(p["stop_loss_price"]), 4)
            if p.get("take_profit_price") is not None:
                rec["take_profit_price"] = round(float(p["take_profit_price"]), 4)
            out["positions"][mid] = rec
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        logger.info("Portfolio state saved: balance=%.2f, positions=%d", balance, len(positions))
    except Exception as e:
        logger.warning("Failed to save portfolio state: %s", e)
