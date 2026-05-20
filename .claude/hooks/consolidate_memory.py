#!/usr/bin/env python3
"""Stop hook: consolidate the last N days of session memory into a summary.

memory-consolidation: 過去 N 日分の `.claude/memory/sessions/YYYYMMDD.tmp` から
- ``## うまくいったアプローチ``
- ``## 試みたが失敗したアプローチ``
の各セクションを集約し、`.claude/memory/consolidated_summary.md` に出力する。

設計判断:
- patterns.json の粒度判定や自動 promotion には介入しない（既存 stop.py の trust_score 計算ロジックを維持）。
- 出力先は auto-memory ではなく、プロジェクトローカルの
  `.claude/memory/consolidated_summary.md`。auto-memory の物理パスは
  Claude Code 側で決まるため触らない。
- 集約方法は単純な行マージ（重複行除去 + 空行除去）。
- Phase 2-B: patterns.json から promotion 候補をログ出力する。
- Phase 2-A: 古い session.tmp を archive/ に移動する。
- 失敗してもセッションを止めない（exit 0）。

呼び出し:
- `.claude/settings.json` の `Stop` hook 配列に登録される。
- stdin から JSON payload を受け取るが、内容は使わない（情報源は session ファイルのみ）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
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

# memory-consolidation Phase 2-A: archive 機能の生存期間（日）。
# DEFAULT_WINDOW_DAYS の 3 倍。要約ウィンドウから外れた直後すぐに archive せず、
# 過去サマリ再生成のための猶予を確保する。
# 環境変数 ``C3_CONSOLIDATE_ARCHIVE_TTL_DAYS`` で上書き可能。
DEFAULT_ARCHIVE_TTL_DAYS = DEFAULT_WINDOW_DAYS * 3

# 出力先（プロジェクトローカル）
OUTPUT_FILE_NAME = "consolidated_summary.md"

# memory-consolidation Phase 2-B: 半自動 promotion 候補ログの出力ファイル名
PROMOTION_CANDIDATES_FILE_NAME = "promotion-candidates.md"

# 候補ログの description 列の最大文字数（表セルの可読性確保）
_PROMOTION_DESC_MAX_LEN = 80

# 候補ログの ID 列の最大文字数（表セル幅を抑える、id が極端に長い場合の保険）
_PROMOTION_CID_MAX_LEN = 60

# 集約対象セクション
TARGET_SECTIONS = ("うまくいったアプローチ", "試みたが失敗したアプローチ")


_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
SESSIONS_DIR = os.path.join(_CLAUDE_DIR, "memory", "sessions")
OUTPUT_PATH = os.path.join(_CLAUDE_DIR, "memory", OUTPUT_FILE_NAME)
ARCHIVE_DIR = os.path.join(_CLAUDE_DIR, "memory", "archive")
PATTERNS_PATH = os.path.join(_CLAUDE_DIR, "memory", "patterns.json")
PROMOTION_CANDIDATES_PATH = os.path.join(
    _CLAUDE_DIR, "memory", PROMOTION_CANDIDATES_FILE_NAME
)

# Stop hook の stdin payload に対する上限（1 MB）[SR-V-001]
MAX_STDIN_BYTES = 1 * 1024 * 1024

# Markdown インジェクション・ターミナルエスケープ防止のため除去する制御文字パターン [SR-V-001] [SR-AI-001]
# - ASCII 制御文字: \x00-\x08, \x0b (VT), \x0c (FF), \x0e-\x1f, \x7f (DEL)
# - Latin-1 補完制御文字: \x80-\x9f
# - Unicode 行/段落区切り: U+2028 (LINE SEPARATOR), U+2029 (PARAGRAPH SEPARATOR)
# - Zero-Width 系: U+200B-U+200F
# - BOM: U+FEFF
# 除外: \x09 (TAB) は _sanitize_field でスペースに置換、\x0a (LF), \x0d (CR) も同様
_CONTROL_CHARS_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f"
    "\x80-\x9f"
    "  "
    "​-‏"
    "﻿]"
)


def _load_session_utils():
    """session_utils モジュールを動的にロードして返す（同階層）。"""
    import importlib.util

    util_path = os.path.join(_HOOKS_DIR, "session_utils.py")
    spec = importlib.util.spec_from_file_location("session_utils", util_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"session_utils が見つかりません: {util_path}")
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
    elif not isinstance(today, datetime):
        today = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    if today.tzinfo is None:
        today = today.replace(tzinfo=timezone.utc)
    today_str = today.date().isoformat()
    start_str = (today.date() - timedelta(days=window_days - 1)).isoformat()

    lines: list[str] = [
        "# 集約サマリ",
        "",
        f"_直近 {window_days} 日（{start_str} 〜 {today_str}）の session ファイル {len(files)} 件をマージ_",
        f"_最終更新: {today.isoformat(timespec='seconds')}_",
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
    patterns_path: str | None = None,
) -> bool:
    """集約サマリを生成して指定パスに書き出す。

    memory-consolidation Phase 2-B: ``patterns_path`` が指定された場合、末尾に
    「## 昇格候補」サマリセクションを追加する（候補 ID + trust のみ、
    詳細は ``promotion-candidates.md`` を参照）。

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

    # Phase 2-B: 昇格候補サマリを末尾に追加
    if patterns_path is not None:
        try:
            section, _ = build_promotion_candidates_section(
                patterns_path, today=today
            )
            summary = summary.rstrip() + "\n\n" + section + "\n"
        except Exception as exc:  # noqa: BLE001
            print(
                f"[consolidate_memory:promotion] section build failed: {exc}",
                file=sys.stderr,
            )

    return _atomic_write(output_path, summary)


# ---------------------------------------------------------------------------
# memory-consolidation Phase 2-B: 半自動 promotion 候補ログ
# ---------------------------------------------------------------------------


def _load_patterns_readonly(patterns_path: str) -> list[dict]:
    """``patterns.json`` を読み込んで ``patterns`` 配列を返す。

    stop.py との競合を避けるため **読み込み専用**。ファイル不在 / JSON
    パース失敗 / スキーマ不正は空リストを返す（呼び出し元でハンドリング）。
    """
    if not os.path.isfile(patterns_path):
        return []
    try:
        with open(patterns_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"[consolidate_memory:promotion] failed to load {patterns_path}: {exc}",
            file=sys.stderr,
        )
        return []
    if not isinstance(data, dict):
        return []
    patterns = data.get("patterns", [])
    if not isinstance(patterns, list):
        print(
            f"[consolidate_memory] patterns.json: expected list under 'patterns', got {type(patterns).__name__}",
            file=sys.stderr,
        )
        return []
    return [p for p in patterns if isinstance(p, dict)]


def _sanitize_field(value: str) -> str:
    """Markdown インジェクション・制御文字インジェクション防止のためのサニタイズ。

    除去対象 [SR-V-001] [SR-AI-001]:
    - CR/LF（改行）→ スペースに置換
    - タブ (\\t) → スペースに置換
    - null byte / その他 ASCII 制御文字 (\\x00-\\x08, \\x0b, \\x0c, \\x0e-\\x1f, \\x7f) → 削除

    Args:
        value: サニタイズ対象の文字列。

    Returns:
        制御文字を除去・置換した文字列。
    """
    sanitized = (
        value.replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("\t", " ")
    )
    return _CONTROL_CHARS_RE.sub("", sanitized)


def _truncate_for_table(text: str, limit: int = _PROMOTION_DESC_MAX_LEN) -> str:
    r"""Markdown 表セル用に文字列を整形する。

    処理順:
      1. 改行 (CR / LF / CRLF) を半角スペースに置換
      2. ``limit`` 文字超過なら末尾を ``…`` で切り詰め（**エスケープ前**）
      3. パイプ ``|`` とバッククォート ``\``` をバックスラッシュエスケープ

    ``limit`` は **エスケープ前の文字数** を意味する。エスケープ後は
    最大 2 倍弱に膨らむ可能性があるが、テーブルセル内表示としては許容。
    """
    flat = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    if len(flat) > limit:
        flat = flat[:limit].rstrip() + "…"
    # `|` と backtick の両方をエスケープ（インラインコードの閉じ忘れ対策）
    return flat.replace("|", r"\|").replace("`", r"\`")


def build_promotion_candidates_section(
    patterns_path: str,
    *,
    today: datetime | None = None,
) -> tuple[str, list[dict]]:
    """consolidated_summary.md 末尾に追加するサマリセクションを返す。

    Args:
        patterns_path: ``patterns.json`` のパス。
        today: 「今日」の基準日（ヘッダ表示用）。省略時は現在 UTC。

    Returns:
        ``(section_markdown, candidates)``。
        ``candidates`` は ``promotion_candidate=true`` かつ ``promoted!=true``
        のパターン dict のリスト（出現順）。
    """
    if today is None:
        today = datetime.now(timezone.utc)
    today_str = (today.date() if isinstance(today, datetime) else today).isoformat()

    patterns = _load_patterns_readonly(patterns_path)
    candidates = [
        p for p in patterns
        if p.get("promotion_candidate") is True and not p.get("promoted", False)
    ]

    lines: list[str] = ["## 昇格候補", ""]
    if not candidates:
        lines.append(f"_候補数: 0 / 最終確認: {today_str}_")
        lines.append("")
        lines.append("_該当エントリなし_")
        return "\n".join(lines), candidates

    lines.append(
        f"_候補数: {len(candidates)} / 最終確認: {today_str} / "
        f"詳細は `.claude/memory/{PROMOTION_CANDIDATES_FILE_NAME}` を参照_"
    )
    lines.append("")
    for c in candidates:
        cid = c.get("id", "?")
        trust = c.get("trust_score", 0.0)
        try:
            trust_str = f"{float(trust):.2f}"
        except (TypeError, ValueError):
            trust_str = "?"
        lines.append(f"- `{cid}` (trust {trust_str})")
    return "\n".join(lines), candidates


def _extract_candidate_fields(c: dict) -> dict:
    """候補 dict から表示用フィールドを抽出する（DRY ヘルパー）。

    Returns:
        ``{"cid", "trust_str", "obs_count", "registered", "last_updated",
            "description"}`` のキーを持つ dict。
    """
    cid = str(c.get("id", "?"))
    trust = c.get("trust_score", 0.0)
    try:
        trust_str = f"{float(trust):.2f}"
    except (TypeError, ValueError):
        trust_str = "?"
    obs = c.get("observations") or []
    obs_count = len(obs) if isinstance(obs, list) else 0
    registered = str(c.get("registered_date", "?"))
    last_updated = str(c.get("last_updated", registered))
    description = str(c.get("description", ""))
    return {
        "cid": cid,
        "trust_str": trust_str,
        "obs_count": obs_count,
        "registered": registered,
        "last_updated": last_updated,
        "description": description,
    }


def write_promotion_candidates_log(
    candidates: list[dict],
    output_path: str = PROMOTION_CANDIDATES_PATH,
    *,
    today: datetime | None = None,
) -> bool:
    """``promotion-candidates.md`` を書き出す（毎回上書き）。

    候補 0 件でも「候補なし」ファイルを必ず出力する（前回出力を上書き
    することで古い候補が残り続けるのを防ぐ）。

    アトミック書き込み: ``tempfile.mkstemp`` + ``os.replace`` パターン。

    ``today`` が指定されたときはヘッダの「最終更新」タイムスタンプに
    使用する（テスト時の決定論性を確保）。省略時は現在 UTC。
    """
    if today is None:
        today = datetime.now(timezone.utc)
    elif not isinstance(today, datetime):
        today = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    if today.tzinfo is None:
        today = today.replace(tzinfo=timezone.utc)
    now_iso = today.isoformat(timespec="seconds")

    lines: list[str] = [
        "# 昇格候補一覧",
        "",
        f"_最終更新: {now_iso} / 候補数: {len(candidates)}_",
        "",
        "`promotion_candidate: true` かつ `promoted` 未設定のパターンを表示します。",
        "昇格するには `/promote-pattern` skill を実行してください。",
        "",
    ]

    if not candidates:
        lines.append("_候補なし_")
        lines.append("")
    else:
        # 表セクション
        lines.append("| ID | trust | 観測 | 登録日 | 説明 |")
        lines.append("|---|---|---|---|---|")
        for c in candidates:
            f = _extract_candidate_fields(c)
            cid_disp = _truncate_for_table(
                _sanitize_field(f["cid"]), limit=_PROMOTION_CID_MAX_LEN
            )
            safe_registered = _sanitize_field(f["registered"])
            desc = _truncate_for_table(_sanitize_field(f["description"]))
            lines.append(
                f"| `{cid_disp}` | {f['trust_str']} | "
                f"{f['obs_count']} | {safe_registered} | {desc} |"
            )
        lines.append("")
        # 詳細セクション（コピペ用）
        lines.append("---")
        lines.append("")
        lines.append("## 詳細（コピペ用）")
        lines.append("")
        for c in candidates:
            f = _extract_candidate_fields(c)
            safe_cid = _sanitize_field(f["cid"])
            safe_desc = _sanitize_field(f["description"])
            safe_registered = _sanitize_field(f["registered"])
            safe_last_updated = _sanitize_field(f["last_updated"])
            lines.append(f"### {safe_cid}  [trust {f['trust_str']}]")
            lines.append(
                f"- 登録日: {safe_registered} / 最終更新: {safe_last_updated} / "
                f"観測: {f['obs_count']} 件"
            )
            lines.append(f"- {safe_desc}")
            lines.append("")

    payload = "\n".join(lines).rstrip() + "\n"
    return _atomic_write(output_path, payload)


def _atomic_write(output_path: str, payload: str) -> bool:
    """tempfile + os.replace でアトミックに書き込む。失敗時は False。"""
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
    except OSError as exc:
        print(
            f"[consolidate_memory] failed to create dir for {output_path}: {exc}",
            file=sys.stderr,
        )
        return False
    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp_", dir=os.path.dirname(output_path)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_path, output_path)
    except OSError as exc:
        print(
            f"[consolidate_memory] failed to write {output_path}: {exc}",
            file=sys.stderr,
        )
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        return False
    return True


# ---------------------------------------------------------------------------
# memory-consolidation Phase 2-A: archive 機能
# ---------------------------------------------------------------------------


def archive_old_sessions(
    sessions_dir: str = SESSIONS_DIR,
    archive_dir: str = ARCHIVE_DIR,
    *,
    ttl_days: int = DEFAULT_ARCHIVE_TTL_DAYS,
    today: datetime | None = None,
) -> list[str]:
    """``ttl_days`` 日以上経過した session.tmp を ``archive_dir`` に移動する。

    memory-consolidation Phase 2-A: session ファイルの永久蓄積を防ぐ。
    同一 FS 内の ``shutil.move`` を使うため rename は基本的にアトミック。

    Args:
        sessions_dir: 移動元ディレクトリ。``YYYYMMDD.tmp`` 形式のファイル群。
        archive_dir: 移動先ディレクトリ。存在しなければ自動生成。
        ttl_days: 何日以上経過したファイルを archive 対象にするか。
            ``today - file_date >= ttl_days`` で判定。
        today: 「今日」の基準日。省略時は ``datetime.now(UTC)``。

    Returns:
        移動に成功した archive 先パスのリスト。
        個別の移動失敗（OSError）は警告のみで継続するため、
        対象だが失敗したファイルはリストに含まれない。
    """
    if not os.path.isdir(sessions_dir):
        return []
    if today is None:
        today = datetime.now(timezone.utc).date()
    elif isinstance(today, datetime):
        today = today.date()

    targets: list[tuple[str, str]] = []  # (src_path, base_name)
    for name in os.listdir(sessions_dir):
        if not name.endswith(".tmp"):
            continue
        stem = name[:-4]
        try:
            d = datetime.strptime(stem, "%Y%m%d").date()
        except ValueError:
            continue
        if (today - d).days >= ttl_days:
            targets.append((os.path.join(sessions_dir, name), name))

    if not targets:
        return []

    try:
        os.makedirs(archive_dir, exist_ok=True)
    except OSError as exc:
        print(
            f"[consolidate_memory] failed to create archive dir {archive_dir}: {exc}",
            file=sys.stderr,
        )
        return []

    moved: list[str] = []
    for src_path, base_name in targets:
        dst_path = _resolve_archive_dest(archive_dir, base_name)
        try:
            shutil.move(src_path, dst_path)
        except OSError as exc:
            print(
                f"[consolidate_memory] failed to archive {src_path}: {exc}",
                file=sys.stderr,
            )
            continue
        moved.append(dst_path)
    return moved


def _resolve_archive_ttl() -> int:
    """``C3_CONSOLIDATE_ARCHIVE_TTL_DAYS`` を安全に解決する。

    不正値・0 以下の値は受け付けず、警告ログ + デフォルトに戻す（[SR-V-001]）。
    """
    raw = os.environ.get("C3_CONSOLIDATE_ARCHIVE_TTL_DAYS")
    if raw is None or raw == "":
        return DEFAULT_ARCHIVE_TTL_DAYS
    try:
        ttl = int(raw)
    except ValueError:
        print(
            f"[consolidate_memory:archive] invalid C3_CONSOLIDATE_ARCHIVE_TTL_DAYS={raw!r}, "
            f"using default {DEFAULT_ARCHIVE_TTL_DAYS}",
            file=sys.stderr,
        )
        return DEFAULT_ARCHIVE_TTL_DAYS
    if ttl < 1:
        print(
            f"[consolidate_memory:archive] C3_CONSOLIDATE_ARCHIVE_TTL_DAYS={ttl} < 1, "
            f"using default {DEFAULT_ARCHIVE_TTL_DAYS} to prevent archiving all sessions",
            file=sys.stderr,
        )
        return DEFAULT_ARCHIVE_TTL_DAYS
    return ttl


def _resolve_archive_dest(archive_dir: str, base_name: str) -> str:
    """同名衝突時に ``YYYYMMDD-{N}.tmp`` で別名を返す。

    既存ファイルが無ければ ``base_name`` のままを返す。
    suffix が増え続けないよう N=1..1000 で打ち止め（保険）。
    """
    candidate = os.path.join(archive_dir, base_name)
    if not os.path.exists(candidate):
        return candidate
    stem = base_name[:-4]  # ".tmp" を除く
    for n in range(1, 1001):
        candidate = os.path.join(archive_dir, f"{stem}-{n}.tmp")
        if not os.path.exists(candidate):
            return candidate
    # 1000 件全て埋まっている異常系: 最後のパスを返して上書きさせる
    # （shutil.move 側で OSError になっても archive_old_sessions が捕捉する）
    return candidate




def run_sync(today: datetime | None = None) -> int:
    """payload に依存しない sync 処理本体。session_stop.py からも呼ばれる.

    同期で実行する処理:
      - MVP セクション集約 + 昇格候補サマリの consolidated_summary.md 書き出し
      - Phase 2-B: promotion-candidates.md 書き出し
      - Phase 2-A: 古い session.tmp の archive/ 移動

    Args:
        today: 集約対象日付（省略時は現在時刻）。session_stop.py から
               複数フェーズで同じ today を共有したい場合に注入する。

    Returns:
        int: 常に 0（失敗してもセッションは止めない設計のため）。
    """
    # main() 全体で同じ "today" を共有する（datetime.now() の二重評価回避 + 決定論性）
    if today is None:
        today = datetime.now(timezone.utc)

    # 同期: MVP + 昇格候補サマリを生成
    try:
        write_summary(
            patterns_path=PATTERNS_PATH,
            today=today,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[consolidate_memory] unexpected error: {exc}", file=sys.stderr)

    # 同期: Phase 2-B 半自動 promotion 候補ログ
    try:
        _, candidates = build_promotion_candidates_section(
            PATTERNS_PATH, today=today
        )
        write_promotion_candidates_log(
            candidates, PROMOTION_CANDIDATES_PATH, today=today
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[consolidate_memory:promotion] unexpected error: {exc}",
            file=sys.stderr,
        )

    # 同期: Phase 2-A 古い session.tmp を archive/ へ移動
    try:
        ttl = _resolve_archive_ttl()
        archive_old_sessions(ttl_days=ttl)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[consolidate_memory:archive] unexpected error: {exc}",
            file=sys.stderr,
        )

    return 0


def main() -> int:
    """Stop フックエントリポイント。失敗してもセッションを止めない（exit 0）。

    consolidate_memory.py が単独実行されたときの互換維持用。
    session_stop.py orchestrator からは run_sync() が直接呼ばれる。
    """
    # stdin の payload は読むが内容は使わない（呼び出し元の Claude Code から送られる）
    # 1 MB 上限: 超過時は stderr に警告して即 return 0（セッションは止めない）[SR-V-001]
    try:
        raw = sys.stdin.read()
        if len(raw) > MAX_STDIN_BYTES:
            print(
                f"[consolidate_memory] stdin payload exceeds max {MAX_STDIN_BYTES} bytes; aborting",
                file=sys.stderr,
            )
            return 0
    except Exception:  # noqa: BLE001
        pass
    return run_sync()


if __name__ == "__main__":
    sys.exit(main())
