#!/usr/bin/env python3
"""Найти активный рынок и вывести conditionId + yes_token_id. Для подстановки в portfolio_state."""
import json
import urllib.request

# Сначала попробуем получить рынки по conditionId текущих позиций
positions = [
    "0xb2eecb8d14e871c5b82a3b037fc5f8b703c218e41aa578c8e870244585b9db78",
    "0x7333b6e016f7f60d86f15f11ed0b41b69deec0b6d73b86933639b1f39a545d87",
]
headers = {"User-Agent": "ClawBot/1.0 (polymarket)"}

# Активные рынки politics (точно есть стакан)
url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=50"
req = urllib.request.Request(url, headers=headers)
with urllib.request.urlopen(req, timeout=15) as r:
    data = json.loads(r.read().decode())

def parse_cids(cids):
    if isinstance(cids, list) and cids:
        return cids[0]
    if isinstance(cids, str) and cids:
        import ast
        try:
            arr = ast.literal_eval(cids)
            return arr[0] if arr else None
        except Exception:
            return None
    return None

# Ищем активный рынок с ценой в разумном диапазоне (есть ликвидность)
found = None
for m in data:
    if m.get("closed") is True:
        continue
    cid = parse_cids(m.get("clobTokenIds"))
    if not cid:
        continue
    prices = m.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices) if "[" in prices else []
        except Exception:
            prices = []
    if isinstance(prices, list) and len(prices) >= 1:
        try:
            p = float(prices[0])
            if 0.02 < p < 0.98:
                found = {"conditionId": m.get("conditionId"), "slug": m.get("slug"), "yes_token_id": cid, "yes_price": p}
                break
        except (TypeError, ValueError):
            pass
if not found:
    found = {"conditionId": data[0].get("conditionId"), "slug": data[0].get("slug"), "yes_token_id": parse_cids(data[0].get("clobTokenIds")), "yes_price": 0.5}

print(json.dumps(found, indent=2))
