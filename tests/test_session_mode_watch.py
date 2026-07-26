"""Tests for .claude/hooks/session_mode_watch.py (P3 hook)

plan-report-20260725-180252.md T1(3) / architecture-report-20260725-175915.md §2 D-3 の
仕様に基づく。

## Red フェーズの経緯（T2 developer 実装前）

T2 developer 実装前の時点では `.claude/hooks/session_mode_watch.py` が存在せず、
`_run_hook()` ヘルパーは対象ファイルが無ければ `FileNotFoundError` を送出する設計に
していた（`pytestmark skipif` は使わない。SKIP では Red の証跡が残らないため）。
当時は構文エラー・タイポによる失敗ではなく、hook 不在という「機能未実装」による
失敗だった（実装後の現在も、本ヘルパーは hook 不在時のフォールバックとして機能する）。

## 擬似リポジトリ方式について

P3 の判定はファイルパスの構造（`sessions/` を含み `.tmp` で終わるか）と Edit の
`old_string`/`new_string` の行差分のみに依存する。`Path(__file__)` からのプロジェクト
ルート導出は不要と判断したため、patterns_guard.py のようなコピー配置は行わず、
`tmp_path` 配下の任意パス文字列を直接 tool_input に渡す。実ファイルの実在は前提にしない。

## 状態遷移表（architecture D-3・単一の判定ソース） — 複数モード行への拡張

実効モード行は「最初の `^モード: ` 行」（HITL 行を含む）。警告判定は 3 条件の OR:
  1. (a) 挿入: old に `^モード: 自律` 行が 0 本、new に 1 本以上
  2. (b) 新出値: new の有効 plan= 値集合に old の集合に無い値がある
  3. (c) 実効行遷移: 各側の実効行ペアに単一行の状態遷移表を適用し、表が警告と定める遷移に該当

各行の状態は「行なし / E（値抽出可）/ N（値抽出不能・plan=欠落 or unclosed quote）」の
3 値。実効行ペアの判定は old/new の状態で決まる（単一行のケースは本表の (c) の特殊形）:

| old実効 | new実効 | 動作 |
|---|---|---|
| 行なし | 行あり(E/N) | 挿入として警告 |
| 行あり | 行なし | 沈黙（削除・HITL 復帰） |
| E | E(同値) | 沈黙（cycles= 更新） |
| E | E(異値) | 差し替えとして警告 |
| N | E | 差し替えとして警告 |
| E | N | 沈黙 |
| N | N | 沈黙 |
| 行なし | 行なし | 沈黙 |

## ケース仕様（plan T1(3) 準拠）

    1. Edit 以外（Write 含む）→ 沈黙
    2. sessions/ 配下の .tmp 以外 → 沈黙
    3. 壊れた JSON → 沈黙・exit 0
    4. 行単位判定（`^モード: 自律`・行中引用は非検知）
    5. 状態遷移表の全行をカバー
    6. 2 経路の検証: stderr `[SessionModeWatch WARN]` と stdout JSON
       (hookEventName == "PostToolUse"・additionalContext に検知種別・対象ファイル名)
    7. 「モード: HITL」のみ → 沈黙

## E 周回 1 修正サイクル追加分（T5 — plan-report §2-B）

    (e') F5: stderr / additionalContext のサニタイズを共有
         `_hook_utils.sanitize_for_terminal()`（C0+DEL+**C1**）へ置換した。
         追加当時のローカル `_sanitize` は `[\\x00-\\x1f\\x7f]`（C0+DEL のみ）で
         C1 制御文字（例 CSI = `\\x9b`）が素通りしていたため Red だった。
         独立した module-level test 関数として追加した（rework2 DC-AM-001）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

WORKTREE_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "session_mode_watch.py"

PLAN_A = "~/.claude/plans/plan-a.md"
PLAN_B = "~/.claude/plans/plan-b.md"
PLAN_SPACE_A = "~/.claude/plans/my plan.md"
PLAN_SPACE_B = "~/.claude/plans/other plan.md"
CYCLES_1 = "C-3/1,E/0"
CYCLES_2 = "C-3/2,E/0"


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------


def _run_hook(
    payload: dict | None,
    *,
    raw_stdin: str | None = None,
) -> subprocess.CompletedProcess:
    if not HOOK_PATH.is_file():
        raise FileNotFoundError(
            f"{HOOK_PATH} が存在しません（P3 hook 未実装のため Red）"
        )
    env: dict[str, str] = {"PYTHONIOENCODING": "utf-8"}
    for key in ("SYSTEMROOT", "PATH"):
        if key in os.environ:
            env[key] = os.environ[key]
    stdin_data = raw_stdin if raw_stdin is not None else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=stdin_data,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _edit_payload(file_path: str, old_string: str, new_string: str) -> dict:
    return {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": file_path,
            "old_string": old_string,
            "new_string": new_string,
        },
    }


def _write_payload(file_path: str, content: str) -> dict:
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": content},
    }


def _sessions_tmp_path(tmp_path: Path) -> str:
    return str(tmp_path / ".claude" / "memory" / "sessions" / "20260725.tmp")


def _non_sessions_path(tmp_path: Path) -> str:
    return str(tmp_path / ".claude" / "memory" / "notes.md")


def _wrong_extension_path(tmp_path: Path) -> str:
    return str(tmp_path / ".claude" / "memory" / "sessions" / "20260725.md")


def _mode_line(plan: str | None, cycles: str, *, quoted: bool = False) -> str:
    """行あり状態（E または N）のモード行を1行生成する（末尾改行なし）。"""
    if plan is None:
        return f"モード: 自律 cycles={cycles}"
    if quoted:
        return f'モード: 自律 plan="{plan}" cycles={cycles}'
    return f"モード: 自律 plan={plan} cycles={cycles}"


def _mode_line_custom_terminator(plan: str, cycles: str, terminator: str) -> str:
    """cycles= 前の空白パターンを任意に変えたモード行（引用符なし）。"""
    return f"モード: 自律 plan={plan}{terminator}cycles={cycles}"


def _unclosed_quote_line(plan: str, cycles: str) -> str:
    """引用符閉じ忘れ（N・unclosed_quote）のモード行。"""
    return f'モード: 自律 plan="{plan} cycles={cycles}'


def _wrap(line: str | None) -> str:
    """session.tmp 風の前後文脈でモード行（または不在）を包む。"""
    header = "現在地: フェーズB\n"
    footer = "## 残タスク\n- foo\n"
    if line is None:
        return header + footer
    return header + line + "\n" + footer


# ---------------------------------------------------------------------------
# 1. Edit 以外（Write 含む） → 沈黙
# ---------------------------------------------------------------------------


class TestNonEditToolIsSilent:
    def test_write_tool_is_silent_even_with_insertion_content(
        self, tmp_path: Path
    ) -> None:
        """Write は old/new の区別ができないため対象外（挿入相当の内容でも沈黙）。"""
        path = _sessions_tmp_path(tmp_path)
        result = _run_hook(_write_payload(path, _wrap(_mode_line(PLAN_A, CYCLES_1))))
        assert result.returncode == 0
        assert not result.stdout.strip()
        assert not result.stderr.strip()

    def test_bash_tool_is_silent(self, tmp_path: Path) -> None:
        path = _sessions_tmp_path(tmp_path)
        payload = {"tool_name": "Bash", "tool_input": {"command": f"echo x >> {path}"}}
        result = _run_hook(payload)
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# 2. 対象パス（sessions/ 配下の .tmp）以外 → 沈黙
# ---------------------------------------------------------------------------


class TestNonTargetPathIsSilent:
    def test_non_sessions_directory_is_silent(self, tmp_path: Path) -> None:
        path = _non_sessions_path(tmp_path)
        old = _wrap(None)
        new = _wrap(_mode_line(PLAN_A, CYCLES_1))
        result = _run_hook(_edit_payload(path, old, new))
        assert result.returncode == 0
        assert not result.stderr.strip(), (
            f"sessions/ 外なのに警告が出た: {result.stderr!r}"
        )

    def test_wrong_extension_is_silent(self, tmp_path: Path) -> None:
        path = _wrong_extension_path(tmp_path)
        old = _wrap(None)
        new = _wrap(_mode_line(PLAN_A, CYCLES_1))
        result = _run_hook(_edit_payload(path, old, new))
        assert result.returncode == 0
        assert not result.stderr.strip(), (
            f".tmp 以外なのに警告が出た: {result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# 3. fail-open
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_malformed_json_stdin_is_silent_exit_zero(self) -> None:
        result = _run_hook(None, raw_stdin="{not valid json")
        assert result.returncode == 0
        assert not result.stderr.strip()

    def test_missing_tool_input_is_silent_exit_zero(self) -> None:
        result = _run_hook({"tool_name": "Edit"})
        assert result.returncode == 0

    def test_empty_stdin_is_silent_exit_zero(self) -> None:
        result = _run_hook(None, raw_stdin="")
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# 4. 行単位判定（行中引用の非検知）
# ---------------------------------------------------------------------------


class TestLineAnchoredDetection:
    def test_mid_line_mention_is_not_detected_as_insertion(self, tmp_path: Path) -> None:
        """「モード: 自律」が行頭以外に出現しても挿入として検知されない。"""
        path = _sessions_tmp_path(tmp_path)
        old = "現在地: フェーズB\n"
        new = "現在地: フェーズB\n参考: モード: 自律 だった話をした\n"
        result = _run_hook(_edit_payload(path, old, new))
        assert result.returncode == 0
        assert not result.stderr.strip(), (
            f"行中引用が誤検知された: {result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# 5. 状態遷移表の全行
# ---------------------------------------------------------------------------


class TestStateTransitionTable:
    def test_insert_none_to_e_warns(self, tmp_path: Path) -> None:
        """行なし → E（値抽出可能な正規行）の挿入は警告。"""
        path = _sessions_tmp_path(tmp_path)
        old = _wrap(None)
        new = _wrap(_mode_line(PLAN_A, CYCLES_1))
        result = _run_hook(_edit_payload(path, old, new))
        assert result.returncode == 0
        assert "[SessionModeWatch WARN]" in result.stderr, (
            f"行なし→E の挿入で警告が出なかった: {result.stderr!r}"
        )

    def test_insert_none_to_n_warns(self, tmp_path: Path) -> None:
        """行なし → N（plan= 欠落）の挿入も警告（無効宣言も確認対象）。"""
        path = _sessions_tmp_path(tmp_path)
        old = _wrap(None)
        new = _wrap(_mode_line(None, CYCLES_1))
        result = _run_hook(_edit_payload(path, old, new))
        assert "[SessionModeWatch WARN]" in result.stderr, (
            f"行なし→N の挿入で警告が出なかった: {result.stderr!r}"
        )

    def test_delete_line_to_none_is_silent(self, tmp_path: Path) -> None:
        """行あり → 行なし（削除・HITL 復帰）は沈黙。"""
        path = _sessions_tmp_path(tmp_path)
        old = _wrap(_mode_line(PLAN_A, CYCLES_1))
        new = _wrap(None)
        result = _run_hook(_edit_payload(path, old, new))
        assert result.returncode == 0
        assert not result.stderr.strip(), (
            f"削除なのに警告が出た: {result.stderr!r}"
        )

    def test_e_to_e_same_value_cycles_update_is_silent(self, tmp_path: Path) -> None:
        """E→E 同値（cycles= のみ更新）は沈黙。"""
        path = _sessions_tmp_path(tmp_path)
        old = _wrap(_mode_line(PLAN_A, CYCLES_1))
        new = _wrap(_mode_line(PLAN_A, CYCLES_2))
        result = _run_hook(_edit_payload(path, old, new))
        assert result.returncode == 0
        assert not result.stderr.strip(), (
            f"cycles= 更新のみなのに警告が出た: {result.stderr!r}"
        )

    def test_e_to_e_different_value_warns(self, tmp_path: Path) -> None:
        """E→E 異値（plan= すり替え）は警告。"""
        path = _sessions_tmp_path(tmp_path)
        old = _wrap(_mode_line(PLAN_A, CYCLES_1))
        new = _wrap(_mode_line(PLAN_B, CYCLES_1))
        result = _run_hook(_edit_payload(path, old, new))
        assert "[SessionModeWatch WARN]" in result.stderr, (
            f"plan= 差し替えで警告が出なかった: {result.stderr!r}"
        )

    def test_n_to_e_warns(self, tmp_path: Path) -> None:
        """N→E（plan= 欠落行 → 正規行）は差し替えとして警告
        （無効宣言から有効宣言への昇格）。"""
        path = _sessions_tmp_path(tmp_path)
        old = _wrap(_mode_line(None, CYCLES_1))
        new = _wrap(_mode_line(PLAN_A, CYCLES_1))
        result = _run_hook(_edit_payload(path, old, new))
        assert "[SessionModeWatch WARN]" in result.stderr, (
            f"N→E の昇格で警告が出なかった: {result.stderr!r}"
        )

    def test_e_to_n_is_silent(self, tmp_path: Path) -> None:
        """E→N（有効→無効への降格）は沈黙（HITL 側へ安全に倒れる）。"""
        path = _sessions_tmp_path(tmp_path)
        old = _wrap(_mode_line(PLAN_A, CYCLES_1))
        new = _wrap(_mode_line(None, CYCLES_2))
        result = _run_hook(_edit_payload(path, old, new))
        assert result.returncode == 0
        assert not result.stderr.strip(), (
            f"E→N の降格なのに警告が出た: {result.stderr!r}"
        )

    def test_n_to_n_cycles_only_change_is_silent(self, tmp_path: Path) -> None:
        """N→N（両側 plan= 欠落・cycles= だけ変化）は沈黙。"""
        path = _sessions_tmp_path(tmp_path)
        old = _wrap(_mode_line(None, CYCLES_1))
        new = _wrap(_mode_line(None, CYCLES_2))
        result = _run_hook(_edit_payload(path, old, new))
        assert result.returncode == 0
        assert not result.stderr.strip(), (
            f"N→N なのに警告が出た: {result.stderr!r}"
        )

    def test_unclosed_quote_old_to_proper_new_warns(self, tmp_path: Path) -> None:
        """unclosed quote（N）→ 正規（E）は N→E の差し替え警告。"""
        path = _sessions_tmp_path(tmp_path)
        old = _wrap(_unclosed_quote_line(PLAN_A, CYCLES_1))
        new = _wrap(_mode_line(PLAN_A, CYCLES_1, quoted=True))
        result = _run_hook(_edit_payload(path, old, new))
        assert "[SessionModeWatch WARN]" in result.stderr, (
            f"unclosed quote → 正規行で警告が出なかった: {result.stderr!r}"
        )

    def test_quoted_path_with_spaces_value_diff_warns(self, tmp_path: Path) -> None:
        """引用符付き空白入りパスの値が変わる差し替えは警告。"""
        path = _sessions_tmp_path(tmp_path)
        old = _wrap(_mode_line(PLAN_SPACE_A, CYCLES_1, quoted=True))
        new = _wrap(_mode_line(PLAN_SPACE_B, CYCLES_1, quoted=True))
        result = _run_hook(_edit_payload(path, old, new))
        assert "[SessionModeWatch WARN]" in result.stderr, (
            f"引用符付きパスの差し替えで警告が出なかった: {result.stderr!r}"
        )

    def test_quoted_path_same_value_cycles_change_is_silent(
        self, tmp_path: Path
    ) -> None:
        """引用符付き同値 + cycles= 変化のみは沈黙。"""
        path = _sessions_tmp_path(tmp_path)
        old = _wrap(_mode_line(PLAN_SPACE_A, CYCLES_1, quoted=True))
        new = _wrap(_mode_line(PLAN_SPACE_A, CYCLES_2, quoted=True))
        result = _run_hook(_edit_payload(path, old, new))
        assert result.returncode == 0
        assert not result.stderr.strip(), (
            f"引用符付き同値の cycles 更新なのに警告が出た: {result.stderr!r}"
        )

    def test_multiple_spaces_and_tab_before_cycles_terminator_same_value_is_silent(
        self, tmp_path: Path
    ) -> None:
        """複数空白・TAB 前置の cycles= 終端でも値が同一なら沈黙
        （`\\s+cycles=` 終端規則・値抽出後 strip）。"""
        path = _sessions_tmp_path(tmp_path)
        old = _wrap(_mode_line_custom_terminator(PLAN_A, CYCLES_1, "   "))  # 3 spaces
        new = _wrap(_mode_line_custom_terminator(PLAN_A, CYCLES_2, "\t"))  # TAB
        result = _run_hook(_edit_payload(path, old, new))
        assert result.returncode == 0
        assert not result.stderr.strip(), (
            f"空白パターンの違いのみで誤検知した: {result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# 6. モード: HITL のみ → 沈黙
# ---------------------------------------------------------------------------


class TestHitlOnlyIsSilent:
    def test_hitl_line_insertion_is_silent(self, tmp_path: Path) -> None:
        """「モード: HITL」の挿入は `^モード: 自律` に一致しないため行なし扱いで沈黙。"""
        path = _sessions_tmp_path(tmp_path)
        old = _wrap(None)
        new = _wrap("モード: HITL")
        result = _run_hook(_edit_payload(path, old, new))
        assert result.returncode == 0
        assert not result.stderr.strip(), (
            f"モード: HITL 挿入で誤って警告が出た: {result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# 7. 2 経路の検証
# ---------------------------------------------------------------------------


class TestTwoChannelOutput:
    def test_insertion_warn_has_stdout_json_with_hook_event_name(
        self, tmp_path: Path
    ) -> None:
        path = _sessions_tmp_path(tmp_path)
        old = _wrap(None)
        new = _wrap(_mode_line(PLAN_A, CYCLES_1))
        result = _run_hook(_edit_payload(path, old, new))
        parsed = json.loads(result.stdout)
        assert "hookSpecificOutput" in parsed
        assert parsed["hookSpecificOutput"]["hookEventName"] == "PostToolUse", (
            f"hookEventName が PostToolUse でない: {parsed}"
        )

    def test_insertion_additional_context_mentions_kind_and_filename(
        self, tmp_path: Path
    ) -> None:
        path = _sessions_tmp_path(tmp_path)
        old = _wrap(None)
        new = _wrap(_mode_line(PLAN_A, CYCLES_1))
        result = _run_hook(_edit_payload(path, old, new))
        parsed = json.loads(result.stdout)
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert "挿入" in ctx, f"additionalContext に検知種別(挿入)が無い: {ctx!r}"
        assert "20260725.tmp" in ctx, (
            f"additionalContext に対象ファイル名が無い: {ctx!r}"
        )

    def test_replacement_additional_context_mentions_kind(self, tmp_path: Path) -> None:
        path = _sessions_tmp_path(tmp_path)
        old = _wrap(_mode_line(PLAN_A, CYCLES_1))
        new = _wrap(_mode_line(PLAN_B, CYCLES_1))
        result = _run_hook(_edit_payload(path, old, new))
        parsed = json.loads(result.stdout)
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert "差し替え" in ctx, f"additionalContext に検知種別(差し替え)が無い: {ctx!r}"

    def test_no_violation_produces_no_stdout(self, tmp_path: Path) -> None:
        path = _sessions_tmp_path(tmp_path)
        old = _wrap(_mode_line(PLAN_A, CYCLES_1))
        new = _wrap(_mode_line(PLAN_A, CYCLES_2))
        result = _run_hook(_edit_payload(path, old, new))
        assert not result.stdout.strip(), (
            f"沈黙のはずが stdout に出力があった: {result.stdout!r}"
        )


# ===========================================================================
# T5 追加分（E 周回 1 修正サイクル・Red）— 独立した module-level test 関数
# ===========================================================================

# C1 制御文字（CSI）。一部端末でエスケープシーケンスのプリフィクスとして解釈される。
_C1_CSI = "\x9b"


def test_c1_control_char_in_filename_is_stripped_from_both_channels(
    tmp_path: Path,
) -> None:
    """[F5・Red] 対象ファイル名の C1 制御文字が stderr / additionalContext に現れない。

    追加当時のローカル `_sanitize` は `[\\x00-\\x1f\\x7f]`（C0+DEL）のみを除去していたため
    C1 (`\\x9b` = CSI) が素通りしていた。共有 `_hook_utils.sanitize_for_terminal()`
    （C0+DEL+C1）へ置換して除去するようになっている。
    """
    path = str(
        tmp_path / ".claude" / "memory" / "sessions" / f"2026{_C1_CSI}0725.tmp"
    )
    old = _wrap(None)
    new = _wrap(_mode_line(PLAN_A, CYCLES_1))
    result = _run_hook(_edit_payload(path, old, new))
    assert result.returncode == 0
    assert "[SessionModeWatch WARN]" in result.stderr, (
        f"前提となる挿入警告自体が出ていない: stderr={result.stderr!r}"
    )
    assert _C1_CSI not in result.stderr, (
        f"C1 制御文字が stderr に残っている: {result.stderr!r}"
    )
    ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert _C1_CSI not in ctx, (
        f"C1 制御文字が additionalContext に残っている: {ctx!r}"
    )


# ===========================================================================
# T8 追加分（E 周回 2 修正サイクル・plan-report §2-C）— 独立した module-level 関数
# ===========================================================================
#
# 【Red ケース】追加時点の実装で失敗していた（機能未実装）
#   (c) F7: 対象パス判定のケース非依存化（`.lower()`）。追加当時は
#       `'sessions' not in parts or not basename.endswith('.tmp')` の
#       case-sensitive 比較だったため `Sessions/` や `.TMP` を沈黙させていた。
#       否定形の構造自体は変更しない（各成分に `.lower()` を適用するのみ）。
#   (d) F9 条件 (b)「新出値」: 追加当時は最初の `^モード: 自律` 行しか走査しなかったため、
#       2 本目の行の plan= 差し替えを検知できなかった。
#   (e) F9 条件 (c)「実効行遷移」: 実効モード行は「最初の `^モード: ` 行」
#       （HITL 行を含む）。追加当時は自律行のみ走査していたため、先行 HITL 行の削除による
#       後続自律行の実効化を検知できなかった。
#
# 【非反転固定ケース】現行実装で既に合格する（回帰ガードとして固定）
#   (f) 2 本のうち先頭の自律行を削除 → 実効行が変わるため引き続き警告。
#       （F9 を集合ベース単独で実装すると沈黙に退行するため、その退行を固定で防ぐ）
#   (g) 2 本とも cycles= のみ変更・実効行不変 → 沈黙。


PLAN_C = "~/.claude/plans/plan-c.md"


def _wrap_lines(lines: list[str]) -> str:
    """session.tmp 風の前後文脈で複数行を包む（`_wrap` の複数行版）。"""
    header = "現在地: フェーズB\n"
    footer = "## 残タスク\n- foo\n"
    return header + "".join(f"{line}\n" for line in lines) + footer


def _sessions_tmp_path_custom(
    tmp_path: Path, sessions_dir: str, basename: str
) -> str:
    """sessions ディレクトリ名 / ファイル名を任意に差し替えた対象パスを作る。"""
    return str(tmp_path / ".claude" / "memory" / sessions_dir / basename)


# ---------------------------------------------------------------------------
# (c) F7: 対象パス判定のケース非依存化 — Red
# ---------------------------------------------------------------------------


def test_uppercase_sessions_directory_insertion_warns(tmp_path: Path) -> None:
    """[T8 (c)・F7・Red] `Sessions/` ディレクトリでもモード行挿入が検知される。

    ケース非依存 FS では `Sessions` は `sessions` と同一実体の別名であり、
    追加当時の case-sensitive メンバシップ判定では検知漏れになっていた。
    """
    path = _sessions_tmp_path_custom(tmp_path, "Sessions", "20260725.tmp")
    old = _wrap(None)
    new = _wrap(_mode_line(PLAN_A, CYCLES_1))
    result = _run_hook(_edit_payload(path, old, new))
    assert result.returncode == 0
    assert "[SessionModeWatch WARN]" in result.stderr, (
        f"`Sessions/` 配下のモード行挿入が検知されなかった: {result.stderr!r}"
    )


def test_uppercase_tmp_extension_insertion_warns(tmp_path: Path) -> None:
    """[T8 (c)・F7・Red] `.TMP` 拡張子でもモード行挿入が検知される。"""
    path = _sessions_tmp_path_custom(tmp_path, "sessions", "20260725.TMP")
    old = _wrap(None)
    new = _wrap(_mode_line(PLAN_A, CYCLES_1))
    result = _run_hook(_edit_payload(path, old, new))
    assert result.returncode == 0
    assert "[SessionModeWatch WARN]" in result.stderr, (
        f"`.TMP` 拡張子のモード行挿入が検知されなかった: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# (d) F9 条件 (b): 2 本目の plan= 差し替え — Red
# ---------------------------------------------------------------------------


def test_second_mode_line_plan_replacement_warns(tmp_path: Path) -> None:
    """[T8 (d)・F9(b)・Red] モード行 2 本の 2 本目の plan= 差し替えを検知する。

    先頭行は同値のまま、2 本目の plan= だけを別プランへ差し替える。
    追加当時の実装は最初の 1 行しか走査しなかったため E→E 同値と判定して沈黙していた。
    全行走査 + 新出値判定（new の E 集合に old に無い値がある）で警告になるようにした。
    """
    path = _sessions_tmp_path(tmp_path)
    old = _wrap_lines(
        [
            _mode_line(PLAN_A, CYCLES_1),
            _mode_line(PLAN_B, CYCLES_1),
        ]
    )
    new = _wrap_lines(
        [
            _mode_line(PLAN_A, CYCLES_1),
            _mode_line(PLAN_C, CYCLES_1),
        ]
    )
    result = _run_hook(_edit_payload(path, old, new))
    assert result.returncode == 0
    assert "[SessionModeWatch WARN]" in result.stderr, (
        f"2 本目の plan= 差し替えが検知されなかった: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# (e) F9 条件 (c): 先行 HITL 行の削除による実効化 — Red
# ---------------------------------------------------------------------------


def test_deleting_preceding_hitl_line_promotes_autonomous_line_warns(
    tmp_path: Path,
) -> None:
    """[T8 (e)・F9(c)・Red] 先行 `モード: HITL` 行の削除で後続自律行が実効化する。

    実効モード行は「最初の `^モード: ` 行」（init-session/autonomous-mode SKILL.md が
    `grep -m1 '^モード: '` で読む意味論）。old の実効行は HITL（= 表の「行なし」へ写像）、
    new の実効行は自律行（E）なので「行なし → 行あり」＝挿入として警告される。
    追加当時の実装は `^モード: 自律` 行のみ走査していたため両側 E 同値と判定して
    沈黙していた。
    """
    path = _sessions_tmp_path(tmp_path)
    old = _wrap_lines(
        [
            "モード: HITL",
            _mode_line(PLAN_A, CYCLES_1),
        ]
    )
    new = _wrap_lines([_mode_line(PLAN_A, CYCLES_1)])
    result = _run_hook(_edit_payload(path, old, new))
    assert result.returncode == 0
    assert "[SessionModeWatch WARN]" in result.stderr, (
        f"先行 HITL 行の削除による自律行の実効化が検知されなかった: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# (f)(g) 非反転固定ケース
# ---------------------------------------------------------------------------


def test_deleting_first_autonomous_line_of_two_still_warns(tmp_path: Path) -> None:
    """[T8 (f)・非反転固定] 2 本のうち先頭の自律行を削除すると引き続き警告される。

    実効行が plan-a → plan-b へ変わるため差し替えとして警告する。
    new の E 集合 {plan-b} は old の E 集合 {plan-a, plan-b} の部分集合なので、
    F9 を「新出値（条件 (b)）」だけで実装すると沈黙へ退行する。
    その退行を防ぐための固定ケース（現行実装でも合格する）。
    """
    path = _sessions_tmp_path(tmp_path)
    old = _wrap_lines(
        [
            _mode_line(PLAN_A, CYCLES_1),
            _mode_line(PLAN_B, CYCLES_1),
        ]
    )
    new = _wrap_lines([_mode_line(PLAN_B, CYCLES_1)])
    result = _run_hook(_edit_payload(path, old, new))
    assert result.returncode == 0
    assert "[SessionModeWatch WARN]" in result.stderr, (
        f"先頭自律行の削除（実効行の変化）で警告が出なかった: {result.stderr!r}"
    )


def test_two_mode_lines_cycles_only_update_is_silent(tmp_path: Path) -> None:
    """[T8 (g)・非反転固定] モード行 2 本とも cycles= のみ変更なら沈黙する。

    実効行（先頭行）の plan= は不変、E 集合も不変のため 3 条件のいずれにも非該当。
    現行実装でも沈黙するため、全行走査化で誤検知が増えないことの固定。
    """
    path = _sessions_tmp_path(tmp_path)
    old = _wrap_lines(
        [
            _mode_line(PLAN_A, CYCLES_1),
            _mode_line(PLAN_B, CYCLES_1),
        ]
    )
    new = _wrap_lines(
        [
            _mode_line(PLAN_A, CYCLES_2),
            _mode_line(PLAN_B, CYCLES_2),
        ]
    )
    result = _run_hook(_edit_payload(path, old, new))
    assert result.returncode == 0
    assert not result.stderr.strip(), (
        f"cycles= のみの更新（実効行不変・集合同一）で警告が出た: {result.stderr!r}"
    )
    assert not result.stdout.strip(), (
        f"沈黙のはずが stdout に出力があった: {result.stdout!r}"
    )


# ===========================================================================
# T11 追加分（E 周回 3 修正サイクル・plan-report §2-D）— 独立した module-level 関数
# ===========================================================================
#
# 【Red ケース】追加時点の実装で失敗していた（機能未実装）
#   (i) F10: 検知種別ラベルの優先順位。architecture 改訂 8 の既定は
#       「(c) 実効行遷移が警告なら表の種別を採用 → それ以外で (a) 挿入 → (b) 差し替え」。
#       追加当時の実装は 3 条件を到達順で先勝ちさせる `if not should_warn:` フローの
#       ため、(b)（新出値判定）が (c) より先に成立するケースでは (c) の種別が
#       採用されずラベルを取り違えていた。
#   (j) F11: パス成分比較の末尾ドット・スペース除去。共有ヘルパー
#       `_hook_utils.norm_component()`（`.lower().rstrip('. ')`）を導入する前は、
#       P3 の `parts` メンバシップ比較・basename の `.tmp` 拡張子判定のいずれも
#       Windows の Win32 パス正規化（末尾ドット・スペースの暗黙除去）による
#       バイパスを閉じられていなかった。
#
# 【対の非反転】正常パスの既存挙動は変わらない
#   (k) 末尾ドット・スペースを含まない通常の `sessions/*.tmp` パスへの挿入は、
#       正規化ヘルパー導入後も引き続き検知される。


def test_boundary_condition_b_and_c_simultaneous_labels_as_insertion(
    tmp_path: Path,
) -> None:
    """[T11 (i)・F10・Red] (b)(c) 同時成立の境界ケースで検知種別は「挿入」。

    old は実効行が「モード: HITL」（表の「行なし」へ写像）で、非実効の 2 本目に
    「モード: 自律 plan=A」を持つ。new は「モード: 自律 plan=B」の 1 本のみ。
    (b) 新出値判定（old の自律行集合 {A} に無い値 B が new にある）と、
    (c) 実効行遷移判定（実効行が old=行なし → new=E）が同時に成立する境界であり、
    architecture 改訂 8 の優先順位では (c) が警告と判定する種別（挿入）を採用すべき
    ケースである。追加当時の実装は到達順で (b) が先に確定してしまうため、
    検知種別が「差し替え」と誤って報告されていた。
    """
    path = _sessions_tmp_path(tmp_path)
    old = _wrap_lines(["モード: HITL", _mode_line(PLAN_A, CYCLES_1)])
    new = _wrap_lines([_mode_line(PLAN_B, CYCLES_1)])
    result = _run_hook(_edit_payload(path, old, new))
    assert result.returncode == 0
    assert "[SessionModeWatch WARN]" in result.stderr, (
        f"境界ケースで警告自体が出なかった: {result.stderr!r}"
    )
    ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "挿入" in ctx, (
        f"検知種別が「挿入」でない（(c) 優先の順位が実装されていない）: {ctx!r}"
    )
    assert "差し替え" not in ctx, (
        f"検知種別に誤って「差し替え」が含まれている: {ctx!r}"
    )


# ---------------------------------------------------------------------------
# (j) F11: パス成分の末尾ドット・スペース除去 — Red
# ---------------------------------------------------------------------------


def test_sessions_directory_trailing_space_insertion_still_detected(
    tmp_path: Path,
) -> None:
    """[T11 (j)・F11・Red] `sessions ` （末尾スペース）ディレクトリでも検知される。

    Windows の Win32 パス正規化は末尾スペースを暗黙に除去するため、
    `sessions ` は実ファイルシステム上 `sessions` と同一実体になり得る。
    共有ヘルパー `norm_component()` 導入前は `p.lower() == 'sessions'` の厳密比較
    のため `sessions ` はメンバシップに一致せず沈黙していた。
    """
    path = _sessions_tmp_path_custom(tmp_path, "sessions ", "20260725.tmp")
    old = _wrap(None)
    new = _wrap(_mode_line(PLAN_A, CYCLES_1))
    result = _run_hook(_edit_payload(path, old, new))
    assert result.returncode == 0
    assert "[SessionModeWatch WARN]" in result.stderr, (
        f"末尾スペース付き `sessions ` 配下のモード行挿入が検知されなかった: "
        f"{result.stderr!r}"
    )


def test_tmp_basename_trailing_dot_insertion_still_detected(tmp_path: Path) -> None:
    """[T11 (j)・F11・Red] `x.TMP.` （拡張子末尾にドット）のファイル名でも検知される。

    共有ヘルパー `norm_component()` 導入前は `basename.lower().endswith('.tmp')` の
    ため、末尾に `.` が付いた `20260725.TMP.` は `.tmp` で終わらず沈黙していた。
    """
    path = _sessions_tmp_path_custom(tmp_path, "sessions", "20260725.TMP.")
    old = _wrap(None)
    new = _wrap(_mode_line(PLAN_A, CYCLES_1))
    result = _run_hook(_edit_payload(path, old, new))
    assert result.returncode == 0
    assert "[SessionModeWatch WARN]" in result.stderr, (
        f"末尾ドット付き `.TMP.` 拡張子のモード行挿入が検知されなかった: "
        f"{result.stderr!r}"
    )


def test_tmp_basename_trailing_space_insertion_still_detected(tmp_path: Path) -> None:
    """[T11 (j)・F11・Red] `x.tmp ` （拡張子末尾にスペース）のファイル名でも検知される。

    共有ヘルパー `norm_component()` 導入前は `basename.lower().endswith('.tmp')` の
    ため、末尾に半角スペースが付いた `20260725.tmp ` は `.tmp` で終わらず沈黙していた。
    """
    path = _sessions_tmp_path_custom(tmp_path, "sessions", "20260725.tmp ")
    old = _wrap(None)
    new = _wrap(_mode_line(PLAN_A, CYCLES_1))
    result = _run_hook(_edit_payload(path, old, new))
    assert result.returncode == 0
    assert "[SessionModeWatch WARN]" in result.stderr, (
        f"末尾スペース付き `.tmp ` 拡張子のモード行挿入が検知されなかった: "
        f"{result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# (k) 対の非反転: 末尾ドット・スペースを含まない通常パスは既存挙動のまま
# ---------------------------------------------------------------------------


def test_normal_sessions_path_without_trailing_chars_still_detected(
    tmp_path: Path,
) -> None:
    """[T11 (k)・非反転固定] 末尾ドット・スペースを含まない通常パスは引き続き検知される。

    正規化ヘルパー導入が既存の（末尾に余分な文字を含まない）通常パスの
    検知を後退させないことの固定ケース。追加当時の実装でも合格する。
    """
    path = _sessions_tmp_path_custom(tmp_path, "sessions", "20260725.tmp")
    old = _wrap(None)
    new = _wrap(_mode_line(PLAN_A, CYCLES_1))
    result = _run_hook(_edit_payload(path, old, new))
    assert result.returncode == 0
    assert "[SessionModeWatch WARN]" in result.stderr, (
        f"通常パスのモード行挿入が検知されなかった（正規化導入で正常系が退行した）: "
        f"{result.stderr!r}"
    )


# ===========================================================================
# T? 追加分（stdin cp932 defect リグレッション・
# architecture-report-20260726-082504.md ADR-3）— 独立した module-level ヘルパー/クラス
# ===========================================================================
#
# 【背景】v2.55.0 で追加した session_mode_watch.py は stdout/stderr のみ
# reconfigure(encoding='utf-8') しており、stdin は Windows ネイティブ既定の
# cp932 のまま payload を読む。ハーネスは UTF-8 JSON を stdin に送るため、
# payload 中の日本語（`old_string`/`new_string` 内の `モード:`）が
# cp932 環境下で mojibake（または UnicodeDecodeError → 既存の
# `except (json.JSONDecodeError, ValueError)` に吸収されて fail-open）になり、
# `^モード: 自律` 正規表現が不一致となって全編集が沈黙ですり抜ける
# （2026-07-26 実機スモークで確定）。
#
# 【既存ヘルパー `_run_hook` は変更しない】
# 既存 37 テストのベースラインを崩さないため、`_run_hook`（json.dumps は
# ensure_ascii 既定=True）はそのまま維持する。本節は独立した新規ヘルパー
# `_run_hook_bytes_stdin` を用い、stdin に生バイト列（UTF-8・
# ensure_ascii=False で非 ASCII を保持した JSON）を渡すことで、
# 実ハーネスと同じワイヤ形式を決定論的に再現する。
#
# 【Red の理由】追加当時は stdin reconfigure が無かったため、
# PYTHONIOENCODING=cp932 環境下で `モード: 自律` 行を含む payload が
# 正しくデコードできず、挿入検知 warn（stderr `[SessionModeWatch WARN]` /
# stdout JSON の additionalContext）が出ていなかった。修正前は以下の
# cp932 系テストが fail していた。


def _run_hook_bytes_stdin(
    payload: dict,
    *,
    pythonioencoding: str,
) -> subprocess.CompletedProcess:
    """stdin に生バイト列（UTF-8・非 ASCII 保持）を渡す専用ヘルパー。

    既存の `_run_hook`（json.dumps は ensure_ascii 既定=True で `\\uXXXX`
    エスケープの純 ASCII になる）とは独立させている。ensure_ascii=True の
    payload は cp932 環境でも化けずに読めてしまい、本 defect（cp932 環境で
    stdin の生の日本語バイト列がデコードミスする）を再現できないため、
    本ヘルパーは `json.dumps(payload, ensure_ascii=False)` を UTF-8
    エンコードした生バイト列を直接 stdin に渡す（実ハーネスと同じワイヤ形式）。

    `env` はホワイトリスト方式で明示的に構築しており、`PYTHONUTF8` は
    決して転記しない（`os.environ` に存在しても引き継がない）。これにより
    Windows ネイティブ既定（cp932 stdin）を PYTHONIOENCODING の値だけで
    決定論的に切り替えられる。
    """
    if not HOOK_PATH.is_file():
        raise FileNotFoundError(
            f"{HOOK_PATH} が存在しません（P3 hook 未実装のため Red）"
        )
    env: dict[str, str] = {"PYTHONIOENCODING": pythonioencoding}
    for key in ("SYSTEMROOT", "PATH"):
        if key in os.environ:
            env[key] = os.environ[key]
    stdin_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert "モード".encode("utf-8") in stdin_bytes, (
        "stdin payload に生の日本語 UTF-8 バイト列が含まれていない"
        "（ensure_ascii=False の前提が崩れている）"
    )
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=stdin_bytes,
        capture_output=True,
        text=False,
        env=env,
    )


def _cp932_regression_payload(tmp_path: Path) -> dict:
    """cp932 defect 再現用の Edit payload を生成する。

    file_path は `sessions/YYYYMMDD.tmp` 形式、old_string にモード行なし、
    new_string に「モード: 自律 plan=~/.claude/plans/x.md cycles=1」行の
    挿入を含む（architecture-report-20260726-082504.md ADR-3 準拠）。
    """
    path = _sessions_tmp_path(tmp_path)
    old_string = _wrap(None)
    new_string = _wrap("モード: 自律 plan=~/.claude/plans/x.md cycles=1")
    return _edit_payload(path, old_string, new_string)


class TestStdinCp932Regression:
    """stdin cp932 defect のリグレッションテスト（ADR-3）。

    PYTHONIOENCODING=cp932・PYTHONUTF8 除去の子プロセスで実 UTF-8 バイト列の
    payload を送り、Windows ネイティブ既定の stdin cp932 環境を決定論的に
    再現する。修正前は session_mode_watch.py が stdin reconfigure を持たず、
    cp932 環境でこのクラスのテストは「hook が沈黙して warn が出ない」ため
    fail していた。修正（stdin reconfigure 追加）後は pass する。
    """

    def test_cp932_stdin_env_still_detects_insertion_warn(
        self, tmp_path: Path
    ) -> None:
        """PYTHONIOENCODING=cp932 環境でも挿入警告が検知される（修正後 green）。

        追加当時は stdin reconfigure が無かったため、cp932 環境下で
        `モード: 自律` 行が正しくデコードできず沈黙し、本テストは fail していた
        （defect の再現＝正しい理由による Red だった）。
        """
        payload = _cp932_regression_payload(tmp_path)
        result = _run_hook_bytes_stdin(payload, pythonioencoding="cp932")
        stderr_text = result.stderr.decode("utf-8", errors="replace")
        stdout_text = result.stdout.decode("utf-8", errors="replace")
        assert result.returncode == 0, (
            f"exit code が 0 でない: returncode={result.returncode} "
            f"stderr={stderr_text!r}"
        )
        assert "[SessionModeWatch WARN]" in stderr_text, (
            "cp932 stdin 環境で挿入警告が検知されなかった"
            f"（stdin cp932 defect の再現）: stderr={stderr_text!r}"
        )
        parsed = json.loads(stdout_text)
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert "挿入" in ctx, (
            f"検知種別(挿入)が additionalContext に無い: {ctx!r}"
        )

    def test_utf8_stdin_env_detects_insertion_warn_as_control(
        self, tmp_path: Path
    ) -> None:
        """[対照] PYTHONIOENCODING=utf-8 環境では同一 payload で警告が出る。

        stdin の暗黙デコードが utf-8 になるケースであり、cp932 特有の
        defect ではないことを示す対照ケース（環境非依存の確認）。
        現行実装でも stdin は暗黙的に PYTHONIOENCODING の値でデコードされる
        ため、本ケースは現行実装でも pass する想定。
        """
        payload = _cp932_regression_payload(tmp_path)
        result = _run_hook_bytes_stdin(payload, pythonioencoding="utf-8")
        stderr_text = result.stderr.decode("utf-8", errors="replace")
        assert result.returncode == 0, (
            f"exit code が 0 でない: returncode={result.returncode} "
            f"stderr={stderr_text!r}"
        )
        assert "[SessionModeWatch WARN]" in stderr_text, (
            "utf-8 stdin 環境で挿入警告が検知されなかった: "
            f"stderr={stderr_text!r}"
        )
