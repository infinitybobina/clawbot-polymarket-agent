#!/usr/bin/env python3
"""
Цены по открытым позициям через CLOB book.
Режим best_bid: для лонг-позиции (YES) используем только best_bid как «цена немедленного выхода» — продать можно по bid.
Состояния: NO_BOOK (get_orderbook вернул None), BOOK_NO_BID (bids=[]), OK (цена = best_bid).
Если bids == [] → price=None, SL/TP пропускаем (продать немедленно нельзя).
"""

import aiohttp
import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Минимальный интервал (сек), после которого книгу из clob_state считаем свежей для позиций
POSITION_BOOK_MAX_AGE_SEC = 30.0

CLOB_BOOK_URL = "https://clob.polymarket.com/book"


async def fetch_book(session: aiohttp.ClientSession, token_id: str) -> Optional[dict]:
    """GET /book?token_id=... Возвращает None при не-200, body с error или без bids/asks. Иначе dict с bids, asks (массивы)."""
    url = f"{CLOB_BOOK_URL}?token_id={token_id}"
    try:
        async with session.get(url) as resp:
            try:
                body = await resp.json()
            except Exception:
                return None
            if resp.status != 200:
                return None
            if isinstance(body, dict) and "error" in body:
                return None
            if not isinstance(body, dict) or "bids" not in body or "asks" not in body:
                return None
            if not isinstance(body.get("bids"), list) or not isinstance(body.get("asks"), list):
                return None
            return body
    except Exception:
        return None


def _parse_price(level) -> Optional[float]:
    """Уровень стакана: [price, size] или {"price": "0.45", "size": "100"}. None если не распарсить."""
    if isinstance(level, (list, tuple)) and len(level) >= 1:
        try:
            return float(level[0])
        except (TypeError, ValueError):
            return None
    if isinstance(level, dict) and "price" in level:
        try:
            return float(level["price"])
        except (TypeError, ValueError):
            return None
    return None


async def get_position_prices(
    token_ids: List[str],
    session: Optional[aiohttp.ClientSession] = None,
) -> Dict[str, Optional[float]]:
    """token_id -> yes_price (best_bid для выхода по YES) или None если нет стакана / пустой bids. session: переиспользуй datafeed.session."""
    if not token_ids:
        return {}
    prices: Dict[str, Optional[float]] = {}
    no_book = 0
    no_bid = 0
    own_session = session is None
    if session is None:
        session = aiohttp.ClientSession()
    try:
        tasks = [fetch_book(session, tid) for tid in token_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            tid = token_ids[i]
            if isinstance(result, Exception):
                no_book += 1
                prices[tid] = None
                continue
            book = result
            if book is None:
                no_book += 1
                prices[tid] = None
                continue
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if not bids:
                no_bid += 1
                prices[tid] = None  # продать немедленно нельзя → SL/TP пропускаем
                continue
            # YES/long (закреплённый режим): цена выхода = только best_bid — продать можно по bid
            best_bid = _parse_price(bids[0])
            if best_bid is None or best_bid <= 0:
                no_bid += 1
                prices[tid] = None
            else:
                prices[tid] = round(best_bid, 4)
                logger.debug("%s: best_bid=%.4f", tid[:16], prices[tid])

        ok = sum(1 for p in prices.values() if p is not None)
        logger.info("Position prices: ok=%d no_book=%d no_bid=%d total=%d", ok, no_book, no_bid, len(token_ids))
        if ok == 0:
            logger.info("skip risk checks: no prices")
        return prices
    finally:
        if own_session:
            await session.close()


def _best_bid_from_book(book: dict) -> Optional[float]:
    """Best bid из книги (для long YES = цена выхода)."""
    bids = book.get("bids") or []
    if not bids:
        return None
    return _parse_price(bids[0])


async def get_position_prices_by_market(
    market_ids: List[str],
    token_ids_by_market: Dict[str, str],
    session: Optional[aiohttp.ClientSession] = None,
    clob_state: Optional[Any] = None,
) -> Dict[str, Optional[float]]:
    """market_id -> yes_price (или None). session: datafeed.session. clob_state: при наличии берём цену из кеша (book), остальное — по сети."""
    if not token_ids_by_market or not market_ids:
        return {}
    token_ids = [token_ids_by_market[mid] for mid in market_ids if mid in token_ids_by_market]
    if not token_ids:
        return {}
    tid_to_price: Dict[str, Optional[float]] = {}
    to_fetch: List[str] = []
    from_cache = 0
    now = time.time()
    if clob_state and getattr(clob_state, "tokens", None):
        for tid in token_ids:
            st = clob_state.tokens.get(tid)
            if st and st.book and st.has_bid and (now - getattr(st, "last_book_ts", 0)) <= POSITION_BOOK_MAX_AGE_SEC:
                bid = _best_bid_from_book(st.book)
                tid_to_price[tid] = round(bid, 4) if bid is not None and bid > 0 else None
                if tid_to_price[tid] is not None:
                    from_cache += 1
            else:
                to_fetch.append(tid)
    else:
        to_fetch = list(token_ids)
    if to_fetch:
        fetched = await get_position_prices(to_fetch, session=session)
        tid_to_price.update(fetched)
    from_fetch = sum(1 for tid in to_fetch if tid_to_price.get(tid) is not None)
    mid_to_price: Dict[str, Optional[float]] = {}
    for mid in market_ids:
        tid = token_ids_by_market.get(mid)
        if tid and tid in tid_to_price:
            mid_to_price[mid] = tid_to_price[tid]
    # Лог: что кладём в position prices — best_bid из кеша (clob_state) или из fetch; пример по первому рынку
    sample = None
    for mid in market_ids:
        p = mid_to_price.get(mid)
        if p is not None:
            sample = (mid[:16] + ".." if len(mid) > 16 else mid, p)
            break
    logger.info(
        "Position prices by market: total=%d from_cache=%d from_fetch=%d; sample mid=%s best_bid=%s",
        len(mid_to_price), from_cache, from_fetch, sample[0] if sample else "-", sample[1] if sample else "-",
    )
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
