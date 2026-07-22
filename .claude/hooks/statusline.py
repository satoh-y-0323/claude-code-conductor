#!/usr/bin/env python3
"""Statusline script for Claude Code.

Displays: [model display name] effort | ctx used X% | 5h lim X% | 7d lim X%

context_window_size (200K / 1M) は ctx used X% と情報重複のため表示しない。
gauge バー描画も省スペース優先で表示しない。
"""

import json
import sys
import threading
from datetime import datetime, timezone
from typing import Any

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


def pct_color(pct: int) -> str:
    if pct > 90:
        return RED
    elif pct > 75:
        return ORANGE
    elif pct > 60:
        return YELLOW
    else:
        return GREEN


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
    data: dict[str, Any] = {}
    try:
        data = json.loads(raw)
    except Exception:
        pass

    header: list[str] = []
    metrics: list[str] = []

    # [model display name]  effort  — スペース区切り
    # （context_window_size は ctx used X% と情報重複のため表示しない）
    model = data.get('model') or {}
    display_name = model.get('display_name', '')
    if display_name:
        header.append(f'[{display_name}]')

    # effort level
    effort = data.get('effort') or {}
    effort_level = effort.get('level', '')
    if effort_level:
        header.append(effort_level)

    # ctx usg %
    ctx_window = data.get('context_window') or {}
    ctx_pct = round(ctx_window.get('used_percentage') or 0)
    metrics.append('ctx used ' + pct_color(ctx_pct) + str(ctx_pct) + '%' + RESET)

    # rate limits
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
            part = '5h lim ' + pct_color(pct) + str(pct) + '%' + RESET
            if reset_str:
                part += ' ' + DIM + '(' + reset_str + ')' + RESET
            metrics.append(part)

        seven_day = (
            rate_limits.get('seven_day') or
            rate_limits.get('7d') or
            rate_limits.get('sevenDay')
        )
        if seven_day:
            pct = round(seven_day.get('used_percentage') or 0)
            reset_str = format_reset_time(seven_day.get('resets_at'))
            part = '7d lim ' + pct_color(pct) + str(pct) + '%' + RESET
            if reset_str:
                part += ' ' + DIM + '(' + reset_str + ')' + RESET
            metrics.append(part)

    output_parts: list[str] = []
    if header:
        output_parts.append(' '.join(header))  # nul-boundary: allow(ステータスライン先頭部の表示文字列。読み手は Claude Code の表示でリポジトリ内に split 側がない)
    output_parts.extend(metrics)
    sys.stdout.write(' | '.join(output_parts) + '\n')  # nul-boundary: allow(ステータスライン 1 行の区切り表示。区切り文字が表示書式そのもの)
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
            total_size += len(line.encode('utf-8'))
            if total_size > MAX_INPUT:
                overflow = total_size - MAX_INPUT
                last_bytes = chunks[-1].encode('utf-8')
                # バイト単位で切り詰め。errors='replace' でマルチバイト境界をまたいだ場合も安全に処理する
                keep = len(last_bytes) - overflow
                chunks[-1] = last_bytes[:keep].decode('utf-8', errors='replace')
                break
    except Exception:
        pass
    finally:
        timer.cancel()
        do_render()


if __name__ == '__main__':
    main()
