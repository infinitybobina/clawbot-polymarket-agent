"""
Метка времени старта текущего процесса бота — для кода и для внешних скриптов.

Файл рядом с репозиторием: bot_session.json (перезаписывается при каждом запуске main_v2).
Внутри процесса: импортировать get_started_at_utc / session_elapsed_seconds.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SESSION_FILENAME = "bot_session.json"

_started_at_utc: Optional[datetime] = None
_started_monotonic: Optional[float] = None


def record_session_start(root: str) -> Dict[str, Any]:
    """Зафиксировать момент старта сессии (один раз за запуск main_loop)."""
    global _started_at_utc, _started_monotonic
    now = datetime.now(timezone.utc)
    _started_at_utc = now.replace(microsecond=0)  # стабильная секунда в ISO/логах
    _started_monotonic = time.monotonic()
    payload = {
        "started_at_utc": _started_at_utc.isoformat().replace("+00:00", "Z"),
        "pid": os.getpid(),
    }
    path = os.path.join(os.path.abspath(root), SESSION_FILENAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except OSError as e:
        logger.warning("bot_session.json write failed: %s", e)
    return payload


def get_started_at_utc() -> Optional[datetime]:
    """Время старта текущего процесса (UTC), если уже вызван record_session_start."""
    return _started_at_utc


def get_started_at_iso_z() -> Optional[str]:
    """ISO-8601 с суффиксом Z, как в bot_session.json."""
    if _started_at_utc is None:
        return None
    return _started_at_utc.isoformat().replace("+00:00", "Z")


def session_elapsed_seconds() -> Optional[float]:
    """Прошло секунд с момента record_session_start (monotonic, без скачков часов)."""
    if _started_monotonic is None:
        return None
    return time.monotonic() - _started_monotonic


def load_session_file(root: str) -> Optional[Dict[str, Any]]:
    """Прочитать bot_session.json с диска (удобно из другого процесса)."""
    path = os.path.join(os.path.abspath(root), SESSION_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
