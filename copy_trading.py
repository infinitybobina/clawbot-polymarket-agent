#!/usr/bin/env python3
"""
Copy-trading signal adapter (paper-safe).

Reads external "leader wallet" ideas from JSON and converts them to
internal BUY-YES signals compatible with RiskManager.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Dict, Any, List, Tuple

logger = logging.getLogger(__name__)


def _load_raw(path: str) -> List[Dict[str, Any]]:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except Exception as e:
        logger.warning("copy_trading: failed to read %s: %s", path, e)
        return []
    if isinstance(obj, dict):
        sigs = obj.get("signals", [])
    else:
        sigs = obj
    if not isinstance(sigs, list):
        return []
    return [s for s in sigs if isinstance(s, dict)]


def build_copy_signals(
    *,
    root_dir: str,
    copy_cfg: Dict[str, Any],
    markets_by_id: Dict[str, Any],
    exclude_ids: set,
    sl_pct: float,
    tp_pct: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    Convert external copy ideas into strategy signals.

    Input format (json file):
    {
      "signals": [
        {"market_id": "...", "wallet": "0x...", "weight": 1.0, "max_entry_price": 0.48}
      ]
    }
    """
    stats = {
        "raw": 0,
        "kept": 0,
        "missing_market": 0,
        "excluded": 0,
        "entry_too_high": 0,
        "bad_record": 0,
    }
    out: List[Dict[str, Any]] = []

    rel_path = str(copy_cfg.get("signals_file") or "copy_signals.json")
    abs_path = rel_path if os.path.isabs(rel_path) else os.path.join(root_dir, rel_path)
    raw = _load_raw(abs_path)
    stats["raw"] = len(raw)
    if not raw:
        return out, stats

    base_size = float(copy_cfg.get("base_size_usd", 300.0))
    min_ev = float(copy_cfg.get("min_expected_ev", 0.04))
    default_max_entry = float(copy_cfg.get("max_entry_price", 0.50))
    default_weight = float(copy_cfg.get("default_weight", 1.0))

    for r in raw:
        try:
            market_id = str(r.get("market_id") or "").strip()
            if not market_id:
                stats["bad_record"] += 1
                continue
            if market_id in exclude_ids:
                stats["excluded"] += 1
                logger.info(
                    "copy_trading: excluded market_id=%s (open position or SL/TP/re-entry cooldown in exclude_ids)",
                    (market_id[:20] + "..") if len(market_id) > 20 else market_id,
                )
                continue
            snap = markets_by_id.get(market_id)
            if not snap:
                stats["missing_market"] += 1
                continue

            yes_ask = getattr(snap, "yes_ask", None)
            yes_px = getattr(snap, "yes_price", None)
            if yes_ask is not None:
                entry = float(yes_ask)
            elif yes_px is not None:
                entry = float(yes_px)
            else:
                stats["bad_record"] += 1
                continue

            max_entry = float(r.get("max_entry_price", default_max_entry))
            if entry > max_entry:
                stats["entry_too_high"] += 1
                continue

            weight = float(r.get("weight", default_weight))
            expected_ev = max(min_ev, min_ev * max(0.5, weight))
            sl = max(0.01, entry * (1.0 - sl_pct))
            tp = min(0.99, entry * (1.0 + tp_pct))
            if sl >= entry:
                sl = round(entry - 0.01, 4)
            if tp <= entry:
                tp = min(0.99, round(entry + 0.01, 4))

            wallet = str(r.get("wallet") or "").strip()
            rationale = "copy-trade signal"
            if wallet:
                rationale = f"copy-trade wallet={wallet[:10]}..."

            out.append(
                {
                    "signal_id": str(uuid.uuid4()),
                    "market_id": market_id,
                    "side": "buy",
                    "outcome": "YES",
                    "limit_price": round(entry, 4),
                    "target_size_usd": round(base_size, 2),
                    "expected_ev": round(expected_ev, 4),
                    "confidence": "medium",
                    "rationale": rationale,
                    "stop_loss_price": round(sl, 4),
                    "take_profit_price": round(tp, 4),
                    "source": "copy",
                }
            )
            stats["kept"] += 1
        except Exception:
            stats["bad_record"] += 1

    return out, stats
