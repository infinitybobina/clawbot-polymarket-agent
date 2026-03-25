"""
Логирование экспериментов по профилям 15M (консервативный / агрессивный).
Интервалы: results_{profile}_intervals.csv.
Итог сессии: results_{profile}_summary.csv и одна строка в results_profiles_summary.csv.
Опционально: results_{profile}_params.json — strategy_params на момент запуска.
"""

import csv
import json
import os
import statistics
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

INTERVALS_HEADER = [
    "profile", "timestamp", "n_markets", "n_decisions", "n_trades", "n_skipped",
    "avg_spread", "median_tte_sec", "avg_size", "pnl_interval",
]
SUMMARY_HEADER = [
    "profile", "n_intervals", "n_calls", "n_trades", "n_markets_traded",
    "winrate", "avg_pnl_trade", "total_pnl", "max_dd",
    "avg_spread", "median_clob_vol_24h",
]


class ExperimentLogger:
    """Пишет интервалы в CSV и по завершении сессии — итоговую строку и сводку по профилям."""

    def __init__(self, profile: str, root: str):
        self.profile = profile
        self.root = root
        self._intervals_path = os.path.join(root, f"results_{profile}_intervals.csv")
        self._summary_path = os.path.join(root, f"results_{profile}_summary.csv")
        self._profiles_summary_path = os.path.join(root, "results_profiles_summary.csv")
        self._header_written = False
        self._n_calls = 0
        self._n_trades_total = 0
        self._markets_traded: Set[str] = set()
        self._pnl_curve: List[float] = [0.0]
        self._spreads: List[float] = []
        self._clob_vols: List[float] = []

    def _ensure_header(self, path: str, header: List[str]) -> None:
        if os.path.exists(path):
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)

    def log_interval(
        self,
        timestamp: Optional[str] = None,
        n_markets: int = 0,
        n_decisions: int = 0,
        n_trades: int = 0,
        n_skipped: Optional[int] = None,
        avg_spread: Optional[float] = None,
        median_tte_sec: Optional[float] = None,
        avg_size: Optional[float] = None,
        pnl_interval: float = 0.0,
        median_clob_vol_24h: Optional[float] = None,
        cumulative_pnl_after: Optional[float] = None,
    ) -> None:
        """Добавить строку в results_{profile}_intervals.csv. cumulative_pnl_after — для расчёта max_dd."""
        if n_skipped is None:
            n_skipped = max(0, n_decisions - n_trades)
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        row = [
            self.profile,
            ts,
            n_markets,
            n_decisions,
            n_trades,
            n_skipped,
            avg_spread if avg_spread is not None else "",
            median_tte_sec if median_tte_sec is not None else "",
            avg_size if avg_size is not None else "",
            pnl_interval,
        ]
        self._ensure_header(self._intervals_path, INTERVALS_HEADER)
        with open(self._intervals_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(row)
        self._n_calls += 1
        self._n_trades_total += n_trades
        if cumulative_pnl_after is not None:
            self._pnl_curve.append(cumulative_pnl_after)
        elif pnl_interval != 0:
            self._pnl_curve.append(self._pnl_curve[-1] + pnl_interval)
        if avg_spread is not None:
            self._spreads.append(avg_spread)
        if median_clob_vol_24h is not None:
            self._clob_vols.append(median_clob_vol_24h)

    def add_markets_traded(self, market_ids: List[str]) -> None:
        """Учесть рынки, по которым прошли сделки в этом интервале."""
        self._markets_traded.update(market_ids)

    def finish_session(
        self,
        trader: Any,
        cumulative_realized_pnl: float,
        strategy_params: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Записать results_{profile}_summary.csv и добавить строку в results_profiles_summary.csv.
        trader: PaperTrader (closed_trades, positions).
        strategy_params: опционально сохранить в results_{profile}_params.json для воспроизводимости.
        """
        closed = getattr(trader, "closed_trades", []) or []
        n_trades = len(closed)
        wins = sum(1 for c in closed if isinstance(c.get("pnl_usd"), (int, float)) and c["pnl_usd"] > 0)
        winrate = (wins / n_trades) if n_trades else 0.0
        pnls = [float(c.get("pnl_usd", 0)) for c in closed if isinstance(c.get("pnl_usd"), (int, float))]
        avg_pnl_trade = statistics.mean(pnls) if pnls else 0.0
        total_pnl = cumulative_realized_pnl
        # max_dd по кривой накопленного PnL
        peak = 0.0
        max_dd = 0.0
        for p in self._pnl_curve:
            if p > peak:
                peak = p
            dd = peak - p
            if dd > max_dd:
                max_dd = dd
        avg_spread = statistics.mean(self._spreads) if self._spreads else 0.0
        median_clob_vol_24h = statistics.median(self._clob_vols) if self._clob_vols else 0.0
        n_intervals = self._n_calls  # один вызов log_interval = один интервал

        summary_row = [
            self.profile,
            n_intervals,
            self._n_calls,
            self._n_trades_total,
            len(self._markets_traded),
            round(winrate, 4),
            round(avg_pnl_trade, 2),
            round(total_pnl, 2),
            round(max_dd, 2),
            round(avg_spread, 4),
            round(median_clob_vol_24h, 2),
        ]
        self._ensure_header(self._summary_path, SUMMARY_HEADER)
        with open(self._summary_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(summary_row)

        self._ensure_header(self._profiles_summary_path, SUMMARY_HEADER)
        with open(self._profiles_summary_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(summary_row)

        if strategy_params:
            params_path = os.path.join(self.root, f"results_{self.profile}_params.json")
            with open(params_path, "w", encoding="utf-8") as f:
                json.dump({"profile": self.profile, "strategy_params": strategy_params}, f, indent=2)


def median_tte_sec_from_candidates(candidates: List[Any]) -> Optional[float]:
    """Медиана seconds_to_resolution по списку кандидатов (MarketSnapshot)."""
    vals = []
    for m in candidates:
        s = getattr(m, "seconds_to_resolution", None)
        if s is not None and isinstance(s, (int, float)):
            vals.append(float(s))
    return statistics.median(vals) if vals else None


def avg_spread_from_candidates(candidates: List[Any]) -> Optional[float]:
    """Средний спред по кандидатам."""
    vals = []
    for m in candidates:
        s = getattr(m, "spread", None)
        if s is not None and isinstance(s, (int, float)):
            vals.append(float(s))
    return statistics.mean(vals) if vals else None


def median_clob_vol_from_candidates(candidates: List[Any]) -> Optional[float]:
    """Медиана CLOB volume 24h по кандидатам (если есть поле clob_volume_24h или volume_usd)."""
    vals = []
    for m in candidates:
        v = getattr(m, "clob_volume_24h", None) or getattr(m, "volume_usd", None)
        if v is not None and isinstance(v, (int, float)) and v > 0:
            vals.append(float(v))
    return statistics.median(vals) if vals else None
