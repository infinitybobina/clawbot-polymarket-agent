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
logging.getLogger(__name__).setLevel(logging.INFO)


@dataclass
class MarketSnapshot:
    market_id: str
    yes_price: float
    no_price: float
    spread: float
    volume_usd: float
    category: str
    question: str = ""
    # Бинарный (2 исхода) vs мультиисходный (3+). Сейчас работаем только с бинарными; мульти — ветка позже.
    outcomes_count: int = 2
    # Для crypto/мелких рынков: лимит размера заявки от объёма и глубины
    end_date_iso: Optional[str] = None
    seconds_to_resolution: Optional[float] = None
    liquidity_usd: float = 0.0
    # v2.0 WebSocket: [yes_token_id, no_token_id] из Gamma clobTokenIds
    clob_token_ids: Optional[List[str]] = None


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

    async def fetch_category_markets(
        self,
        category: str,
        min_volume_usd: float = 100_000,
        limit: int = 50,
    ) -> List[MarketSnapshot]:
        """Gamma REST: рынки по категории (politics, sports, culture, economy). Только объёмный фильтр."""
        cat_lower = (category or "politics").strip().lower()
        url = f"{self.gamma_base}/markets?active=true&category={cat_lower}&limit={limit}&offset=0"
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
                outcomes_count = len(prices) if isinstance(prices, (list, tuple)) else 2
                yes_p = float(prices[0]) if len(prices) > 0 else 0.0
                no_p = float(prices[1]) if len(prices) > 1 else 0.0
                cids = m.get("clobTokenIds")
                clob_ids = list(cids) if isinstance(cids, (list, tuple)) and len(cids) >= 2 else None
                snap = MarketSnapshot(
                    market_id=str(m.get("conditionId") or m.get("id") or ""),
                    yes_price=yes_p,
                    no_price=no_p,
                    spread=abs(yes_p - no_p),
                    volume_usd=vol_f,
                    category=str(m.get("category") or cat_lower),
                    question=str(m.get("question") or ""),
                    outcomes_count=outcomes_count,
                    clob_token_ids=clob_ids,
                )
                markets.append(snap)
        self.markets.update({m.market_id: m for m in markets})
        binary_count = sum(1 for m in markets if m.outcomes_count == 2)
        logger.info("Fetched %d %s markets (%d binary; vol >= %.0f)",
                    len(markets), cat_lower, binary_count, min_volume_usd)
        return markets

    async def fetch_politics_markets(self, min_volume_usd: float = 500_000) -> List[MarketSnapshot]:
        """Gamma REST: топ рынки Politics. Обёртка над fetch_category_markets."""
        return await self.fetch_category_markets("politics", min_volume_usd=min_volume_usd)

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
            outcomes_count = len(prices) if isinstance(prices, (list, tuple)) else 2
            yes_p = float(prices[0]) if len(prices) > 0 else 0.0
            no_p = float(prices[1]) if len(prices) > 1 else 0.0
            cids = m.get("clobTokenIds")
            clob_ids = list(cids) if isinstance(cids, (list, tuple)) and len(cids) >= 2 else None
            snap = MarketSnapshot(
                market_id=str(m.get("conditionId") or m.get("id") or ""),
                yes_price=yes_p,
                no_price=no_p,
                spread=abs(yes_p - no_p),
                volume_usd=vol_f,
                category=str(m.get("category") or "crypto"),
                question=str(m.get("question") or ""),
                outcomes_count=outcomes_count,
                end_date_iso=end_d if isinstance(end_d, str) else None,
                seconds_to_resolution=sec_to if sec_to is not None else None,
                liquidity_usd=liq_f,
                clob_token_ids=clob_ids,
            )
            markets.append(snap)
        self.markets.update({m.market_id: m for m in markets})
        binary_count = sum(1 for m in markets if m.outcomes_count == 2)
        logger.info("Fetched %d crypto markets (%d binary; resolution <= %.1fh, vol >= %.0f, liq >= %.0f)",
                    len(markets), binary_count, max_hours_to_resolution, min_volume_usd, min_liquidity_usd)
        return markets

    def _binary_markets(self):
        """Только бинарные рынки (2 исхода). Мультиисходные — отдельная ветка анализа позже."""
        all_binary = []
        for m in self.markets.values():
            oc = getattr(m, "outcomes_count", None)
            if oc is None:
                cids = getattr(m, "clob_token_ids", None)
                oc = len(cids) if cids and isinstance(cids, (list, tuple)) else 2
            if oc == 2:
                all_binary.append(m)
        logger.info("DEBUG _binary_markets: %d from %d total", len(all_binary), len(self.markets))
        return all_binary

    def get_top_mispricing(self, top_n: int = 5) -> List[MarketSnapshot]:
        """Топ N бинарных рынков по спреду для стратегии (без фильтра по цене)."""
        return sorted(self._binary_markets(), key=lambda m: m.spread, reverse=True)[:top_n]

    def get_tradeable_top(
        self,
        top_n: int,
        max_entry: float = 0.99,
        min_yes: float = 0.02,
        exclude_ids: Optional[set] = None,
    ) -> List[MarketSnapshot]:
        """Сначала отбор по пригодности (YES в диапазоне), потом топ по спреду. Только бинарные рынки."""
        exclude_ids = exclude_ids or set()
        binary_markets = self._binary_markets()

        logger.info("=== YES PRICES DEBUG ===")
        for m in binary_markets[:10]:
            logger.info("YES=%.4f vol=$%.0fk id=%s", m.yes_price, m.volume_usd / 1e3, (m.market_id[:8] if m.market_id else ""))

        logger.info("DEBUG FILTER START: %d binary markets", len(binary_markets))

        rejected = 0
        tradeable = []

        for m in binary_markets:
            vol_k = int(m.volume_usd // 1000) if m.volume_usd else 0
            mid_short = m.market_id[:12] if m.market_id else ""

            if m.market_id in exclude_ids:
                logger.info("DEBUG filter_rejected: YES=%.4f vol=$%dk id=%s reason=excluded", m.yes_price, vol_k, mid_short)
                rejected += 1
                continue

            if m.yes_price < min_yes:
                logger.info("DEBUG filter_rejected: YES=%.4f vol=$%dk id=%s reason=YES<min_yes(%.3f)", m.yes_price, vol_k, mid_short, min_yes)
                rejected += 1
                continue

            if m.yes_price >= max_entry:
                logger.info("DEBUG filter_rejected: YES=%.4f vol=$%dk id=%s reason=YES>=max_entry(%.3f)", m.yes_price, vol_k, mid_short, max_entry)
                rejected += 1
                continue

            tradeable.append(m.market_id)

        logger.info("DEBUG FILTER END: rejected=%d tradeable=%d", rejected, len(tradeable))

        top_tradeable = sorted(
            tradeable,
            key=lambda mid: getattr(self.markets.get(mid), "spread", 0),
            reverse=True,
        )[:top_n]
        logger.info("FINAL candidates: %d (top %d of %d)", len(top_tradeable), top_n, len(tradeable))
        return [self.markets[mid] for mid in top_tradeable if mid in self.markets]

    def tradeable_diagnostic(
        self,
        max_entry: float = 0.99,
        min_yes: float = 0.02,
        exclude_ids: Optional[set] = None,
    ) -> dict:
        """Диагностика: сколько бинарных отсекается по YES и по exclude. Главный резак — yes_price >= max_entry."""
        exclude_ids = exclude_ids or set()
        binary = self._binary_markets()
        yes_above = sum(1 for m in binary if m.yes_price >= max_entry)
        yes_below = sum(1 for m in binary if m.yes_price < min_yes)
        in_range = sum(1 for m in binary if min_yes <= m.yes_price < max_entry)
        excluded_by_id = sum(1 for m in binary if min_yes <= m.yes_price < max_entry and m.market_id in exclude_ids)
        return {
            "binary_total": len(binary),
            "yes_above_max_entry": yes_above,
            "yes_below_min": yes_below,
            "in_yes_range": in_range,
            "excluded_held_cooldown": excluded_by_id,
        }

    # TODO: ветка мультиисходных рынков (outcomes_count >= 3): отдельный отбор и логика анализа/торговли


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
