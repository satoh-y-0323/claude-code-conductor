#!/usr/bin/env python3
"""「モード:」行の有効性判定・純関数リファレンス実装（T4-3 / DC-GP-005）。

architecture-report-20260714-213000.md §3-3（有効性判定 3 条件）・§3-4(b)（パス用
封じ込め型防御）の仕様を、入出力テスト可能な純関数として切り出したもの。

**位置づけ（重要・§3-3 の注記を参照）**: 本 feature の実装は skill 文書＋LLM 解釈であり、
`モード:` 行を実行時に解釈する主体は文書を読んだ親 Claude / init-session の LLM 判断である。
本関数は実運用のデータフローからは呼ばれない。したがって**誤発動ゼロの一次防御ではない**
（一次防御は test-report の 5a 手動検証チェックリスト）。本関数は判定仕様の**回帰検出・
リファレンス実装**として存在する。将来モード行解釈が production コード（hook / c3 サブコマンド）
に落ちた時点で、この純関数が実行時経路へ昇格し得る。

有効性判定 3 条件（architecture §3-3・逐語）:
    1. 行が "モード: 自律 " で始まる
    2. "plan=" トークンがあり plan-path が非空
    3. plan-path を封じ込め検査（改行/NUL/制御文字検出時無効化 → realpath 正規化 →
       許可ルート配下判定）に通し、許可ルート配下に正規化できたうえで実在する

いずれか 1 つでも欠ければ無効（HITL 扱い）。

CLI 呼び出し契約（SR-AI-001・SKILL.md の機械実行必須化から呼ばれる）:
    - 入力: **標準入力からモード行 1 行**を読み取る（argv は使わない。シェルクォート・
      空白入りパス・メタ文字による事故を避けるため。NUL/改行境界の一般則と整合）。
    - 出力: 判定結果を機械可読の 1 行で stdout に返す（フィールド区切りは TAB）。
        * 有効: ``VALID<TAB>{正規化済み絶対パス}`` / exit code 0
        * 無効: ``INVALID<TAB>{理由コード}`` / exit code 1
          理由コード: ``bad_prefix`` / ``no_plan_token`` / ``unclosed_quote`` /
          ``control_char`` / ``outside_allowed_root`` / ``not_found``
    - 呼び出し例:
        ``printf '%s' "$MODE_LINE" | python .claude/skills/autonomous-mode/scripts/mode_line.py``
"""
from __future__ import annotations

import os
import re
import sys

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
# CLI 契約はモード行（日本語プレフィックス "モード: 自律 "）を stdin から受ける。
# Windows では stdin 既定が cp932 のため、Bash ツール等が送る UTF-8 バイト列を
# 誤デコードして常に bad_prefix（fail-closed だが有効宣言も一律無効化）となる。
# stdin も UTF-8 に固定して呼び出し契約を環境非依存にする。
if sys.stdin and hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")

# 許可ルート既定値（architecture §3-4(b)）: 委任プランの正規永続先のみ。
# テストは monkeypatch でこの定数を差し替えて密閉化する（サイクル 3 DC-GP-001）。
DEFAULT_ALLOWED_ROOT: str = os.path.expanduser(os.path.join("~", ".claude", "plans"))

_MODE_PREFIX = "モード: 自律 "
_PLAN_TOKEN_PREFIX = "plan="


def _extract_plan_path(rest: str) -> tuple[str | None, str | None]:
    """"自律 " より後ろのトークン列から plan= の値を取り出す。

    Returns:
        (plan_path, reason_code) の tuple。
        - 有効: (path_value, None)
        - 引用符閉じ忘れ: (None, "unclosed_quote")
        - plan= 欠落: (None, None)

    引用符優先: plan= 直後が " なら次の " までを plan 値とし、閉じ " が無ければ unclosed_quote。
    引用符なし: 従来どおり最初の " cycles=" または行末までを plan 値とする。
    """
    # plan= を探す
    plan_idx = rest.find("plan=")
    if plan_idx == -1:
        return None, None

    after_plan_eq = rest[plan_idx + 5:]  # "plan=" の 5 文字後ろから

    # 引用符優先パース
    if after_plan_eq.startswith('"'):
        # 引用符で始まる → 閉じ引用符を探す
        closing_quote_idx = after_plan_eq.find('"', 1)  # インデックス 0 のダブルクォート以降を検索
        if closing_quote_idx == -1:
            # 閉じ引用符がない → unclosed_quote エラー
            return None, "unclosed_quote"
        # 引用符内の内容（引用符を除外）を返す
        value = after_plan_eq[1:closing_quote_idx]
        return value or None, None

    # 引用符なし（従来形）: 最初の " cycles=" または行末まで
    match = re.search(r'(.*?)(?:\s+cycles=|$)', after_plan_eq)
    if match:
        value = match.group(1).strip()
        return value or None, None
    return None, None


def _has_control_chars(value: str) -> bool:
    """改行・NUL・その他 C0/DEL 制御文字を含むか。"""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _classify_mode_line(
    line: str, allowed_root: str | None = None
) -> tuple[bool, str]:
    """モード行を判定し ``(valid, detail)`` を返す（CLI 契約・純関数の共通実装）。

    ``valid`` が True のとき ``detail`` は正規化済み絶対パス。False のとき
    ``detail`` は理由コード（``bad_prefix`` / ``no_plan_token`` /
    ``unclosed_quote`` / ``control_char`` / ``outside_allowed_root`` / ``not_found``）。
    ``is_valid_autonomous_mode`` はこの関数の bool 部分を返す薄いラッパで、
    判定ロジックの単一の情報源はここに集約する。
    """
    root = allowed_root if allowed_root is not None else DEFAULT_ALLOWED_ROOT

    # 条件 1: "モード: 自律 " で始まる
    if not line.startswith(_MODE_PREFIX):
        return False, "bad_prefix"
    rest = line[len(_MODE_PREFIX):]

    # 条件 2: plan= トークンがあり非空・引用符対応
    plan_path, extract_reason = _extract_plan_path(rest)
    if extract_reason == "unclosed_quote":
        # 引用符閉じ忘れ
        return False, "unclosed_quote"
    if not plan_path:
        return False, "no_plan_token"

    # 条件 3 前段: パス用封じ込め型防御（改行/NUL/制御文字検出時無効化 → realpath 正規化 → 許可ルート判定）
    if _has_control_chars(plan_path):
        return False, "control_char"

    expanded = os.path.expanduser(plan_path)
    try:
        real_plan = os.path.realpath(expanded)
        real_root = os.path.realpath(root)
    except OSError:
        return False, "not_found"

    try:
        common = os.path.commonpath([real_plan, real_root])
    except ValueError:
        # 異なるドライブ（Windows）など比較不能 → 許可ルート外として無効
        return False, "outside_allowed_root"
    if common != real_root:
        return False, "outside_allowed_root"

    # 条件 3 後段: 実在確認
    if not os.path.isfile(real_plan):
        return False, "not_found"
    return True, real_plan


def is_valid_autonomous_mode(line: str, allowed_root: str | None = None) -> bool:
    """「モード:」行が自律モードとして有効かを判定する（純関数）。

    Args:
        line: session.tmp の「モード: ...」行 1 行分（末尾改行を含まない）。
        allowed_root: 許可ルートディレクトリ。None なら ``DEFAULT_ALLOWED_ROOT``
            （呼び出し時点のモジュール属性を参照するため、テストは
            ``monkeypatch.setattr(mode_line, "DEFAULT_ALLOWED_ROOT", ...)`` で
            差し替えられる）。

    Returns:
        3 条件すべてを満たせば True（有効・自律継続）。1 つでも欠ければ False（無効・HITL）。
    """
    valid, _detail = _classify_mode_line(line, allowed_root)
    return valid


if __name__ == "__main__":
    # CLI 呼び出し契約（docstring 参照）: 標準入力からモード行 1 行を読み取り、
    # 判定結果を機械可読 1 行（VALID/INVALID + TAB 区切り詳細）で返す。argv は使わない。
    # 読み取り〜判定を try/except で包み、UnicodeDecodeError 等の例外時も生の
    # トレースバックを stdout/stderr に出さず INVALID\tdecode_error / exit 1 の
    # 契約内に封じ込める（fail-closed）。
    try:
        # readline に上限 65536 を付与。上限で切れた行は通常判定へ流れ、封じ込め検査
        # （制御文字・許可ルート判定・実在確認）で無効化される（fail-closed）。
        _raw = sys.stdin.readline(65536)
        _line = _raw.rstrip("\r\n")  # 行終端子のみ除去（内部の文字は封じ込め検査に委ねる）
        _valid, _detail = _classify_mode_line(_line)
    except Exception:
        print("INVALID\tdecode_error")
        sys.exit(1)
    if _valid:
        print(f"VALID\t{_detail}")
        sys.exit(0)
    print(f"INVALID\t{_detail}")
    sys.exit(1)
