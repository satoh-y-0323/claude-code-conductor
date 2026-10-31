"""Tests for .claude/hooks/design_review_count_check.py (PostToolUse・配布対象)

design-review-report（C-3 の監査レポート）のサマリ表の件数と、本文の指摘見出しの
重要度別の数が食い違っていたら警告する hook の振る舞いを測る（task test-count・Red）。

## 測る性質

関数 ``check_counts(text) -> (一致か, 見出しの数, 表の値 | None)``:
  - 見出しレベル 3 の ``[DC-<ID>]`` 直後の括弧（全角・半角）の先頭の重要度を数える。
    ID の形・重要度の太字・節の名前に依存しない。レベル 2 / 4 の見出しは数えない
  - サマリ表は ``| 重要度 | 件数… |`` をヘッダに持つ最初の表の、High/Medium/Low/合計 行の
    2 番目のセル（数値・太字可）。3 番目以降は読まない。数値でない行は無いものとして扱う
  - コードフェンス（``` / ~~~・先頭空白可）の中は数えない。閉じていないフェンスは末尾まで
  - 判定: 見出し 0 かつ表なし → 一致。見出しありかつ表なし → 食い違い。
    表あり → 4 行がそろい各値（合計は 3 つの和）が見出しの数と等しいときだけ一致

hook 本体（サブプロセス・stdin に payload）:
  - Write / Edit で `.claude/reports/` 直下の ``design-review-report-*.md`` のみ対象。
    本文はディスクから読み直す（payload の content は使わない）
  - 食い違い → exit 0・stdout に PostToolUse の additionalContext JSON（表の値と見出しの
    数の両方を含む）・stderr にも警告
  - 一致・対象外・読めない・stdin 壊れ → exit 0・stdout 空

両方向（常に沈黙するスタブ／常に警告するスタブ）のどちらでも赤になる組にしてある。

## Red の理由

hook が未作成のため、各ヘルパーが ``FileNotFoundError``（hook 未実装）を送出して失敗する。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "design_review_count_check.py"

SEVERITIES = ("High", "Medium", "Low")
TOTAL = "合計"


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------


def _env() -> dict[str, str]:
    env: dict[str, str] = {"PYTHONIOENCODING": "utf-8"}
    for key in ("SYSTEMROOT", "PATH"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _require_hook() -> None:
    if not HOOK_PATH.is_file():
        raise FileNotFoundError(
            f"{HOOK_PATH} が存在しません（hook 未実装のため Red）"
        )


_CHECK_SCRIPT = textwrap.dedent(
    """
    import importlib.util, json, sys
    spec = importlib.util.spec_from_file_location("drcc", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    text = json.loads(sys.stdin.read())["text"]
    ok, headings, table = mod.check_counts(text)
    sys.stdout.write(json.dumps({"ok": ok, "headings": headings, "table": table}))
    """
)


def check_counts(text: str) -> tuple[bool, dict, dict | None]:
    """hook モジュールの check_counts を別プロセスで呼ぶ（import 時の stdin 再設定を隔離）。"""
    _require_hook()
    proc = subprocess.run(
        [sys.executable, "-c", _CHECK_SCRIPT, str(HOOK_PATH)],
        input=json.dumps({"text": text}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(),
    )
    assert proc.returncode == 0, f"check_counts の呼び出しに失敗: {proc.stderr}"
    data = json.loads(proc.stdout)
    return data["ok"], data["headings"], data["table"]


def _run_hook(payload: dict | None, *, raw_stdin: str | None = None) -> subprocess.CompletedProcess:
    _require_hook()
    stdin_data = raw_stdin if raw_stdin is not None else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=stdin_data,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(),
    )


def _heading(sev: str, idx: int, *, id_prefix: str = "DC-GP-", paren: str = "full") -> str:
    if paren == "full":
        return f"### [{id_prefix}{idx:03d}]（{sev}・起因: C計画）"
    return f"### [{id_prefix}{idx:03d}]({sev}, 起因: C計画)"


def _table(high, medium, low, total, *, header: str = "| 重要度 | 件数 |") -> str:
    rows = [header, "|---|---|"]
    for label, value in (("High", high), ("Medium", medium), ("Low", low), (TOTAL, total)):
        if value is None:
            continue
        rows.append(f"| {label} | {value} |")
    return "\n".join(rows)


def _doc(headings: list[str], table: str | None, *, findings_title: str = "## Findings") -> str:
    parts = ["# 設計監査レポート", ""]
    if table is not None:
        parts += ["## サマリ", "", table, ""]
    parts += [findings_title, ""]
    for h in headings:
        parts += [h, "", "本文。", ""]
    return "\n".join(parts)


def _headings(high: int, medium: int, low: int) -> list[str]:
    out: list[str] = []
    n = 1
    for sev, count in (("High", high), ("Medium", medium), ("Low", low)):
        for _ in range(count):
            out.append(_heading(sev, n))
            n += 1
    return out


def _counts(high: int, medium: int, low: int) -> dict:
    return {"High": high, "Medium": medium, "Low": low}


# ---------------------------------------------------------------------------
# 1. check_counts: 判定（両方向）
# ---------------------------------------------------------------------------


class TestCheckCountsJudgement:
    def test_matching_table_and_headings(self) -> None:
        ok, headings, table = check_counts(_doc(_headings(2, 1, 0), _table(2, 1, 0, 3)))
        assert ok is True
        assert headings == _counts(2, 1, 0)
        assert table == {"High": 2, "Medium": 1, "Low": 0, TOTAL: 3}

    def test_severity_value_differs(self) -> None:
        ok, headings, table = check_counts(_doc(_headings(2, 1, 0), _table(1, 1, 0, 2)))
        assert ok is False
        assert headings == _counts(2, 1, 0)
        assert table == {"High": 1, "Medium": 1, "Low": 0, TOTAL: 2}

    def test_only_total_differs(self) -> None:
        ok, _, table = check_counts(_doc(_headings(2, 1, 0), _table(2, 1, 0, 4)))
        assert ok is False
        assert table[TOTAL] == 4

    def test_missing_total_row(self) -> None:
        """合計行の欠落は食い違い（4 行がそろうことが一致の条件）。"""
        ok, _, table = check_counts(_doc(_headings(1, 0, 0), _table(1, 0, 0, None)))
        assert ok is False
        assert TOTAL not in table

    def test_missing_severity_row(self) -> None:
        ok, _, table = check_counts(_doc(_headings(1, 1, 0), _table(1, 1, None, 2)))
        assert ok is False
        assert "Low" not in table
        assert table == {"High": 1, "Medium": 1, TOTAL: 2}

    def test_headings_without_table(self) -> None:
        ok, headings, table = check_counts(_doc(_headings(0, 2, 1), None))
        assert ok is False
        assert headings == _counts(0, 2, 1)
        assert table is None

    def test_no_headings_no_table_is_silent(self) -> None:
        ok, headings, table = check_counts(_doc([], None))
        assert ok is True
        assert headings == _counts(0, 0, 0)
        assert table is None

    def test_no_headings_with_zero_table_matches(self) -> None:
        ok, _, table = check_counts(_doc([], _table(0, 0, 0, 0)))
        assert ok is True
        assert table == {"High": 0, "Medium": 0, "Low": 0, TOTAL: 0}

    def test_no_headings_with_nonzero_table_differs(self) -> None:
        ok, headings, _ = check_counts(_doc([], _table(1, 0, 0, 1)))
        assert ok is False
        assert headings == _counts(0, 0, 0)

    def test_heading_counts_always_have_three_keys(self) -> None:
        _, headings, _ = check_counts(_doc(_headings(1, 0, 0), _table(1, 0, 0, 1)))
        assert set(headings) == {"High", "Medium", "Low"}


# ---------------------------------------------------------------------------
# 2. check_counts: 見出しの数え方
# ---------------------------------------------------------------------------


class TestHeadingCounting:
    def test_id_shape_bold_and_half_width_paren_are_counted(self) -> None:
        headings = [
            "### [DC-T8-GP-001]（High・起因: C計画）",
            "### [DC-1234]（**Medium**・起因: B設計）",
            "### [DC-AS-7](Low, 起因: A要件)",
            "### [DC-AM-002](**High**)",
        ]
        ok, counts, _ = check_counts(_doc(headings, _table(2, 1, 1, 4)))
        assert counts == _counts(2, 1, 1)
        assert ok is True

    @pytest.mark.parametrize(
        "title",
        ["## Findings", "## 2. Findings", "## Findings（今回・新規）", "## 指摘一覧"],
    )
    def test_section_name_does_not_matter(self, title: str) -> None:
        ok, counts, _ = check_counts(
            _doc(_headings(1, 1, 1), _table(1, 1, 1, 3), findings_title=title)
        )
        assert counts == _counts(1, 1, 1)
        assert ok is True

    def test_other_heading_levels_are_not_counted(self) -> None:
        headings = [
            "## [DC-GP-001]（High・起因: C計画）",
            "#### [DC-GP-002]（High・起因: C計画）",
            "### [DC-GP-003]（Low・起因: C計画）",
        ]
        ok, counts, _ = check_counts(_doc(headings, _table(0, 0, 1, 1)))
        assert counts == _counts(0, 0, 1)
        assert ok is True

    def test_severity_not_at_paren_start_is_not_counted(self) -> None:
        headings = [
            "### [DC-GP-001]（起因: High）",
            "### [DC-GP-002]（Medium・起因: C計画）",
        ]
        _, counts, _ = check_counts(_doc(headings, _table(0, 1, 0, 1)))
        assert counts == _counts(0, 1, 0)

    def test_heading_without_dc_id_is_not_counted(self) -> None:
        headings = [
            "### 補足（High）",
            "### [CR-GP-001]（High・起因: C計画）",
            "### [DC-GP-003]（High・起因: C計画）",
        ]
        _, counts, _ = check_counts(_doc(headings, _table(1, 0, 0, 1)))
        assert counts == _counts(1, 0, 0)


# ---------------------------------------------------------------------------
# 3. check_counts: サマリ表の読み方
# ---------------------------------------------------------------------------


class TestTableReading:
    def test_header_suffix_is_accepted(self) -> None:
        table = _table(1, 0, 0, 1, header="| 重要度 | 件数（新規） |")
        ok, _, read = check_counts(_doc(_headings(1, 0, 0), table))
        assert read == {"High": 1, "Medium": 0, "Low": 0, TOTAL: 1}
        assert ok is True

    def test_bold_numbers_are_accepted(self) -> None:
        table = "\n".join(
            [
                "| 重要度 | 件数 |",
                "|---|---|",
                "| High | **2** |",
                "| Medium | 0 |",
                "| Low | **1** |",
                "| 合計 | **3** |",
            ]
        )
        ok, _, read = check_counts(_doc(_headings(2, 0, 1), table))
        assert read == {"High": 2, "Medium": 0, "Low": 1, TOTAL: 3}
        assert ok is True

    def test_only_second_cell_is_read(self) -> None:
        table = "\n".join(
            [
                "| 重要度 | 件数（新規） | 件数（累計） |",
                "|---|---|---|",
                "| High | 1 | 9 |",
                "| Medium | 0 | 9 |",
                "| Low | 0 | 9 |",
                "| 合計 | 1 | 27 |",
            ]
        )
        ok, _, read = check_counts(_doc(_headings(1, 0, 0), table))
        assert read == {"High": 1, "Medium": 0, "Low": 0, TOTAL: 1}
        assert ok is True

    def test_third_cell_matching_does_not_rescue_wrong_second_cell(self) -> None:
        table = "\n".join(
            [
                "| 重要度 | 件数 | 備考 |",
                "|---|---|---|",
                "| High | 5 | 1 |",
                "| Medium | 0 | 0 |",
                "| Low | 0 | 0 |",
                "| 合計 | 5 | 1 |",
            ]
        )
        ok, _, read = check_counts(_doc(_headings(1, 0, 0), table))
        assert read["High"] == 5
        assert ok is False

    def test_non_numeric_second_cell_is_treated_as_missing(self) -> None:
        table = "\n".join(
            [
                "| 重要度 | 件数 |",
                "|---|---|",
                "| High | - |",
                "| Medium | 0 |",
                "| Low | 0 |",
                "| 合計 | 0 |",
            ]
        )
        ok, _, read = check_counts(_doc([], table))
        assert "High" not in read
        assert read == {"Medium": 0, "Low": 0, TOTAL: 0}
        assert ok is False

    def test_only_first_matching_table_is_read(self) -> None:
        text = "\n".join(
            [
                "# 設計監査レポート",
                "",
                "| 項目 | 件数 |",
                "|---|---|",
                "| High | 9 |",
                "",
                _table(1, 0, 0, 1),
                "",
                "## 前回の表",
                "",
                _table(7, 7, 7, 21),
                "",
                "## Findings",
                "",
                _heading("High", 1),
            ]
        )
        ok, _, read = check_counts(text)
        assert read == {"High": 1, "Medium": 0, "Low": 0, TOTAL: 1}
        assert ok is True


# ---------------------------------------------------------------------------
# 4. check_counts: コードフェンス
# ---------------------------------------------------------------------------


class TestCodeFences:
    @pytest.mark.parametrize("fence", ["```", "~~~", "  ```markdown"])
    def test_headings_inside_fence_are_not_counted(self, fence: str) -> None:
        close = fence.strip()[:3]
        text = "\n".join(
            [
                _doc(_headings(1, 0, 0), _table(1, 0, 0, 1)),
                "",
                fence,
                _heading("High", 50),
                _heading("Low", 51),
                close,
                "",
            ]
        )
        ok, counts, _ = check_counts(text)
        assert counts == _counts(1, 0, 0)
        assert ok is True

    def test_table_inside_fence_is_not_read(self) -> None:
        text = "\n".join(
            [
                "# 設計監査レポート",
                "",
                "```",
                _table(5, 5, 5, 15),
                "```",
                "",
                _table(0, 1, 0, 1),
                "",
                "## Findings",
                "",
                _heading("Medium", 1),
            ]
        )
        ok, _, read = check_counts(text)
        assert read == {"High": 0, "Medium": 1, "Low": 0, TOTAL: 1}
        assert ok is True

    @pytest.mark.parametrize(
        ("opening", "inner", "closing"),
        [
            ("````markdown", "```", "````"),  # 短いフェンスは長い開きを閉じない
            ("```", "```markdown", "```"),  # info string 付きの行は閉じない
            ("~~~~", "~~~", "~~~~"),
        ],
    )
    def test_nested_fence_inner_lines_are_not_counted(
        self, opening: str, inner: str, closing: str
    ) -> None:
        text = "\n".join(
            [
                _doc(_headings(1, 0, 0), _table(1, 0, 0, 1)),
                "",
                opening,
                inner,
                _heading("High", 70),
                "```" if inner.startswith("`") else "~~~",
                closing,
                "",
            ]
        )
        ok, counts, _ = check_counts(text)
        assert counts == _counts(1, 0, 0)
        assert ok is True

    def test_longer_closing_fence_closes(self) -> None:
        text = "\n".join(
            [
                _doc(_headings(0, 1, 0), _table(0, 1, 1, 2)),
                "",
                "```",
                "コード",
                "`````",
                "",
                _heading("Low", 80),
            ]
        )
        ok, counts, _ = check_counts(text)
        assert counts == _counts(0, 1, 1)
        assert ok is True

    def test_backtick_line_with_backtick_in_info_is_not_a_fence(self) -> None:
        # CommonMark: バッククォートのフェンスの info string にバッククォートは入らない
        text = "\n".join(
            [
                _doc(_headings(1, 0, 0), _table(1, 0, 0, 1)),
                "",
                "```インライン```",
                "",
                _heading("Low", 90),
            ]
        )
        ok, counts, _ = check_counts(text)
        assert counts == _counts(1, 0, 1)
        assert ok is False

    def test_unclosed_fence_runs_to_end(self) -> None:
        text = "\n".join(
            [
                _doc(_headings(0, 0, 1), _table(0, 0, 1, 1)),
                "",
                "```",
                _heading("High", 60),
                _heading("High", 61),
            ]
        )
        ok, counts, _ = check_counts(text)
        assert counts == _counts(0, 0, 1)
        assert ok is True

    def test_heading_after_closed_fence_is_counted(self) -> None:
        text = "\n".join(
            [
                "# 設計監査レポート",
                "",
                _table(1, 0, 0, 1),
                "",
                "```",
                "コード",
                "```",
                "",
                _heading("High", 1),
            ]
        )
        ok, counts, _ = check_counts(text)
        assert counts == _counts(1, 0, 0)
        assert ok is True


# ---------------------------------------------------------------------------
# 5. hook 本体（サブプロセス）
# ---------------------------------------------------------------------------


MISMATCH_TEXT = _doc(_headings(3, 0, 0), _table(7, 0, 0, 7))
MATCH_TEXT = _doc(_headings(3, 0, 0), _table(3, 0, 0, 3))


def _reports_dir(tmp_path: Path, *parts: str) -> Path:
    d = tmp_path.joinpath(*parts) if parts else tmp_path / ".claude" / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_report(directory: Path, basename: str, text: str) -> Path:
    p = directory / basename
    p.write_text(text, encoding="utf-8")
    return p


def _payload(tool_name: str, file_path: Path | str, content: str = "") -> dict:
    tool_input: dict = {"file_path": str(file_path)}
    if tool_name == "Write":
        tool_input["content"] = content
    else:
        tool_input["old_string"] = "a"
        tool_input["new_string"] = "b"
    return {"tool_name": tool_name, "tool_input": tool_input}


def _assert_silent(proc: subprocess.CompletedProcess) -> None:
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", f"沈黙すべきところで stdout が出た: {proc.stdout!r}"


def _assert_warned(proc: subprocess.CompletedProcess) -> str:
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() != "", "食い違いで stdout JSON が出ていない"
    data = json.loads(proc.stdout)
    hso = data["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    context = hso["additionalContext"]
    assert isinstance(context, str) and context
    assert proc.stderr.strip() != "", "食い違いで stderr に警告が出ていない"
    return context


NAME = "design-review-report-20261030-162815.md"


class TestHookWarnsOnMismatch:
    @pytest.mark.parametrize("tool", ["Write", "Edit"])
    def test_mismatch_warns(self, tmp_path: Path, tool: str) -> None:
        text = _doc(_headings(13, 0, 0), _table(29, 0, 0, 29))
        p = _write_report(_reports_dir(tmp_path), NAME, text)
        context = _assert_warned(_run_hook(_payload(tool, p, text)))
        # パス・ファイル名由来の数字を除いたうえで、サマリ表の値（29）と
        # 見出しの数（13）の両方を含むこと
        for noise in (str(p), p.as_posix(), str(tmp_path), tmp_path.as_posix(), p.name):
            context = context.replace(noise, "")
        assert "29" in context, context
        assert "13" in context, context

    def test_headings_without_table_warns(self, tmp_path: Path) -> None:
        text = _doc(_headings(0, 2, 0), None)
        p = _write_report(_reports_dir(tmp_path), NAME, text)
        _assert_warned(_run_hook(_payload("Write", p, text)))

    def test_reads_content_from_disk_not_payload(self, tmp_path: Path) -> None:
        """ディスクが食い違い・payload が一致 → 警告（ディスクを読み直している）。"""
        p = _write_report(_reports_dir(tmp_path), NAME, MISMATCH_TEXT)
        _assert_warned(_run_hook(_payload("Write", p, MATCH_TEXT)))

    def test_case_insensitive_directory_names(self, tmp_path: Path) -> None:
        d = _reports_dir(tmp_path, ".CLAUDE", "REPORTS")
        p = _write_report(d, NAME, MISMATCH_TEXT)
        _assert_warned(_run_hook(_payload("Edit", p)))


class TestHookSilent:
    @pytest.mark.parametrize("tool", ["Write", "Edit"])
    def test_match_is_silent(self, tmp_path: Path, tool: str) -> None:
        p = _write_report(_reports_dir(tmp_path), NAME, MATCH_TEXT)
        _assert_silent(_run_hook(_payload(tool, p, MATCH_TEXT)))

    def test_disk_match_with_mismatching_payload_is_silent(self, tmp_path: Path) -> None:
        p = _write_report(_reports_dir(tmp_path), NAME, MATCH_TEXT)
        _assert_silent(_run_hook(_payload("Write", p, MISMATCH_TEXT)))

    def test_no_headings_no_table_is_silent(self, tmp_path: Path) -> None:
        text = _doc([], None)
        p = _write_report(_reports_dir(tmp_path), NAME, text)
        _assert_silent(_run_hook(_payload("Write", p, text)))

    @pytest.mark.parametrize("tool", ["Read", "Bash", "NotebookEdit", "MultiEdit"])
    def test_other_tools_are_silent(self, tmp_path: Path, tool: str) -> None:
        p = _write_report(_reports_dir(tmp_path), NAME, MISMATCH_TEXT)
        payload = {"tool_name": tool, "tool_input": {"file_path": str(p)}}
        _assert_silent(_run_hook(payload))

    @pytest.mark.parametrize(
        "basename",
        [
            "plan-report-20261030-162815.md",
            "code-review-report-20261030-162815.md",
            "design-review-report-20261030-162815.txt",
            "my-design-review-report-20261030-162815.md",
        ],
    )
    def test_other_names_are_silent(self, tmp_path: Path, basename: str) -> None:
        p = _write_report(_reports_dir(tmp_path), basename, MISMATCH_TEXT)
        _assert_silent(_run_hook(_payload("Write", p, MISMATCH_TEXT)))

    def test_archive_subdir_is_silent(self, tmp_path: Path) -> None:
        d = _reports_dir(tmp_path, ".claude", "reports", "archive")
        p = _write_report(d, NAME, MISMATCH_TEXT)
        _assert_silent(_run_hook(_payload("Write", p, MISMATCH_TEXT)))

    def test_reports_not_under_dot_claude_is_silent(self, tmp_path: Path) -> None:
        d = _reports_dir(tmp_path, "docs", "reports")
        p = _write_report(d, NAME, MISMATCH_TEXT)
        _assert_silent(_run_hook(_payload("Write", p, MISMATCH_TEXT)))

    def test_missing_file_is_silent(self, tmp_path: Path) -> None:
        p = _reports_dir(tmp_path) / NAME
        _assert_silent(_run_hook(_payload("Write", p, MISMATCH_TEXT)))

    def test_broken_stdin_is_silent(self) -> None:
        _assert_silent(_run_hook(None, raw_stdin="{not json"))

    def test_missing_file_path_is_silent(self) -> None:
        _assert_silent(_run_hook({"tool_name": "Write", "tool_input": {}}))


# ---------------------------------------------------------------------------
# 6. 異常に長い数値セル（fail-open）  — task test-fix1
# ---------------------------------------------------------------------------
#
# サマリ表の数値セルが非常に長い（5000 桁）と、``int()`` が Python 3.11+ の整数文字列変換の
# 上限（4300 桁）で ValueError を送出する。警告の hook は異常時 fail-open（exit 0）であり、
# check_counts も例外を送出しないこと。


_HUGE = "9" * 5000

_CHECK_NO_RAISE_SCRIPT = textwrap.dedent(
    """
    import importlib.util, json, sys
    spec = importlib.util.spec_from_file_location("drcc", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    text = json.loads(sys.stdin.read())["text"]
    try:
        ok, headings, table = mod.check_counts(text)
    except BaseException as exc:  # noqa: BLE001 - 送出の有無そのものを測る
        sys.stdout.write(json.dumps({"raised": type(exc).__name__ + ": " + str(exc)[:200]}))
    else:
        # 表の値は巨大整数を含みうる（json 化で再び上限に当たる）ため、キーだけ返す
        sys.stdout.write(json.dumps({
            "raised": None,
            "ok": ok,
            "headings": headings,
            "table_keys": None if table is None else sorted(table),
        }))
    """
)


def _check_counts_no_raise(text: str) -> dict:
    _require_hook()
    proc = subprocess.run(
        [sys.executable, "-c", _CHECK_NO_RAISE_SCRIPT, str(HOOK_PATH)],
        input=json.dumps({"text": text}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(),
    )
    assert proc.returncode == 0, f"check_counts の呼び出しスクリプトが失敗: {proc.stderr}"
    return json.loads(proc.stdout)


def _huge_table(label: str) -> str:
    values = {"High": "3", "Medium": "0", "Low": "0", TOTAL: "3"}
    values[label] = _HUGE
    rows = ["| 重要度 | 件数 |", "|---|---|"]
    rows += [f"| {lab} | {values[lab]} |" for lab in ("High", "Medium", "Low", TOTAL)]
    return "\n".join(rows)


def _assert_fail_open(proc: subprocess.CompletedProcess) -> None:
    """exit 0。stdout は空か、出すなら PostToolUse の正しい JSON のみ。"""
    assert proc.returncode == 0, f"例外で落ちた（fail-open でない）: {proc.stderr[-2000:]}"
    out = proc.stdout.strip()
    if out:
        data = json.loads(out)
        assert data["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


class TestHugeNumberCell:
    @pytest.mark.parametrize("label", ["High", "Medium", "Low", TOTAL])
    def test_check_counts_does_not_raise(self, label: str) -> None:
        result = _check_counts_no_raise(_doc(_headings(3, 0, 0), _huge_table(label)))
        assert result["raised"] is None, f"check_counts が例外を送出した: {result['raised']}"
        # 5000 桁の値は見出しの数と一致しえない（読めない扱いでも食い違い）
        assert result["ok"] is False
        assert result["headings"] == _counts(3, 0, 0)

    @pytest.mark.parametrize("tool", ["Write", "Edit"])
    @pytest.mark.parametrize("label", ["High", TOTAL])
    def test_hook_is_fail_open(self, tmp_path: Path, tool: str, label: str) -> None:
        text = _doc(_headings(3, 0, 0), _huge_table(label))
        p = _write_report(_reports_dir(tmp_path), NAME, text)
        _assert_fail_open(_run_hook(_payload(tool, p, text)))


# ---------------------------------------------------------------------------
# 7. 対象の判定は正規化した basename（norm_component）で行う  — task test-fix1
# ---------------------------------------------------------------------------
#
# report_contract_check と同じく ``norm_component``（``.lower().rstrip('. ')``）で正規化した
# basename で対象かを判定する。ケース非依存 FS の別名・末尾ドット/スペースは同じ実体を指すため、
# 食い違いがあれば警告が出ること。
#
# 末尾ドット・スペースは Windows でも作れる（Win32 がパス末尾のドット/スペースを取り除いて
# 同じ実体を作成・オープンする。Python 3.11.9 / Windows 11 で実測）。POSIX では名前のまま
# 作られる。どちらでも payload のパスで開けるので除外しない。


@pytest.mark.parametrize(
    "basename",
    [
        "Design-Review-Report-20261030-162815.md",
        "DESIGN-REVIEW-REPORT-20261030-162815.md",
        NAME + ".",
        NAME + " ",
        NAME + " .",
        "Design-Review-Report-20261030-162815.md. ",
    ],
)
class TestNormalizedBasename:
    def test_mismatch_warns(self, tmp_path: Path, basename: str) -> None:
        p = _write_report(_reports_dir(tmp_path), basename, MISMATCH_TEXT)
        _assert_warned(_run_hook(_payload("Write", p, MISMATCH_TEXT)))

    def test_match_is_silent(self, tmp_path: Path, basename: str) -> None:
        p = _write_report(_reports_dir(tmp_path), basename, MATCH_TEXT)
        _assert_silent(_run_hook(_payload("Edit", p)))


# ---------------------------------------------------------------------------
# 8. 本文の読み取り上限 512 KiB（バイト）  — task test-fix1
# ---------------------------------------------------------------------------
#
# 上限を超えるファイルは判定せず沈黙する（途中で切った本文で判定して誤警告しない）。
# 上限ちょうど以下は判定する。上限は文字数ではなくバイト数（UTF-8）であり、日本語で
# 文字数 < 上限 < バイト数 になる組で区別する。
#
# ファイルは write_bytes で書く（Windows の改行変換でバイト数がずれないように）。


_READ_LIMIT_BYTES = 512 * 1024


def _sized_doc(target_bytes: int, *, table: str, headings: list[str], filler: str) -> bytes:
    """サマリ表 → 詰め物 → 見出し の順で、UTF-8 でちょうど ``target_bytes`` の本文を作る。

    見出しを末尾に置くため、先頭から上限で切って判定する実装は見出しを取りこぼす。
    """
    head = "\n".join(["# 設計監査レポート", "", "## サマリ", "", table, "", ""])
    tail_lines = ["", "## Findings", ""]
    for h in headings:
        tail_lines += [h, "", "本文。", ""]
    tail = "\n".join(tail_lines)
    rest = target_bytes - len(head.encode("utf-8")) - len(tail.encode("utf-8")) - 1  # 詰め物の改行
    unit = len(filler.encode("utf-8"))
    n, r = divmod(rest, unit)
    padding = filler * n + "a" * r + "\n"
    data = (head + padding + tail).encode("utf-8")
    assert len(data) == target_bytes
    return data


def _write_sized(tmp_path: Path, data: bytes) -> Path:
    p = _reports_dir(tmp_path) / NAME
    p.write_bytes(data)
    assert p.stat().st_size == len(data)
    return p


_MISMATCH_PARTS = {"table": _table(7, 0, 0, 7), "headings": _headings(3, 0, 0)}
_MATCH_PARTS = {"table": _table(3, 0, 0, 3), "headings": _headings(3, 0, 0)}


class TestFileReadLimit:
    def test_ascii_exactly_limit_mismatch_warns(self, tmp_path: Path) -> None:
        p = _write_sized(tmp_path, _sized_doc(_READ_LIMIT_BYTES, filler="a", **_MISMATCH_PARTS))
        _assert_warned(_run_hook(_payload("Write", p)))

    def test_ascii_exactly_limit_match_is_silent(self, tmp_path: Path) -> None:
        p = _write_sized(tmp_path, _sized_doc(_READ_LIMIT_BYTES, filler="a", **_MATCH_PARTS))
        _assert_silent(_run_hook(_payload("Write", p)))

    def test_ascii_one_byte_over_limit_mismatch_is_silent(self, tmp_path: Path) -> None:
        p = _write_sized(
            tmp_path, _sized_doc(_READ_LIMIT_BYTES + 1, filler="a", **_MISMATCH_PARTS)
        )
        _assert_silent(_run_hook(_payload("Write", p)))

    def test_over_limit_match_is_not_misjudged_by_truncation(self, tmp_path: Path) -> None:
        """一致している本文でも、上限で切ると末尾の見出しが欠けて食い違いに見える。
        上限超過は判定しないので沈黙すること（切った本文で誤警告しない）。"""
        p = _write_sized(
            tmp_path, _sized_doc(_READ_LIMIT_BYTES + 4096, filler="a", **_MATCH_PARTS)
        )
        _assert_silent(_run_hook(_payload("Edit", p)))

    def test_multibyte_bytes_exactly_limit_mismatch_warns(self, tmp_path: Path) -> None:
        data = _sized_doc(_READ_LIMIT_BYTES, filler="あ", **_MISMATCH_PARTS)
        assert len(data.decode("utf-8")) < _READ_LIMIT_BYTES // 2
        p = _write_sized(tmp_path, data)
        _assert_warned(_run_hook(_payload("Write", p)))

    def test_multibyte_chars_under_limit_but_bytes_over_is_silent(self, tmp_path: Path) -> None:
        """文字数は上限の半分未満・バイト数は上限 + 1。バイトで判定していれば沈黙する
        （文字数で判定する実装は全文を読んで警告する）。"""
        data = _sized_doc(_READ_LIMIT_BYTES + 1, filler="あ", **_MISMATCH_PARTS)
        assert len(data.decode("utf-8")) < _READ_LIMIT_BYTES // 2
        p = _write_sized(tmp_path, data)
        _assert_silent(_run_hook(_payload("Write", p)))
