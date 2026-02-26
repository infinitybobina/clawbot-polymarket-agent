#!/usr/bin/env python3
"""
Бэктест ClawBot: 5 этапов.
1. Исторические данные (симуляция 30 дней hourly по паттернам Polymarket)
2. Симуляция по часам (720 ч): fetch → strategy → risk → paper_trader → PnL
3. Метрики: Sharpe, Winrate, Profit, Drawdown, Calmar
4. Структура Backtester
5. Реалистичные цены: 60% тренды, 25% range, 15% спайки; slippage 0.5%, fees 0.1%
"""

import logging
import random
import math
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from datafeed import MarketSnapshot
from strategy import ClawBotStrategy
from riskmanager import RiskManager
from paper_trader import PaperTrader

logger = logging.getLogger(__name__)

SLIPPAGE_PCT = 0.005
FEE_PCT = 0.001
RISK_FREE_ANNUAL = 0.05  # для Sharpe


def _simulate_hourly_prices(
    n_hours: int,
    start_price: float = 0.50,
    trend_pct: float = 0.60,
    range_pct: float = 0.25,
    spike_pct: float = 0.15,
    seed: Optional[int] = None,
) -> List[float]:
    """
    Реалистичная симуляция: 60% тренды (импульс 3–12 ч), 25% range (0.45–0.55), 15% спайки.
    """
    if seed is not None:
        random.seed(seed)
    prices = [start_price]
    regime = "trend"
    trend_duration = 0
    trend_direction = 0.0

    for h in range(1, n_hours):
        r = random.random()
        if r < spike_pct:
            # Спайк (новостной)
            move = random.uniform(-0.15, 0.15)
            regime = "spike"
        elif r < spike_pct + range_pct:
            # Range 0.45–0.55
            move = random.uniform(-0.01, 0.01)
            regime = "range"
        else:
            # Тренд (импульс 3–12 ч)
            if regime != "trend" or trend_duration <= 0:
                trend_duration = random.randint(3, 12)
                trend_direction = random.uniform(-0.008, 0.008)
            move = trend_direction
            trend_duration -= 1
            regime = "trend"

        p = prices[-1] + move
        p = max(0.01, min(0.99, p))
        prices.append(round(p, 4))
    return prices


def build_snapshots_at_hour(
    market_ids: List[str],
    prices_by_market: Dict[str, List[float]],
    hour: int,
    volume_usd: float = 10_000_000,  # порог в стратегии 5M
    category: str = "politics",
) -> List[MarketSnapshot]:
    """Строит снимки рынков на час T для стратегии."""
    snapshots = []
    for mid in market_ids:
        if mid not in prices_by_market or hour >= len(prices_by_market[mid]):
            continue
        yes_p = prices_by_market[mid][hour]
        no_p = 1.0 - yes_p
        snapshots.append(
            MarketSnapshot(
                market_id=mid,
                yes_price=yes_p,
                no_price=no_p,
                spread=abs(yes_p - no_p),
                volume_usd=volume_usd,
                category=category,
                question="",
            )
        )
    return snapshots


def prices_at_hour_dict(prices_by_market: Dict[str, List[float]], hour: int) -> Dict[str, float]:
    """Словарь market_id -> цена на час T (для fill и mark-to-market)."""
    out = {}
    for mid, series in prices_by_market.items():
        if hour < len(series):
            out[mid] = series[hour]
    return out


@dataclass
class Backtester:
    """Бэктест: симуляция по часам, метрики."""

    initial_balance: float = 100_000
    days: int = 30
    n_markets: int = 20   # оптимизация: 20 сделок, цель Sharpe >1.0
    use_llm: bool = False  # в бэктесте по умолчанию без LLM (экономия и стабильность)
    max_positions_per_market: int = 2   # мультипозиции: до 2 ордеров на рынок → 25–30 сделок
    # Опциональные параметры теста (переопределяют дефолты)
    min_ev_threshold: float = 0.025
    min_volume_usd: float = 100_000
    max_single_market_pct: float = 0.04
    # Улучшение Winrate/Profit/Sharpe: строже вход, топ-N сигналов в час
    min_yes_edge: float = 0.07
    max_signals_per_hour: int = 2
    size_by_ev: bool = True
    # Порог импульса: пропускать рынок, если цена за час упала больше чем на momentum_threshold (0 = только рост)
    momentum_threshold: float = 0.02
    # Seed для симуляции цен (разные seed → разные сценарии; 90-дневные результаты от него сильно зависят)
    price_seed: Optional[int] = 42

    def __post_init__(self):
        self.balance = self.initial_balance
        self.trades: List[Dict[str, Any]] = []
        self.trade_count: int = 0
        self.equity_curve: List[float] = [self.initial_balance]
        self.market_ids: List[str] = []
        self.prices_by_market: Dict[str, List[float]] = {}
        self.trader = PaperTrader(initial_balance=self.initial_balance)
        self.risk_mgr = RiskManager()
        self.risk_mgr.config["max_single_market_pct"] = self.max_single_market_pct
        self.risk_mgr.config["max_category_pct"] = 0.75
        self.risk_mgr.config["max_exposure_pct"] = 0.80
        self.strategy = ClawBotStrategy(
            use_llm=self.use_llm,
            min_ev_threshold=self.min_ev_threshold,
            min_volume_usd=self.min_volume_usd,
            min_yes_edge=self.min_yes_edge,
            size_by_ev=self.size_by_ev,
        )
        self._prepare_historical()

    def _prepare_historical(self) -> None:
        """Генерирует N*24 часов цен по N рынкам (реалистичная симуляция). Разные price_seed дают разные сценарии."""
        n_hours = self.days * 24
        base = self.price_seed if self.price_seed is not None else 42
        for i in range(self.n_markets):
            mid = f"0xmarket_{i:04d}"
            self.market_ids.append(mid)
            self.prices_by_market[mid] = _simulate_hourly_prices(
                n_hours, start_price=0.45 + i * 0.02, seed=base + i
            )
        logger.info("Historical data: %d markets x %d hours (seed=%s)", self.n_markets, n_hours, base)

    def run(self, days: Optional[int] = None) -> Dict[str, Any]:
        """Запуск бэктеста: цикл по часам, вызов стратегии/риска/исполнения, обновление PnL."""
        total_hours = (days or self.days) * 24
        for hour in range(total_hours - 1):
            # Прогресс при LLM (каждые 3 дня), чтобы видеть, что процесс не завис
            if self.use_llm and hour > 0 and hour % 72 == 0:
                print(f"  [LLM backtest] день {hour // 24}/{total_hours // 24}, час {hour}/{total_hours}")
            # 1. Данные на час T
            snapshots = build_snapshots_at_hour(
                self.market_ids, self.prices_by_market, hour
            )
            if not snapshots:
                continue
            # Фильтр импульса: не покупать, если цена упала за час больше чем на momentum_threshold
            if hour > 0 and self.momentum_threshold is not None:
                snapshots = [
                    s for s in snapshots
                    if self.prices_by_market[s.market_id][hour] >= self.prices_by_market[s.market_id][hour - 1] - self.momentum_threshold
                ]
            top = sorted(snapshots, key=lambda m: m.spread, reverse=True)[: self.n_markets]

            # 2. Сигналы; берём только топ по EV (меньше шума → выше Winrate/Sharpe)
            signals = self.strategy.generate_signals(top)
            if signals:
                signals = sorted(signals, key=lambda s: float(s.get("expected_ev", 0)), reverse=True)[: self.max_signals_per_hour]
            approved = []
            if signals:
                # 3. Risk Manager
                result = self.risk_mgr.process_signals(signals)
                approved = result["approved_orders"]
                if approved:
                    # 4. Paper trader: исполнение по цене T со slippage и fee
                    fill_prices = prices_at_hour_dict(self.prices_by_market, hour)
                    self.trader.execute_orders(
                        approved,
                        backtest_fill_prices=fill_prices,
                        slippage_pct=SLIPPAGE_PCT,
                        fee_pct=FEE_PCT,
                    )
                    self.trade_count += len(approved)
                    self._sync_risk_portfolio()

            # 5. PnL: mark-to-market по цене T+1
            next_prices = prices_at_hour_dict(self.prices_by_market, hour + 1)
            self._append_equity(mark_to_market=next_prices)
        return self.metrics()

    def _sync_risk_portfolio(self) -> None:
        """Синхронизация состояния Risk Manager с Paper Trader после исполнения."""
        self.risk_mgr.portfolio.balance_usd = self.trader.balance
        self.risk_mgr.portfolio.positions = {
            mid: float(p["size_tokens"]) * float(p["avg_price"])
            for mid, p in self.trader.positions.items()
        }

    def _append_equity(
        self, mark_to_market: Optional[Dict[str, float]] = None
    ) -> None:
        m = self.trader.get_portfolio_metrics(mark_to_market_prices=mark_to_market)
        self.equity_curve.append(m["total_value"])

    def metrics(self) -> Dict[str, Any]:
        """Sharpe, Winrate, Profit, Drawdown, Calmar."""
        if len(self.equity_curve) < 2:
            return {
                "sharpe": 0.0,
                "winrate_pct": 0.0,
                "profit_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "calmar": 0.0,
                "total_trades": 0,
                "final_balance": self.initial_balance,
            }

        curve = self.equity_curve
        returns = []
        for i in range(1, len(curve)):
            if curve[i - 1] > 0:
                returns.append((curve[i] - curve[i - 1]) / curve[i - 1])
            else:
                returns.append(0.0)

        n = len(returns)
        avg_ret = sum(returns) / n if n else 0
        std_ret = (sum((r - avg_ret) ** 2 for r in returns) / n) ** 0.5 if n else 1e-9
        # Sharpe (annualized, hourly -> * sqrt(24*365))
        risk_free_hourly = RISK_FREE_ANNUAL / (24 * 365)
        sharpe = (avg_ret - risk_free_hourly) / std_ret * math.sqrt(24 * 365) if std_ret else 0

        final = curve[-1]
        profit_pct = (final - self.initial_balance) / self.initial_balance * 100

        peak = curve[0]
        max_dd = 0.0
        for v in curve:
            if v > peak:
                peak = v
            if peak > 0:
                dd = (peak - v) / peak
                if dd > max_dd:
                    max_dd = dd
        max_drawdown_pct = max_dd * 100
        annual_ret = profit_pct / (self.days / 365) if self.days else 0
        calmar = annual_ret / (max_drawdown_pct or 1e-9)

        wins = sum(1 for r in returns if r > 0)
        winrate_pct = wins / n * 100 if n else 0

        return {
            "sharpe": round(sharpe, 4),
            "winrate_pct": round(winrate_pct, 2),
            "profit_pct": round(profit_pct, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "calmar": round(calmar, 4),
            "total_trades": self.trade_count,
            "final_balance": round(final, 2),
            "equity_curve_length": len(curve),
        }


# Пресеты тестов (качество при минимуме сделок и т.д.)
TEST_PRESETS = {
    "A": {  # Тест A: качество при минимуме сделок | EV=3%, Vol>$500k, Risk=3%
        "n_markets": 12,
        "min_ev_threshold": 0.03,
        "min_volume_usd": 500_000,
        "max_single_market_pct": 0.03,
        "max_category_pct": 0.36,
        "max_exposure_pct": 0.36,
        "momentum_threshold": 0.02,  # порог импульса: 0 = только рост, 0.02 = допуск падения 2%
        "min_yes_edge": 0.08,
    },
}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.WARNING)
    argv = sys.argv[1:]
    days = 30
    if argv and argv[0].isdigit():
        days = int(argv[0])
        argv = argv[1:]
    preset_name = argv[0] if argv and argv[0].upper() in TEST_PRESETS else None
    if preset_name:
        argv = argv[1:]
    use_llm = "llm" in [x.lower() for x in argv]
    # Порог импульса из аргумента: m0.02 или m0.01
    momentum_override = None
    for a in argv:
        if a.lower().startswith("m") and len(a) > 1:
            try:
                momentum_override = float(a[1:])
                break
            except ValueError:
                pass
    if preset_name and preset_name.upper() in TEST_PRESETS:
        preset = TEST_PRESETS[preset_name.upper()]
        kw = dict(
            days=days,
            n_markets=preset["n_markets"],
            min_ev_threshold=preset["min_ev_threshold"],
            min_volume_usd=preset["min_volume_usd"],
            max_single_market_pct=preset["max_single_market_pct"],
            use_llm=use_llm,
        )
        if "momentum_threshold" in preset:
            kw["momentum_threshold"] = preset["momentum_threshold"]
        if "min_yes_edge" in preset:
            kw["min_yes_edge"] = preset["min_yes_edge"]
        if momentum_override is not None:
            kw["momentum_threshold"] = momentum_override
        bt = Backtester(**kw)
        bt.risk_mgr.config["max_category_pct"] = preset["max_category_pct"]
        bt.risk_mgr.config["max_exposure_pct"] = preset["max_exposure_pct"]
        llm_tag = " + LLM" if use_llm else ""
        mom = momentum_override if momentum_override is not None else preset.get("momentum_threshold", 0.02)
        print(f"Test {preset_name}{llm_tag}: EV={preset['min_ev_threshold']:.0%}, Vol>${preset['min_volume_usd']/1e3:.0f}k, Risk={preset['max_single_market_pct']:.0%}, momentum={mom}")
    else:
        kw = dict(days=days)
        if momentum_override is not None:
            kw["momentum_threshold"] = momentum_override
        bt = Backtester(**kw)
    result = bt.run()
    print(f"Backtest result ({days} days):")
    for k, v in result.items():
        print(f"  {k}: {v}")
