#!/usr/bin/env python3
"""Получить yes_token_id для рынков Filecoin из Gamma API."""
import json
import urllib.request

url = "https://gamma-api.polymarket.com/markets?category=crypto&limit=200"
req = urllib.request.Request(url, headers={"User-Agent": "ClawBot/1.0 (polymarket)"})
with urllib.request.urlopen(req, timeout=15) as r:
    data = json.loads(r.read().decode())

for m in data:
    slug = (m.get("slug") or "").lower()
    if "filecoin" in slug or "fil " in slug or " fil" in slug:
        cids = m.get("clobTokenIds")
        if isinstance(cids, str):
            import ast
            cids = ast.literal_eval(cids) if cids else []
        if cids:
            print("slug:", m.get("slug"))
            print("conditionId:", m.get("conditionId"))
            print("yes_token_id:", cids[0])
            print("---")
