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
    stop_loss_price: float = 0.0
    take_profit_price: float = 1.0

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
            "rationale": self.rationale,
            "stop_loss_price": round(self.stop_loss_price, 4),
            "take_profit_price": round(self.take_profit_price, 4),
        }

class ClawBotStrategy:
    def __init__(
        self,
        base_balance_usd: float = 100000,
        use_llm: bool = False,
        min_ev_threshold: float = 0.025,
        min_volume_usd: float = 100_000,
        min_yes_edge: float = 0.05,
        size_by_ev: bool = False,
        sl_pct: float = 0.07,
        tp_pct: float = 0.18,
    ):
        self.base_balance = base_balance_usd
        self.min_ev_threshold = min_ev_threshold
        self.min_volume_usd = min_volume_usd
        self.min_yes_edge = min_yes_edge
        self.size_by_ev = size_by_ev
        self.use_llm = use_llm
        self.sl_pct = sl_pct
        self.tp_pct = tp_pct

    def generate_signals(self, markets: List[MarketSnapshot]) -> List[Dict[str, Any]]:
        """Простая стратегия или LLM (если use_llm=True). При 0 сигналов от LLM — fallback на простую стратегию."""
        if self.use_llm:
            try:
                llm = LLMStrategy()
                signals = llm.generate_signals(
                    markets,
                    min_ev_threshold=self.min_ev_threshold,
                    sl_pct=self.sl_pct,
                    tp_pct=self.tp_pct,
                )
                if not signals and markets:
                    logger.info("LLM returned 0 signals, fallback to simple strategy for %d candidates", len(markets))
                    signals = self._generate_signals_simple(markets)
                    if not signals and markets:
                        logger.info(
                            "No signals after LLM + simple (strict thresholds). Skip slot — no synthetic test trade."
                        )
                return signals
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
        """BUY YES при недооценке: yes_price далеко от 0.50, спред, объём."""
        yes_edge = 0.50 - market.yes_price
        spread_edge = market.spread * 100
        
        # Строже вход: min_yes_edge (0.07–0.08) даёт меньше, но качественнее сделок → выше Winrate
        if (yes_edge > self.min_yes_edge and spread_edge > 0.02 and market.volume_usd > self.min_volume_usd):
            limit_price = market.yes_price * 0.99
            ev = yes_edge * 0.8
            # Размер: фиксированный 2% или от EV
            if self.size_by_ev:
                pct = 0.01 + 0.02 * min(1.0, (ev - self.min_ev_threshold) / 0.02)
                pct = max(0.01, min(0.03, pct))
            else:
                pct = 0.02
            target_usd = self.base_balance * pct
            # SL/TP в % от цены входа; гарантируем SL < entry и TP > entry (иначе риск отклонит)
            entry = limit_price
            stop_loss_price = max(0.01, entry * (1 - self.sl_pct))
            if stop_loss_price >= entry:
                stop_loss_price = round(entry - 0.01, 4)
            take_profit_price = min(0.99, entry * (1 + self.tp_pct))
            if take_profit_price <= entry:
                take_profit_price = min(0.99, round(entry + 0.01, 4))

            return TradingSignal(
                signal_id=str(uuid.uuid4()),
                market_id=market.market_id,
                side="buy",
                outcome="YES",
                limit_price=limit_price,
                target_size_usd=target_usd,
                expected_ev=ev,
                confidence="medium",
                rationale=f"YES undervalued ({yes_edge:.1%}), spread {spread_edge:.1%}, volume ${market.volume_usd/1e6:.1f}M",
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
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

**Задача**: По рынкам ниже верни только сигналы с реально сильным edge. Лучше **пустой массив signals**, чем слабый вход.

**Формат ответа** строго один JSON-объект с ключом "signals" (массив; может быть пустым):
{{"signals": [
  {{"signal_id": "uuid", "market_id": "0x...", "side": "buy", "outcome": "YES", "limit_price": 0.42, "target_size_usd": 2000, "expected_ev": 0.07, "confidence": "medium", "rationale": "..."}}
]}}

**Рынки**:
{markets_text}

Правила:
- Включай сигнал только если expected_ev >= **0.06** (6%) и обоснование убедительное; иначе верни "signals": [].
- limit_price из цены YES (или * 0.99). target_size_usd: 2000. signal_id: uuid. rationale обязателен.
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
        self,
        candidates: List[MarketSnapshot],
        min_ev_threshold: float = 0.025,
        sl_pct: float = 0.07,
        tp_pct: float = 0.18,
    ) -> List[Dict[str, Any]]:
        if not candidates:
            logger.info("LLM: 0 candidates, skip API and return []")
            return []
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
                required = {"market_id", "target_size_usd", "outcome", "side"}  # limit_price подставляем при 0
                raw_count = len(raw)
                by_id = {m.market_id: m for m in candidates}
                signals = []
                for s in raw:
                    if not (isinstance(s, dict) and {"market_id", "target_size_usd", "outcome", "side"}.issubset(s.keys())):
                        continue
                    if s.get("market_id") not in by_id:
                        continue  # только сигналы по рынкам из переданного списка кандидатов
                    if float(s.get("expected_ev", 0)) < min_ev_threshold:
                        continue
                    # Нормализуем ключи от LLM (могут прийти limitPrice, stop_loss и т.д.)
                    if "limitPrice" in s and "limit_price" not in s:
                        s["limit_price"] = s["limitPrice"]
                    # limit_price может прийти 0 или отсутствовать — всегда берём цену из данных рынка при необходимости
                    lp = float(s.get("limit_price") or s.get("limitPrice") or 0)
                    if lp <= 0 and s.get("market_id") in by_id:
                        lp = getattr(by_id[s["market_id"]], "yes_price", 0.5) or 0.5
                    if lp <= 0:
                        lp = 0.5
                    lp = max(lp, 0.02)  # никогда не выдавать limit_price 0 (мёртвый рынок)
                    s["limit_price"] = round(lp * 0.99, 4)
                    entry = float(s["limit_price"])
                    sl = max(0.01, entry * (1 - sl_pct))
                    if sl >= entry:
                        sl = round(entry - 0.01, 4)
                    s["stop_loss_price"] = round(sl, 4)
                    tp = min(0.99, entry * (1 + tp_pct))
                    if tp <= entry:
                        tp = min(0.99, round(entry + 0.01, 4))
                    s["take_profit_price"] = round(tp, 4)
                    signals.append(s)
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
