"""
Block 1: Data Feed
Gamma API + CLOB WebSocket (fixed)
"""

import asyncio
import aiohttp
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
    question: str = ""


class ClawBotDataFeed:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.markets: Dict[str, MarketSnapshot] = {}
        self.gamma_base = "https://gamma-api.polymarket.com"

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
        return None

    async def fetch_politics_markets(self) -> List[MarketSnapshot]:
        """Gamma REST: топ рынки Politics с volume > 1M"""
        url = f"{self.gamma_base}/markets?active=true&category=politics&limit=50&offset=0"
        async with self.session.get(url) as resp:
            data = await resp.json()
        raw_list = data if isinstance(data, list) else data.get("markets", [])
        markets = []
        for m in raw_list:
            vol = m.get("volume24h") or m.get("volume24hr") or m.get("volume") or m.get("volumeNum") or 0
            vol_f = float(vol) if vol else 0
            if vol_f > 1000000:
                prices_raw = m.get("outcomePrices") or "[\"0\", \"0\"]"
                if isinstance(prices_raw, str):
                    try:
                        prices = json.loads(prices_raw)
                    except json.JSONDecodeError:
                        prices = ["0", "0"]
                else:
                    prices = prices_raw
                yes_p = float(prices[0]) if len(prices) > 0 else 0.0
                no_p = float(prices[1]) if len(prices) > 1 else 0.0
                markets.append(MarketSnapshot(
                    market_id=str(m.get("conditionId") or m.get("id") or ""),
                    yes_price=yes_p,
                    no_price=no_p,
                    spread=abs(yes_p - no_p),
                    volume_usd=vol_f,
                    category=str(m.get("category") or ""),
                    question=str(m.get("question") or "")
                ))
        self.markets.update({m.market_id: m for m in markets})
        logger.info(f"Fetched {len(markets)} politics markets")
        return markets

    def get_top_mispricing(self, top_n: int = 5) -> List[MarketSnapshot]:
        """Топ N рынков по спреду для стратегии"""
        return sorted(
            self.markets.values(),
            key=lambda m: m.spread,
            reverse=True
        )[:top_n]


async def test_datafeed():
    """Тест Data Feed"""
    async with ClawBotDataFeed() as feed:
        markets = await feed.fetch_politics_markets()
        top = feed.get_top_mispricing(5)
        logger.info(f"Top {len(top)} candidates")
        return top


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(test_datafeed())
