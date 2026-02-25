"""
Block 2: Strategy Engine
Преобразует MarketSnapshot → JSON Signals для Risk Manager
Simple + LLM modes
"""

import uuid
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from datafeed import MarketSnapshot

logger = logging.getLogger(__name__)

@dataclass
class TradingSignal:
    signal_id: str
    market_id: str
    side: str
    outcome: str
    limit_price: float
    target_size_usd: float
    expected_ev: float
    confidence: str
    rationale: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "market_id": self.market_id,
            "side": self.side,
            "outcome": self.outcome,
            "limit_price": round(self.limit_price, 4),
            "target_size_usd": self.target_size_usd,
            "expected_ev": round(self.expected_ev, 4),
            "confidence": self.confidence,
            "rationale": self.rationale
        }

class ClawBotStrategy:
    def __init__(
        self,
        base_balance_usd: float = 100000,
        use_llm: bool = False,
        min_ev_threshold: float = 0.025,
        min_volume_usd: float = 100_000,
    ):
        self.base_balance = base_balance_usd
        self.min_ev_threshold = min_ev_threshold
        self.min_volume_usd = min_volume_usd
        self.use_llm = use_llm

    def generate_signals(self, markets: List[MarketSnapshot]) -> List[Dict[str, Any]]:
        """Простая стратегия или LLM (если use_llm=True)."""
        if self.use_llm:
            try:
                llm = LLMStrategy()
                return llm.generate_signals(markets, min_ev_threshold=self.min_ev_threshold)
            except Exception as e:
                logger.warning(f"LLM failed ({e}), falling back to simple strategy")
                return self._generate_signals_simple(markets)
        return self._generate_signals_simple(markets)

    def _generate_signals_simple(self, markets: List[MarketSnapshot]) -> List[Dict[str, Any]]:
        signals = []
        for market in markets:
            signal = self._analyze_market(market)
            if signal and signal.expected_ev >= self.min_ev_threshold:
                signals.append(signal.to_dict())
                logger.info(f"Generated signal: {signal.market_id[:8]} {signal.side} {signal.outcome} EV={signal.expected_ev:.3f}")
        
        return signals

    def _analyze_market(self, market: MarketSnapshot) -> Optional[TradingSignal]:
        """BUY YES если цена < 0.45 + большой спред"""
        yes_edge = 0.50 - market.yes_price
        spread_edge = market.spread * 100
        
        # Фильтр качества: EV по порогу + volume > min_volume_usd
        if (yes_edge > 0.05 and spread_edge > 0.02 and market.volume_usd > self.min_volume_usd):
            limit_price = market.yes_price * 0.99
            
            return TradingSignal(
                signal_id=str(uuid.uuid4()),
                market_id=market.market_id,
                side="buy",
                outcome="YES",
                limit_price=limit_price,
                target_size_usd=self.base_balance * 0.02,
                expected_ev=yes_edge * 0.8,
                confidence="medium",
                rationale=f"YES undervalued ({yes_edge:.1%}), spread {spread_edge:.1%}, volume ${market.volume_usd/1e6:.1f}M"
            )
        
        return None

# === LLM INTEGRATION (опционально) ===
import os
import json

try:
    from openai import OpenAI
    _OPENAI_AVAILABLE = True
except ImportError:
    OpenAI = None
    _OPENAI_AVAILABLE = False

# Фигурные скобки в JSON-примере экранированы ({{ }}), иначе .format() воспринимает их как плейсхолдеры
SYSTEM_PROMPT = """
ClawBot Polymarket Strategy Agent.

**Задача**: По рынкам ниже выбери хотя бы один с EV >= 2.5% (expected value >= 0.025) и volume > $100k. Нужен минимум 1 сигнал для проверки пайплайна (риск-менеджмент и симуляция сделок).

**Формат ответа** строго один JSON-объект с ключом "signals" (массив, минимум 1 элемент):
{{"signals": [
  {{"signal_id": "uuid", "market_id": "0x...", "side": "buy", "outcome": "YES", "limit_price": 0.42, "target_size_usd": 2000, "expected_ev": 0.05, "confidence": "medium", "rationale": "..."}}
]}}

**Рынки**:
{markets_text}

Правила:
- Верни минимум ОДИН сигнал: выбери рынок с лучшим EV >= 2.5% и volume > $100k из списка (скопируй market_id из строки рынка).
- expected_ev >= 0.025 (минимум 2.5%, например 0.025–0.10).
- limit_price возьми из цены YES рынка (или * 0.99).
- target_size_usd: 2000. signal_id: любой uuid. Обоснование (rationale) обязательно.
"""

class LLMStrategy:
    def __init__(self):
        if not _OPENAI_AVAILABLE or OpenAI is None:
            raise ImportError("Install openai: pip install openai")
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("Set OPENAI_API_KEY in .env")
        self.client = OpenAI(api_key=api_key)

    def generate_signals(
        self, candidates: List[MarketSnapshot], min_ev_threshold: float = 0.025
    ) -> List[Dict[str, Any]]:
        # Полный market_id нужен для ответа LLM (блоки 3–4 используют его)
        markets_text = "\n".join([
            f"- market_id={m.market_id} | YES={m.yes_price:.4f}, NO={m.no_price:.4f}, "
            f"spread={m.spread:.1%}, vol=${m.volume_usd/1e6:.1f}M"
            for m in candidates
        ])
        
        prompt = SYSTEM_PROMPT.format(markets_text=markets_text)
        
        response = self.client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        
        try:
            content = response.choices[0].message.content
            if not content or not content.strip():
                signals = []
            else:
                # Убираем обёртку ```json ... ``` если модель вернула markdown
                text = content.strip()
                if text.startswith("```"):
                    lines = text.split("\n")
                    text = "\n".join(
                        line for line in lines
                        if not line.strip().startswith("```")
                    )
                result = json.loads(text)
                if isinstance(result, list):
                    raw = result
                elif isinstance(result, dict):
                    raw = result.get("signals", [])
                    # Модель может вернуть другой ключ или пустой массив
                    if not raw and result:
                        logger.info("LLM ответ (ключи): %s", list(result.keys()))
                else:
                    raw = []
                if not isinstance(raw, list):
                    raw = []
                # Оставляем только валидные сигналы (есть market_id, target_size_usd, ...) и EV >= min_ev_threshold
                required = {"market_id", "target_size_usd", "outcome", "side", "limit_price"}
                raw_count = len(raw)
                signals = [
                    s for s in raw
                    if isinstance(s, dict) and required.issubset(s.keys())
                    and float(s.get("expected_ev", 0)) >= min_ev_threshold
                ]
                # Диагностика: почему 0 после проверки полей
                if raw_count > 0 and len(signals) == 0:
                    first = raw[0] if raw else {}
                    keys_in = set(first.keys()) if isinstance(first, dict) else set()
                    missing = required - keys_in
                    logger.warning(
                        "LLM вернул %d сигналов, но все отфильтрованы. У первого нет полей: %s (есть ключи: %s)",
                        raw_count, missing, keys_in
                    )
                else:
                    logger.info("LLM raw: %d, после проверки полей: %d", raw_count, len(signals))
        except Exception as e:
            logger.warning("LLM JSON failed (%s), using simple strategy", e)
            signals = []

        logger.info("LLM generated %d signals", len(signals))
        return signals

# SmartStrategy — алиас для совместимости (логика уже в ClawBotStrategy)
SmartStrategy = ClawBotStrategy

# Тест
if __name__ == "__main__":
    print("Strategy ready. Use SmartStrategy(use_llm=True) in main.py")
