#!/usr/bin/env python3
"""Context gauge statusline script.
Displays context usage + optional rate limit gauges (when plan provides rate_limits data).
"""

import json
import sys
import threading
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
sys.stdin.reconfigure(encoding='utf-8')

MAX_INPUT = 64 * 1024  # 64 KB

# ANSI color / style codes
GREEN  = '\x1b[32m'
RED    = '\x1b[31m'
YELLOW = '\x1b[33m'
ORANGE = '\x1b[38;5;208m'
DIM    = '\x1b[2m'
RESET  = '\x1b[0m'

# Gauge characters
BLOCK       = '█'
BLOCK_EMPTY = '░'
TOTAL_CELLS = 10


def pct_color(pct: int) -> str:
    if pct > 90:
        return RED
    elif pct > 75:
        return ORANGE
    elif pct > 60:
        return YELLOW
    else:
        return GREEN


def build_gauge(pct: int) -> str:
    filled = min(pct // 10, TOTAL_CELLS)
    empty  = TOTAL_CELLS - filled
    color  = pct_color(pct)
    return (
        DIM + '[' + RESET +
        color + BLOCK * filled + RESET +
        DIM + BLOCK_EMPTY * empty + RESET +
        DIM + ']' + RESET
    )


def format_reset_time(resets_at) -> str:
    if not resets_at:
        return ''
    try:
        if isinstance(resets_at, (int, float)):
            ts_sec = float(resets_at)
        else:
            ts_sec = datetime.fromisoformat(
                str(resets_at).replace('Z', '+00:00')
            ).timestamp()
        now_sec = datetime.now(timezone.utc).timestamp()
        diff_sec = int(ts_sec - now_sec)
    except Exception:
        return ''

    if diff_sec <= 0:
        return 'reset'

    days  = diff_sec // 86400
    hours = (diff_sec % 86400) // 3600
    mins  = (diff_sec % 3600) // 60

    if days > 0:
        return f'{days}d {hours}h'
    if hours > 0:
        return f'{hours}h {mins}m'
    return f'{mins}m'


def render_output(raw: str) -> None:
    data: dict = {}
    try:
        data = json.loads(raw)
    except Exception:
        pass

    ctx_window = data.get('context_window') or {}
    ctx_pct = round(ctx_window.get('used_percentage') or 0)

    parts = [
        DIM + 'context usage:' + RESET + ' ' +
        build_gauge(ctx_pct) + ' ' +
        pct_color(ctx_pct) + str(ctx_pct) + '%' + RESET
    ]

    rate_limits = data.get('rate_limits')
    if rate_limits:
        five_hour = (
            rate_limits.get('five_hour') or
            rate_limits.get('5h') or
            rate_limits.get('fiveHour')
        )
        if five_hour:
            pct = round(five_hour.get('used_percentage') or 0)
            reset_str = format_reset_time(five_hour.get('resets_at'))
            part = (
                DIM + '5hour limits:' + RESET + ' ' +
                build_gauge(pct) + ' ' +
                pct_color(pct) + str(pct) + '%' + RESET
            )
            if reset_str:
                part += ' ' + DIM + reset_str + RESET
            parts.append(part)

        seven_day = (
            rate_limits.get('seven_day') or
            rate_limits.get('7d') or
            rate_limits.get('sevenDay')
        )
        if seven_day:
            pct = round(seven_day.get('used_percentage') or 0)
            reset_str = format_reset_time(seven_day.get('resets_at'))
            part = (
                DIM + '7day limits:' + RESET + ' ' +
                build_gauge(pct) + ' ' +
                pct_color(pct) + str(pct) + '%' + RESET
            )
            if reset_str:
                part += ' ' + DIM + reset_str + RESET
            parts.append(part)

    sys.stdout.write('  '.join(parts) + '\n')
    sys.stdout.flush()


def main() -> None:
    chunks = []
    total_size = 0
    rendered = False

    def do_render():
        nonlocal rendered
        if rendered:
            return
        rendered = True
        render_output(''.join(chunks))

    # Timeout fallback: render with whatever we have after 5 seconds
    timer = threading.Timer(5.0, do_render)
    timer.daemon = True
    timer.start()

    try:
        for line in sys.stdin:
            chunks.append(line)
            total_size += len(line)
            if total_size > MAX_INPUT:
                break
    except Exception:
        pass
    finally:
        timer.cancel()
        do_render()


if __name__ == '__main__':
    main()
