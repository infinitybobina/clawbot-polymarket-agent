"""
Block 1: Data Feed
Gamma API + CLOB WebSocket (fixed)
"""

import asyncio
import aiohttp
import json
import logging
from datetime import datetime, timezone
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
    # Для crypto/мелких рынков: лимит размера заявки от объёма и глубины
    end_date_iso: Optional[str] = None
    seconds_to_resolution: Optional[float] = None
    liquidity_usd: float = 0.0


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

    async def fetch_politics_markets(self, min_volume_usd: float = 500_000) -> List[MarketSnapshot]:
        """Gamma REST: топ рынки Politics с volume > min_volume_usd (по умолчанию 500k — больше кандидатов)."""
        url = f"{self.gamma_base}/markets?active=true&category=politics&limit=50&offset=0"
        async with self.session.get(url) as resp:
            data = await resp.json()
        raw_list = data if isinstance(data, list) else data.get("markets", [])
        markets = []
        for m in raw_list:
            vol = m.get("volume24h") or m.get("volume24hr") or m.get("volume") or m.get("volumeNum") or 0
            vol_f = float(vol) if vol else 0
            if vol_f >= min_volume_usd:
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

    async def fetch_crypto_markets(
        self,
        min_volume_usd: float = 10_000,
        min_liquidity_usd: float = 1_000,
        max_hours_to_resolution: float = 1.0,
        limit: int = 50,
    ) -> List[MarketSnapshot]:
        """Рынки Crypto с фильтром по времени до экспирации (1h), объёму и ликвидности.
        Чтобы наша заявка не двигала рынок: дальше в main размер ограничивают по объёму/глубине."""
        url = f"{self.gamma_base}/markets?active=true&closed=false&category=crypto&limit={limit}"
        async with self.session.get(url) as resp:
            data = await resp.json()
        raw_list = data if isinstance(data, list) else data.get("markets", [])
        now = datetime.now(timezone.utc)
        max_seconds = max_hours_to_resolution * 3600
        markets = []
        for m in raw_list:
            cat = (m.get("category") or "").strip().lower()
            if cat != "crypto":
                continue
            vol = m.get("volume24h") or m.get("volume24hr") or m.get("volume") or m.get("volumeNum") or 0
            vol_f = float(vol) if vol else 0
            if vol_f < min_volume_usd:
                continue
            liq = m.get("liquidityNum") or m.get("liquidity") or 0
            liq_f = float(liq) if liq else 0
            if liq_f < 0:
                liq_f = 0
            if min_liquidity_usd > 0 and liq_f < min_liquidity_usd:
                continue
            end_d = m.get("endDate") or m.get("endDateIso") or ""
            if not end_d:
                continue
            try:
                if "T" in str(end_d):
                    end_dt = datetime.fromisoformat(str(end_d).replace("Z", "+00:00"))
                else:
                    end_dt = datetime.fromisoformat(str(end_d) + "T23:59:59+00:00")
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                sec_to = (end_dt - now).total_seconds()
            except Exception:
                sec_to = None
            if sec_to is not None and (sec_to <= 0 or sec_to > max_seconds):
                continue
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
            snap = MarketSnapshot(
                market_id=str(m.get("conditionId") or m.get("id") or ""),
                yes_price=yes_p,
                no_price=no_p,
                spread=abs(yes_p - no_p),
                volume_usd=vol_f,
                category=str(m.get("category") or "crypto"),
                question=str(m.get("question") or ""),
                end_date_iso=end_d if isinstance(end_d, str) else None,
                seconds_to_resolution=sec_to if sec_to is not None else None,
                liquidity_usd=liq_f,
            )
            markets.append(snap)
        self.markets.update({m.market_id: m for m in markets})
        logger.info("Fetched %d crypto markets (resolution <= %.1fh, vol >= %.0f, liq >= %.0f)",
                    len(markets), max_hours_to_resolution, min_volume_usd, min_liquidity_usd)
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
