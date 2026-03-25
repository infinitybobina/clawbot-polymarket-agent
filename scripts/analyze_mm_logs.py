#!/usr/bin/env python3
"""
Подсчёт MM-событий в логе: MM place, MM FILL, MM order timeout.
Оценка: не слишком ли редко исполняются лимитки и не слишком ли часто тайм-аут без сделок.

Использование:
  python scripts/analyze_mm_logs.py [path_to_log]
  python scripts/analyze_mm_logs.py clawbot_v2_run.log
"""
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent.parent
    log_path = root / "clawbot_v2_run.log"
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        log_path = Path(sys.argv[1])

    if not log_path.exists():
        print(f"Log not found: {log_path}", file=sys.stderr)
        return 1

    place = fill = timeout = 0
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if "MM place:" in line:
                place += 1
            elif "MM FILL:" in line:
                fill += 1
            elif "MM order timeout:" in line:
                timeout += 1

    print(f"# MM log summary: {log_path}")
    print(f"  MM place:       {place}  (выставлено лимиток)")
    print(f"  MM FILL:        {fill}  (исполнено)")
    print(f"  MM order timeout: {timeout}  (снято по таймауту)")
    if place > 0:
        fill_pct = 100.0 * fill / place
        print(f"  Fill rate:      {fill_pct:.1f}%  (FILL / place)")
        if fill_pct < 20 and timeout > place:
            print("#  → Мало исполнений и много тайм-аутов: поднять max_spread (0.06) или снизить min_best_level_size (40)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
