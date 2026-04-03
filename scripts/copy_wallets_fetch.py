#!/usr/bin/env python3
"""
Build copy_signals.json from file or HTTP API (A2A-friendly bridge).

This script is intentionally generic: your external service can output almost any
JSON shape as long as records include market_id (or condition_id) and optionally
wallet/weight/max_entry_price.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Iterable, List
from urllib.request import Request, urlopen


def _load_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _fetch_json_url(url: str, token: str = "") -> Any:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    with urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _iter_records(obj: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(obj, list):
        for x in obj:
            if isinstance(x, dict):
                yield x
        return
    if not isinstance(obj, dict):
        return

    # Common containers used by APIs/services.
    for key in ("signals", "items", "data", "results", "trades", "positions", "events"):
        val = obj.get(key)
        if isinstance(val, list):
            for x in val:
                if isinstance(x, dict):
                    yield x
            return
        if isinstance(val, dict):
            for x in _iter_records(val):
                yield x
            return

    # Fallback: treat top-level dict as single record when it looks signal-like.
    if any(k in obj for k in ("market_id", "condition_id", "wallet", "address")):
        yield obj


def _parse_ts_maybe(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(float(v), tz=timezone.utc)
        except Exception:
            return None
    s = str(v).strip()
    if not s:
        return None
    # Accept ISO and ISO with trailing Z.
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_record(r: Dict[str, Any]) -> Dict[str, Any] | None:
    market_id = (
        r.get("market_id")
        or r.get("condition_id")
        or r.get("market")
        or r.get("id")
    )
    if not market_id:
        return None
    wallet = r.get("wallet") or r.get("address") or r.get("trader")
    # "weight" can come as score/follow_weight/confidence.
    weight = r.get("weight", r.get("follow_weight", r.get("score", 1.0)))
    max_entry = r.get("max_entry_price", r.get("entry_cap", r.get("max_price")))
    ts = r.get("ts") or r.get("timestamp") or r.get("created_at") or r.get("time")
    return {
        "market_id": str(market_id).strip(),
        "wallet": str(wallet).strip() if wallet is not None else "",
        "weight": weight,
        "max_entry_price": max_entry,
        "ts": ts,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Build copy_signals.json for bot copy_trading adapter")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-file", dest="from_file", help="Source JSON file path")
    src.add_argument("--from-url", dest="from_url", help="Source API URL")

    p.add_argument("--token", default=os.environ.get("A2A_TOKEN", ""), help="Bearer token for --from-url")
    p.add_argument("--leaders-file", default="", help="Optional JSON list of allowed wallet addresses")
    p.add_argument("--max-age-min", type=int, default=180, help="Drop signals older than this many minutes (0 disables)")
    p.add_argument("--max-signals", type=int, default=25, help="Cap output signals")
    p.add_argument("--default-weight", type=float, default=1.0, help="Fallback weight")
    p.add_argument("--default-max-entry", type=float, default=0.50, help="Fallback max_entry_price")
    p.add_argument("--output", default="copy_signals.json", help="Output file")
    p.add_argument("--dry-run", action="store_true", help="Do not write output, print summary only")
    args = p.parse_args()

    try:
        src_obj = _load_json_file(args.from_file) if args.from_file else _fetch_json_url(args.from_url, args.token)
    except Exception as e:
        print(f"ERROR: source read failed: {e}", file=sys.stderr)
        return 2

    allow_wallets = set()
    if args.leaders_file:
        try:
            leaders = _load_json_file(args.leaders_file)
            if isinstance(leaders, dict):
                leaders = leaders.get("wallets", [])
            if isinstance(leaders, list):
                allow_wallets = {str(x).strip().lower() for x in leaders if str(x).strip()}
        except Exception as e:
            print(f"WARNING: leaders file ignored: {e}", file=sys.stderr)

    now_utc = datetime.now(timezone.utc)
    age_limit = timedelta(minutes=max(0, args.max_age_min))
    stats = {"raw": 0, "normalized": 0, "dropped_old": 0, "dropped_wallet": 0, "dropped_bad": 0, "kept": 0}
    out: List[Dict[str, Any]] = []

    for rec in _iter_records(src_obj):
        stats["raw"] += 1
        n = _normalize_record(rec)
        if not n or not n["market_id"]:
            stats["dropped_bad"] += 1
            continue
        stats["normalized"] += 1

        if allow_wallets:
            w = (n.get("wallet") or "").lower()
            if not w or w not in allow_wallets:
                stats["dropped_wallet"] += 1
                continue

        if args.max_age_min > 0:
            ts = _parse_ts_maybe(n.get("ts"))
            if ts is not None and (now_utc - ts) > age_limit:
                stats["dropped_old"] += 1
                continue

        try:
            weight = float(n.get("weight") if n.get("weight") is not None else args.default_weight)
        except Exception:
            weight = args.default_weight
        try:
            max_entry = float(n.get("max_entry_price") if n.get("max_entry_price") is not None else args.default_max_entry)
        except Exception:
            max_entry = args.default_max_entry

        out.append(
            {
                "market_id": n["market_id"],
                "wallet": n.get("wallet", ""),
                "weight": round(max(0.1, weight), 4),
                "max_entry_price": round(min(0.99, max(0.01, max_entry)), 4),
            }
        )

    # Deduplicate by market_id: keep highest weight.
    by_market: Dict[str, Dict[str, Any]] = {}
    for s in out:
        mid = s["market_id"]
        prev = by_market.get(mid)
        if prev is None or float(s.get("weight", 0)) >= float(prev.get("weight", 0)):
            by_market[mid] = s

    out = sorted(by_market.values(), key=lambda x: float(x.get("weight", 0)), reverse=True)[: max(1, args.max_signals)]
    stats["kept"] = len(out)
    payload = {"signals": out}

    print(
        "copy_wallets_fetch: "
        f"raw={stats['raw']} normalized={stats['normalized']} kept={stats['kept']} "
        f"dropped_old={stats['dropped_old']} dropped_wallet={stats['dropped_wallet']} dropped_bad={stats['dropped_bad']}"
    )
    if out:
        top = ", ".join(f"{x['market_id'][:12]}..(w={x['weight']})" for x in out[:5])
        print(f"top signals: {top}")

    if args.dry_run:
        return 0

    try:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"ERROR: failed writing {args.output}: {e}", file=sys.stderr)
        return 3

    print(f"written: {args.output} ({len(out)} signals)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
