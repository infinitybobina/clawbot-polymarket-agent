"""
v2.0: реалтайм цены — WebSocket CLOB Polymarket или REST fallback.
Обновления каждые 1–10 сек; snapshot() для main loop.
"""

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL = 10


class PriceStream:
    """Локальное хранилище цен; обновляется из WebSocket или REST poll."""

    def __init__(self) -> None:
        self._prices: Dict[str, Dict[str, Any]] = {}  # market_id -> {yes_price, timestamp, ...}
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Актуальные цены всех рынков для strategy/risk. Возвращает копию."""
        return dict(self._prices)

    def update(self, market_id: str, yes_price: float) -> None:
        """Обновить цену по рынку (из WS или REST)."""
        self._prices[market_id] = {"yes_price": yes_price, "timestamp": time.time()}

    def update_batch(self, prices: Dict[str, float]) -> None:
        """Массовое обновление: market_id -> yes_price."""
        now = time.time()
        for mid, price in prices.items():
            self._prices[mid] = {"yes_price": price, "timestamp": now}

    # --- REST fallback: опрос каждые interval_sec ---
    async def start_rest(
        self,
        get_prices: Callable[[], Any],
        market_ids: List[str],
        interval_sec: float = 10.0,
    ) -> None:
        """Фоновый цикл: get_prices() возвращает dict market_id -> yes_price (или datafeed.markets)."""
        self._stop.clear()

        async def _loop() -> None:
            while not self._stop.is_set():
                try:
                    result = await get_prices()
                    if isinstance(result, dict) and result and isinstance(next(iter(result.values())), (int, float)):
                        self.update_batch(result)
                    else:
                        # datafeed.markets: market_id -> MarketSnapshot
                        for mid in market_ids:
                            if mid in result and hasattr(result[mid], "yes_price"):
                                self.update(mid, float(result[mid].yes_price))
                except Exception as e:
                    logger.warning("PriceStream REST poll error: %s", e)
                await asyncio.sleep(interval_sec)

        self._task = asyncio.create_task(_loop())
        logger.info("PriceStream REST started (interval=%.0fs, %d markets)", interval_sec, len(market_ids))

    # --- WebSocket: подписка по token IDs ---
    async def start_ws(
        self,
        asset_ids: List[str],
        market_id_by_asset: Optional[Dict[str, str]] = None,
    ) -> None:
        """Подписка на CLOB WebSocket; assets_ids = YES token IDs из Gamma clobTokenIds."""
        self._stop.clear()
        market_by_asset = market_id_by_asset or {}

        async def _run_ws() -> None:
            try:
                import aiohttp
            except ImportError:
                logger.warning("aiohttp required for WebSocket; use REST fallback")
                return
            while not self._stop.is_set():
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.ws_connect(WS_URL) as ws:
                            sub = {"assets_ids": asset_ids, "type": "market", "custom_feature_enabled": True}
                            await ws.send_str(json.dumps(sub))
                            last_ping = time.time()
                            async for msg in ws:
                                if self._stop.is_set():
                                    break
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    try:
                                        data = json.loads(msg.data)
                                    except json.JSONDecodeError:
                                        continue
                                    et = data.get("event_type")
                                    market = data.get("market")
                                    if et == "best_bid_ask" and market:
                                        bid = float(data.get("best_bid") or 0)
                                        ask = float(data.get("best_ask") or 0)
                                        price = (bid + ask) / 2 if (bid and ask) else (bid or ask)
                                        if price > 0:
                                            mid = market_by_asset.get(data.get("asset_id", ""), market)
                                            self.update(mid, price)
                                    elif et == "price_change" and market:
                                        for pc in data.get("price_changes", []):
                                            price = float(pc.get("best_bid") or pc.get("best_ask") or pc.get("price") or 0)
                                            if price > 0:
                                                aid = pc.get("asset_id", "")
                                                mid = market_by_asset.get(aid, market)
                                                self.update(mid, price)
                                    elif et == "last_trade_price":
                                        price = float(data.get("price") or 0)
                                        if price > 0 and market:
                                            mid = market_by_asset.get(data.get("asset_id", ""), market)
                                            self.update(mid, price)
                                if time.time() - last_ping >= PING_INTERVAL:
                                    await ws.send_str("PING")
                                    last_ping = time.time()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning("PriceStream WS error: %s", e)
                    await asyncio.sleep(5)

        self._task = asyncio.create_task(_run_ws())
        logger.info("PriceStream WebSocket started (%d assets)", len(asset_ids))

    async def stop(self) -> None:
        """Остановить фоновый цикл."""
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
