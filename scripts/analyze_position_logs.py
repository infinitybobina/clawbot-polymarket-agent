#!/usr/bin/env python3
"""
Анализ логов «Position price:» по market_id.
Проверка: до экспирации был ли best_bid живой или только None → потом 0.01.

Использование:
  python scripts/analyze_position_logs.py [path_to_log] [market_id_prefix] [--lines 20]
  python scripts/analyze_position_logs.py clawbot_v2_run.log 0x9c1a953fe92c83 --lines 20

Если не передать market_id_prefix — выводятся все строки Position price: (можно перенаправить в файл и отфильтровать вручную).
"""
import re
import sys
from pathlib import Path

LOG_PATTERN = re.compile(r"Position price:\s+mid=([^\s.]+)[.\s]+best_bid=(\S+)\s+sl=([\d.]+)")


def main():
    root = Path(__file__).resolve().parent.parent
    log_path = root / "clawbot_v2_run.log"
    mid_prefix = ""
    max_lines = 20

    args = list(sys.argv[1:])
    if args and not args[0].startswith("-"):
        log_path = Path(args.pop(0))
    if args and not args[0].startswith("-"):
        mid_prefix = args.pop(0)
    if "--lines" in args:
        i = args.index("--lines")
        if i + 1 < len(args):
            max_lines = int(args[i + 1])
            args.pop(i)
            args.pop(i)
        else:
            args.pop(i)

    if not log_path.exists():
        print(f"Log not found: {log_path}", file=sys.stderr)
        sys.exit(1)

    collected = []
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if "Position price:" not in line:
                continue
            m = LOG_PATTERN.search(line)
            if not m:
                collected.append((line.strip(), None))
                continue
            mid, best_bid, sl = m.groups()
            if mid_prefix and not mid.startswith(mid_prefix) and mid_prefix not in mid:
                continue
            collected.append((line.strip(), (mid, best_bid, sl)))

    # последние N записей по выбранному mid
    if mid_prefix:
        subset = collected[-max_lines:] if len(collected) > max_lines else collected
    else:
        subset = collected[-max_lines:] if len(collected) > max_lines else collected

    print(f"# Log: {log_path} | mid_prefix={mid_prefix!r} | showing last {len(subset)} matching lines\n")
    live_count = 0
    for item in subset:
        if isinstance(item[1], tuple):
            mid, best_bid, sl = item[1]
            is_live = best_bid not in ("None", "null", "") and best_bid != "0.01"
            if is_live:
                live_count += 1
            tag = "LIVE" if is_live else "DEAD"
            print(f"  [{tag}] {item[0]}")
        else:
            print(f"  {item[0]}")
    if not collected and mid_prefix:
        print("# No lines found for this market_id prefix. Try without prefix or check log.")
    if mid_prefix and subset and live_count == 0:
        print("\n# Все best_bid=None или 0.01 → проблема на стороне стрима/Polymarket (нет котировок между); стратегия по таким рынкам нереализуема.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
