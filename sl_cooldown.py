"""
Cooldown после закрытия по Stop Loss или Take Profit: не открывать позицию по этому рынку N запусков.
"""

import json
import logging
import os
from typing import Dict, Set, List

logger = logging.getLogger(__name__)

COOLDOWN_FILENAME = "sl_cooldown.json"
TP_COOLDOWN_FILENAME = "tp_cooldown.json"
DEFAULT_RUNS = 20
DEFAULT_TP_RUNS = 5


def _cooldown_path(root: str, filename: str = COOLDOWN_FILENAME) -> str:
    return os.path.join(root, filename)


def load_cooldown(root: str) -> Dict[str, int]:
    """Загрузить { market_id: runs_left } из sl_cooldown.json."""
    path = _cooldown_path(root, COOLDOWN_FILENAME)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: int(v) for k, v in data.items() if int(v) > 0}
    except Exception as e:
        logger.warning("Failed to load SL cooldown: %s", e)
        return {}


def save_cooldown(root: str, cooldown: Dict[str, int]) -> None:
    """Сохранить cooldown в sl_cooldown.json (только runs_left > 0)."""
    path = _cooldown_path(root, COOLDOWN_FILENAME)
    try:
        out = {k: v for k, v in cooldown.items() if v > 0}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
    except Exception as e:
        logger.warning("Failed to save SL cooldown: %s", e)


def tick_cooldown(cooldown: Dict[str, int]) -> Dict[str, int]:
    """Уменьшить runs_left на 1 у всех, убрать <= 0. Возвращает новый словарь."""
    return {k: v - 1 for k, v in cooldown.items() if v > 1}


def add_to_cooldown(cooldown: Dict[str, int], market_ids: List[str], runs: int = DEFAULT_RUNS) -> Dict[str, int]:
    """Добавить рынки в cooldown на runs запусков. Возвращает обновлённый словарь."""
    for mid in market_ids:
        if mid:
            cooldown[mid] = runs
    return cooldown


def get_cooldown_set(cooldown: Dict[str, int]) -> Set[str]:
    """Множество market_id, по которым действует cooldown."""
    return set(cooldown.keys())


# --- TP cooldown (отдельный файл) ---

def load_tp_cooldown(root: str) -> Dict[str, int]:
    """Загрузить { market_id: runs_left } из tp_cooldown.json."""
    path = _cooldown_path(root, TP_COOLDOWN_FILENAME)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: int(v) for k, v in data.items() if int(v) > 0}
    except Exception as e:
        logger.warning("Failed to load TP cooldown: %s", e)
        return {}


def save_tp_cooldown(root: str, cooldown: Dict[str, int]) -> None:
    """Сохранить cooldown в tp_cooldown.json (только runs_left > 0)."""
    path = _cooldown_path(root, TP_COOLDOWN_FILENAME)
    try:
        out = {k: v for k, v in cooldown.items() if v > 0}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
    except Exception as e:
        logger.warning("Failed to save TP cooldown: %s", e)
