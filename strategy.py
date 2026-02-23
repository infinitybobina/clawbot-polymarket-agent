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
    def __init__(self, base_balance_usd: float = 100000, use_llm: bool = False):
        self.base_balance = base_balance_usd
        self.min_ev_threshold = 0.03
        self.use_llm = use_llm

    def generate_signals(self, markets: List[MarketSnapshot]) -> List[Dict[str, Any]]:
        """Простая стратегия или LLM (если use_llm=True)."""
        if self.use_llm:
            try:
                llm = LLMStrategy()
                return llm.generate_signals(markets)
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
        
        if (yes_edge > 0.05 and spread_edge > 0.02 and market.volume_usd > 5000000):
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

SYSTEM_PROMPT = """
ClawBot Polymarket Strategy Agent.

**Задача**: Анализируй рынки, найди mispricing (EV ≥ 3%).

**Формат ответа** JSON массив:
[
  {
    "signal_id": "uuid",
    "market_id": "0x...",
    "side": "buy|sell",
    "outcome": "YES|NO",
    "limit_price": 0.42,
    "target_size_usd": 2000,
    "expected_ev": 0.06,
    "confidence": "low|medium|high",
    "rationale": "твоя логика"
  }
]

**Рынки**:
{markets_text}

Правила:
- Только EV ≥ 0.03
- limit_price = рынок * 0.99
- Обоснование обязательно
"""

class LLMStrategy:
    def __init__(self):
        if not _OPENAI_AVAILABLE or OpenAI is None:
            raise ImportError("Install openai: pip install openai")
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("Set OPENAI_API_KEY in .env")
        self.client = OpenAI(api_key=api_key)

    def generate_signals(self, candidates: List[MarketSnapshot]) -> List[Dict[str, Any]]:
        markets_text = "\n".join([
            f"- {m.market_id[:8]}: YES={m.yes_price:.4f}, NO={m.no_price:.4f}, "
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
            result = json.loads(content)
            signals = result.get("signals", result) if isinstance(result, dict) else result
            if not isinstance(signals, list):
                signals = []
        except Exception:
            logger.warning("LLM JSON failed, using simple")
            signals = []

        logger.info(f"LLM generated {len(signals)} signals")
        return signals

# SmartStrategy — алиас для совместимости (логика уже в ClawBotStrategy)
SmartStrategy = ClawBotStrategy

# Тест
if __name__ == "__main__":
    print("Strategy ready. Use SmartStrategy(use_llm=True) in main.py")
