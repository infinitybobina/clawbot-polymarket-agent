#!/usr/bin/env python3
"""
Block 1: Data Feed
- Gamma REST: discovery рынков (Politics, volume > 1M)
- CLOB WebSocket: live стаканы (YES/NO цены, спред)
"""

import asyncio
import aiohttp
import websockets
import json
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class MarketSnapshot:
    market_id: str
    yes_price: float
    no_price: float
    spread: float
    volume_usd: float
    category: str

class ClawBotDataFeed:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.ws = None
        self.markets: Dict[str, MarketSnapshot] = {}
        self.gamma_base = "https://gamma.api.polymarket.com"  # [web:1]
        self.clob_ws = "wss://ws-subscriptions-clob.polymarket.com"  # [web:9]

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def fetch_politics_markets(self) -> List[MarketSnapshot]:
        """Gamma REST: топ рынки Politics с volume > 1M"""
        url = f"{self.gamma_base}/markets?active=true&category=politics&limit=50&offset=0"
        async with self.session.get(url) as resp:
            data = await resp.json()
            markets = []
            for m in data.get("markets", []):
                if m.get("volume24h", 0) > 1000000:  # 1M USD
                    markets.append(MarketSnapshot(
                        market_id=m["id"],
                        yes_price=m["outcomes"][0]["price"],  # YES
                        no_price=m["outcomes"][1]["price"],   # NO
                        spread=abs(m["outcomes"][0]["price"] - m["outcomes"][1]["price"]),
                        volume_usd=m["volume24h"],
                        category=m["category"]
                    ))
            self.markets.update({m.market_id: m for m in markets})
            logger.info(f"Fetched {len(markets)} politics markets")
            return markets

    async def connect_clob_ws(self):
        """CLOB WebSocket: live обновления стаканов"""
        async with websockets.connect(self.clob_ws) as ws:
            # Подписка на топ рынки
            subscribe_msg = {
                "type": "subscribe",
                "topics": [f"market/{market_id}" for market_id in self.markets.keys()]
            }
            await ws.send(json.dumps(subscribe_msg))
            
            async for message in ws:
                data = json.loads(message)
                if data.get("type") == "orderbook_update":
                    market_id = data["market_id"]
                    if market_id in self.markets:
                        self.markets[market_id].yes_price = data["yes_bid"]  # live цены
                        self.markets[market_id].no_price = data["no_ask"]
                        logger.info(f"Updated {market_id}: YES={data['yes_bid']:.3f}")

    async def get_top_mispricing(self, top_n: int = 5) -> List[MarketSnapshot]:
        """Для Strategy: топ рынки по спреду (возможный edge)"""
        return sorted(
            self.markets.values(),
            key=lambda m: m.spread,
            reverse=True
        )[:top_n]

# Тест
async def test_datafeed():
    async with ClawBotDataFeed() as feed:
        markets = await feed.fetch_politics_markets()
        print(f"Top 3: {[m.market_id for m in markets[:3]]}")
        # await feed.connect_clob_ws()  # раскомменти для live

if __name__ == "__main__":
    asyncio.run(test_datafeed())
    