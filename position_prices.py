#!/usr/bin/env python3
"""
Цены по открытым позициям через CLOB book (mid = (bid+ask)/2).
Вызывается каждые 10s для list(positions.keys()); yes_token_id хранится в позиции при открытии.
"""

import aiohttp
import asyncio
import logging
import time
from typing import Dict, List

logger = logging.getLogger(__name__)

CLOB_BOOK_URL = "https://clob.polymarket.com/book"


async def fetch_book(session: aiohttp.ClientSession, token_id: str) -> dict:
    """GET /book?token_id=..."""
    url = f"{CLOB_BOOK_URL}?token_id={token_id}"
    async with session.get(url) as resp:
        return await resp.json()


def _parse_price(level) -> float:
    """Уровень стакана: [price, size] или {"price": "0.45", "size": "100"}."""
    if isinstance(level, (list, tuple)) and len(level) >= 1:
        return float(level[0])
    if isinstance(level, dict) and "price" in level:
        return float(level["price"])
    return 0.0


async def get_position_prices(token_ids: List[str]) -> Dict[str, float]:
    """token_id -> yes_price (mid из book)."""
    if not token_ids:
        return {}
    prices = {}
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_book(session, tid) for tid in token_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            tid = token_ids[i]
            if isinstance(result, dict) and result.get("bids") is not None and result.get("asks") is not None:
                bids, asks = result["bids"], result["asks"]
                bid = _parse_price(bids[0]) if bids else 0.0
                ask = _parse_price(asks[0]) if asks else 1.0
                if bid <= 0 and ask <= 0:
                    prices[tid] = 0.5
                else:
                    mid = (bid + ask) / 2 if (bid and ask) else (bid or ask)
                    prices[tid] = round(mid, 4)
                logger.debug("%s: bid=%.4f ask=%.4f mid=%.4f", tid[:16], bid, ask, prices[tid])
            else:
                logger.warning("Book fail %s: %s", tid[:16], result)
                prices[tid] = 0.5  # fallback

    ok = len([p for p in prices.values() if p > 0 and p < 1])
    logger.info("Position prices: %d/%d ok", ok, len(token_ids))
    return prices


async def get_position_prices_by_market(
    market_ids: List[str],
    token_ids_by_market: Dict[str, str],
) -> Dict[str, float]:
    """market_id -> yes_price. Вызов CLOB по token_id и обратное отображение."""
    if not token_ids_by_market or not market_ids:
        return {}
    token_ids = [token_ids_by_market[mid] for mid in market_ids if mid in token_ids_by_market]
    if not token_ids:
        return {}
    tid_to_price = await get_position_prices(token_ids)
    mid_to_price = {}
    for mid in market_ids:
        tid = token_ids_by_market.get(mid)
        if tid and tid in tid_to_price:
            mid_to_price[mid] = tid_to_price[tid]
    return mid_to_price


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG)
    token_id = sys.argv[1] if len(sys.argv) > 1 else None
    if not token_id:
        print("Usage: python position_prices.py <token_id>")
        print("Example (Filecoin YES token): python position_prices.py 21215480146251559775526450497952933039230622804728164945422855146438385145625")
        sys.exit(1)
    out = asyncio.run(get_position_prices([token_id]))
    print("Result:", out)
