#!/usr/bin/env python3
"""Stop hook: consolidate the last N days of session memory into a summary.

F-004 MVP: 過去 N 日分の `.claude/memory/sessions/YYYYMMDD.tmp` から
- ``## うまくいったアプローチ``
- ``## 試みたが失敗したアプローチ``
の各セクションを集約し、`.claude/memory/consolidated_summary.md` に出力する。

設計判断（MVP スコープ）:
- patterns.json の粒度判定や自動 promotion には介入しない（既存 stop.py の trust_score 計算ロジックを維持）。
- 出力先は auto-memory ではなく、プロジェクトローカルの
  `.claude/memory/consolidated_summary.md`。auto-memory の物理パスは
  Claude Code 側で決まるため、本 MVP では触らない。
- 集約方法は単純な行マージ（重複行除去 + 空行除去）。LLM 要約は使わない。
- 失敗してもセッションを止めない（exit 0）。

呼び出し:
- `.claude/settings.json` の `Stop` hook 配列に登録される。
- stdin から JSON payload を受け取るが、内容は使わない（情報源は session ファイルのみ）。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


# 集約ウィンドウ（直近何日分の session ファイルを対象にするか）
DEFAULT_WINDOW_DAYS = 7

# 出力先（プロジェクトローカル）
OUTPUT_FILE_NAME = "consolidated_summary.md"

# 集約対象セクション
TARGET_SECTIONS = ("うまくいったアプローチ", "試みたが失敗したアプローチ")


_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
SESSIONS_DIR = os.path.join(_CLAUDE_DIR, "memory", "sessions")
OUTPUT_PATH = os.path.join(_CLAUDE_DIR, "memory", OUTPUT_FILE_NAME)


def _load_session_utils():
    """session_utils モジュールを動的にロードして返す（同階層）。"""
    import importlib.util

    util_path = os.path.join(_HOOKS_DIR, "session_utils.py")
    spec = importlib.util.spec_from_file_location("session_utils", util_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def list_recent_session_files(
    sessions_dir: str = SESSIONS_DIR,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    today: datetime | None = None,
) -> list[str]:
    """``YYYYMMDD.tmp`` 形式のうち、直近 ``window_days`` 日分のパスを返す。

    ファイル名から日付を解釈する。日付として読めないものは無視する。
    返り値は古い順（後で集約結果に時系列で並べるため）。
    """
    if not os.path.isdir(sessions_dir):
        return []
    if today is None:
        today = datetime.now(timezone.utc).date()
    elif isinstance(today, datetime):
        today = today.date()
    cutoff = today - timedelta(days=window_days - 1)

    selected: list[tuple[datetime, str]] = []
    for name in os.listdir(sessions_dir):
        if not name.endswith(".tmp"):
            continue
        stem = name[:-4]
        try:
            d = datetime.strptime(stem, "%Y%m%d").date()
        except ValueError:
            continue
        if cutoff <= d <= today:
            selected.append((d, os.path.join(sessions_dir, name)))
    selected.sort(key=lambda t: t[0])
    return [p for _, p in selected]


def _collect_section_lines(
    files: list[str],
    section: str,
    extract_fn,
) -> list[str]:
    """各ファイルから指定セクションを抽出し、行単位でマージする。

    重複行・空行・末尾空白は除去する。出現順は保持する。
    """
    seen: dict[str, None] = {}
    for path in files:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        body = extract_fn(text, section)
        if not body:
            continue
        for line in body.splitlines():
            stripped = line.rstrip()
            if not stripped:
                continue
            seen.setdefault(stripped, None)
    return list(seen.keys())


def build_summary_markdown(
    files: list[str],
    *,
    window_days: int,
    extract_fn,
    today: datetime | None = None,
) -> str:
    """集約結果の Markdown を組み立てる。"""
    if today is None:
        today = datetime.now(timezone.utc)
    today_str = today.date().isoformat() if isinstance(today, datetime) else str(today)
    start_str = (today.date() - timedelta(days=window_days - 1)).isoformat() \
        if isinstance(today, datetime) else str(today)

    lines: list[str] = [
        "# 集約サマリ",
        "",
        f"_直近 {window_days} 日（{start_str} 〜 {today_str}）の session ファイル {len(files)} 件をマージ_",
        f"_最終更新: {datetime.now(timezone.utc).isoformat(timespec='seconds')}_",
        "",
        "本ファイルは `.claude/hooks/consolidate_memory.py` が Stop フックで自動生成する。",
        "重複行・空行を除去した単純マージのため、文脈は元の session ファイルを参照すること。",
        "",
    ]

    for section in TARGET_SECTIONS:
        section_lines = _collect_section_lines(files, section, extract_fn)
        lines.append(f"## {section}")
        lines.append("")
        if section_lines:
            lines.extend(section_lines)
        else:
            lines.append("_該当エントリなし_")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_summary(
    output_path: str = OUTPUT_PATH,
    *,
    sessions_dir: str = SESSIONS_DIR,
    window_days: int = DEFAULT_WINDOW_DAYS,
    today: datetime | None = None,
) -> bool:
    """集約サマリを生成して指定パスに書き出す。

    Returns:
        書き出し成功時 True、対象ファイル無し / I/O エラー時 False。
    """
    files = list_recent_session_files(
        sessions_dir, window_days=window_days, today=today
    )
    if not files:
        return False

    util = _load_session_utils()
    summary = build_summary_markdown(
        files,
        window_days=window_days,
        extract_fn=util.extract_section,
        today=today,
    )

    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(summary)
    except OSError as exc:
        print(
            f"[consolidate_memory] failed to write {output_path}: {exc}",
            file=sys.stderr,
        )
        return False
    return True


def main() -> int:
    """Stop フックエントリポイント。失敗してもセッションを止めない（exit 0）。"""
    # stdin の payload は読むが内容は使わない（呼び出し元の Claude Code から送られる）
    try:
        sys.stdin.read()
    except Exception:  # noqa: BLE001
        pass

    try:
        write_summary()
    except Exception as exc:  # noqa: BLE001
        print(f"[consolidate_memory] unexpected error: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
