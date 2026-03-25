"""
Cooldown after ENTRY: prevent re-entering the same market for N ticks.

Stored as { market_id: ticks_left } in reentry_cooldown.json and ticked down every loop tick.
This is intentionally time-based (via ticks), so it persists across restarts and blocks rapid churn
in the same market.
"""

import json
import logging
import os
from typing import Dict, Set, List

logger = logging.getLogger(__name__)

COOLDOWN_FILENAME = "reentry_cooldown.json"


def _cooldown_path(root: str) -> str:
    return os.path.join(root, COOLDOWN_FILENAME)


def load_reentry_cooldown(root: str) -> Dict[str, int]:
    """Load { market_id: ticks_left } from reentry_cooldown.json."""
    path = _cooldown_path(root)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: int(v) for k, v in data.items() if int(v) > 0}
    except Exception as e:
        logger.warning("Failed to load re-entry cooldown: %s", e)
        return {}


def save_reentry_cooldown(root: str, cooldown: Dict[str, int]) -> None:
    """Save cooldown to reentry_cooldown.json (only ticks_left > 0)."""
    path = _cooldown_path(root)
    try:
        out = {k: v for k, v in cooldown.items() if int(v) > 0}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
    except Exception as e:
        logger.warning("Failed to save re-entry cooldown: %s", e)


def tick_reentry_cooldown(cooldown: Dict[str, int]) -> Dict[str, int]:
    """Decrement ticks_left by 1 for all markets; drop expired."""
    return {k: v - 1 for k, v in cooldown.items() if int(v) > 1}


def add_to_reentry_cooldown(cooldown: Dict[str, int], market_ids: List[str], ticks: int) -> Dict[str, int]:
    """Add markets to cooldown for `ticks` ticks."""
    if ticks <= 0:
        return cooldown
    for mid in market_ids:
        if mid:
            cooldown[mid] = int(ticks)
    return cooldown


def get_reentry_cooldown_set(cooldown: Dict[str, int]) -> Set[str]:
    return set(cooldown.keys())

