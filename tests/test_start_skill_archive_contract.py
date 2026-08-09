"""Red フェーズ: `start/SKILL.md` のアーカイブ経路の静的契約テスト。

上流契約（SSOT）:
- `.claude/reports/architecture-report-20260809-154124.md`（改訂 2）§1-2 / §3-1 / §3-4 / AC-4
- `.claude/reports/plan-report-20260809-164111.md` の `test-archive` タスク（性質 13〜15）

測る性質:
- 性質 13: `.claude/reports/` を対象とする生の `mv` **および** `mkdir` が本文に 1 件も残っていない
- 性質 14: Step 0 のアーカイブ経路が `archive_reports.py` を `c3 run` で**ルート相対パス**で呼んでいる
- 性質 15: フェーズ選択の日本語ラベルと `--phase` 値の対応が **Markdown 表の行として**書かれている
- 性質 16（補足・下記の注記を参照）: 配布 `.claude/settings.json` の `permissions.allow` に
  新スクリプトのパターンが追加されている

**性質 15 の判定単位を「行」に固定する理由**: SKILL.md には既に「要件定義」「設計」「計画」
「レビュー」も `requirements-report-*.md` 等も別文脈で存在するため、「8 語がファイル全体に
含まれるか」の `in` 検査にすると**対応表が無くても緑になる**（空の緑）。ラベルと値が
**同一のテーブル行に同居していること**を要求する。

**フェーズ値の照合はトークン境界付き**で行う。素朴な部分文字列一致だと
`code-review-report-*.md` が `review` を、`plan-report-*.md` が `plan` を含むため、
対応表が無くても緑になる（`(?<![0-9A-Za-z_-])` / `(?![0-9A-Za-z_-])` でこれを排除する）。

Red の理由: `start/SKILL.md` の Step 0 が未改修（`mkdir -p ... && mv ...` のまま）で、
`archive_reports.py` の呼び出しも対応表も存在しないため。機能未実装による正しい失敗である。

**性質 16 について（計画の性質一覧に無い補足テスト）**: 設計 §1-2 と plan の `impl-archive`
prompt が同一文字列で allow パターンを凍結しており、この変更にテストが 1 件も無いと
「配布設定の変更」だけ無検査で通る。逐語が 2 箇所で確定済みのため brittle にはならないと
判断して追加した（test-report にも明記済み）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
START_SKILL_PATH = REPO_ROOT / ".claude" / "skills" / "start" / "SKILL.md"
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"

# 設計 §3-1 が定める呼び出し形（ルート相対パス）。
ARCHIVE_SCRIPT_REL = ".claude/skills/start/scripts/archive_reports.py"
ARCHIVE_ALLOW_PATTERN = f"Bash(c3 run {ARCHIVE_SCRIPT_REL}*)"

# コマンド先頭 / パイプ / `&&` / バッククォート直後の `mv` `mkdir` を拾う。
_RAW_MV_RE = re.compile(r"(?:^|[\s;&|`(])mv\s")
_RAW_MKDIR_RE = re.compile(r"(?:^|[\s;&|`(])mkdir\s")

# `c3 run <なにか>archive_reports.py` の <なにか> を捕まえる。
_C3_RUN_ARCHIVE_RE = re.compile(r"c3 run\s+(\S*archive_reports\.py)")


def read_start_skill() -> str:
    """`start/SKILL.md` の全文を返す。"""
    assert START_SKILL_PATH.is_file(), f"start/SKILL.md が見つからない: {START_SKILL_PATH}"
    return START_SKILL_PATH.read_text(encoding="utf-8")


def extract_step0(content: str) -> str:
    """`## Step 0` 見出しから次の `## ` 見出しの直前までを返す。"""
    match = re.search(r"^##\s+Step 0.*?$", content, re.MULTILINE)
    assert match is not None, "start/SKILL.md に `## Step 0` 見出しが無い"
    start = match.start()
    nxt = re.search(r"^##\s", content[match.end():], re.MULTILINE)
    end = len(content) if nxt is None else match.end() + nxt.start()
    return content[start:end]


def table_rows(content: str) -> list[str]:
    """Markdown のテーブル行（`|` で始まり `|` で終わる行）を返す。"""
    rows = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2:
            rows.append(stripped)
    return rows


def token_in(text: str, token: str) -> bool:
    """`token` が英数字・`_`・`-` に挟まれていない独立トークンとして出現するか。"""
    pattern = re.compile(
        r"(?<![0-9A-Za-z_-])" + re.escape(token) + r"(?![0-9A-Za-z_-])"
    )
    return pattern.search(text) is not None


# ---------------------------------------------------------------------------
# 性質 13: 生の mv / mkdir が残っていない
# ---------------------------------------------------------------------------


class TestNoRawShellArchiveCommands:
    """AC-4 / INV-3。経路が 1 本化されたことを静的に押さえる。

    `mkdir` まで対象に含めるのは、`mkdir -p ... && c3 run ...` の形が残ると
    後始末で `.claude/settings.local.json` の `Bash(mkdir -p .claude/reports/archive)`
    allow を消せなくなるため（archive 作成はスクリプトの責務）。
    """

    def test_no_raw_mv_targeting_reports_dir(self):
        """性質 13: `.claude/reports/` を対象とする生の `mv` が 1 行も無い。"""
        content = read_start_skill()
        offenders = [
            (index, line)
            for index, line in enumerate(content.splitlines(), start=1)
            if ".claude/reports" in line and _RAW_MV_RE.search(line)
        ]
        assert offenders == [], (
            "start/SKILL.md に `.claude/reports/` を対象とする生の `mv` が残っている:\n"
            + "\n".join(f"  L{index}: {line.strip()}" for index, line in offenders)
        )

    def test_no_raw_mkdir_targeting_reports_dir(self):
        """性質 13: `.claude/reports/` を対象とする生の `mkdir` が 1 行も無い。"""
        content = read_start_skill()
        offenders = [
            (index, line)
            for index, line in enumerate(content.splitlines(), start=1)
            if ".claude/reports" in line and _RAW_MKDIR_RE.search(line)
        ]
        assert offenders == [], (
            "start/SKILL.md に `.claude/reports/` を対象とする生の `mkdir` が残っている"
            "（archive の作成はスクリプトの責務）:\n"
            + "\n".join(f"  L{index}: {line.strip()}" for index, line in offenders)
        )


# ---------------------------------------------------------------------------
# 性質 14: Step 0 が c3 run + ルート相対パスで呼ぶ
# ---------------------------------------------------------------------------


class TestStep0CallsArchiveScript:
    """設計 §3-1 の呼び出し契約。"""

    def test_step0_invokes_archive_script_via_c3_run(self):
        """性質 14: Step 0 節の中で `c3 run <ルート相対パス>` として呼ばれている。"""
        step0 = extract_step0(read_start_skill())
        hits = _C3_RUN_ARCHIVE_RE.findall(step0)
        assert hits, (
            "Step 0 節に `c3 run ...archive_reports.py` の呼び出しが無い。\n"
            f"Step 0 節の内容:\n{step0}"
        )
        assert all(hit == ARCHIVE_SCRIPT_REL for hit in hits), (
            "Step 0 の呼び出しがルート相対パスになっていない"
            f"（期待: {ARCHIVE_SCRIPT_REL} / 実際: {sorted(set(hits))}）。\n"
            "`tests/test_refgraph*.py` が全 skill script を `md_c3_run` の target として"
            "解決できる必要がある。"
        )

    def test_every_c3_run_of_the_script_uses_the_root_relative_path(self):
        """性質 14: ファイル全体でも `c3 run` 経由の呼び出しは全てルート相対パスである。

        `${CLAUDE_SKILL_DIR}` 形や skill 相対形が 1 件でも混ざると refgraph の
        `md_c3_run` 解決が形式ごとに割れるため、全出現を同一形に固定する。
        """
        content = read_start_skill()
        hits = _C3_RUN_ARCHIVE_RE.findall(content)
        assert hits, "SKILL.md 全体に `c3 run ...archive_reports.py` が 1 件も無い"
        wrong = sorted({hit for hit in hits if hit != ARCHIVE_SCRIPT_REL})
        assert wrong == [], f"ルート相対でない呼び出しが混ざっている: {wrong}"


# ---------------------------------------------------------------------------
# 性質 15: 日本語ラベル ↔ --phase 値の対応表
# ---------------------------------------------------------------------------


class TestPhaseLabelTable:
    """設計 §3-4 / 未確定事項 3。対応は Markdown 表の**行**で書く。"""

    PAIRS = [
        ("要件定義", "requirements"),
        ("設計", "architecture"),
        ("計画", "plan"),
        ("レビュー", "review"),
    ]

    def test_each_label_and_phase_value_share_one_markdown_table_row(self):
        """性質 15: 4 組すべてが「同一のテーブル行」に同居している。

        冒頭の 5 行は検査器そのものの健全性の固定（`plan-report-*.md` /
        `code-review-report-*.md` のような既存記述が `plan` / `review` に部分一致して
        空の緑を作らないこと）。独立したテストメソッドにすると SKILL.md 未改修でも
        自明に緑になるため、本テストへ畳み込んでいる。
        """
        assert not token_in("| 計画 | `plan-report-*.md` |", "plan")
        assert not token_in("| レビュー | `code-review-report-*.md` |", "review")
        assert not token_in("| 要件定義 | `requirements-report-*.md` |", "requirements")
        assert token_in("| 計画 | `plan` |", "plan")
        assert token_in("| レビュー | `--phase review` |", "review")

        content = read_start_skill()
        rows = table_rows(content)
        assert rows, "start/SKILL.md に Markdown テーブル行が 1 行も無い"

        missing = [
            f"{label} ↔ {value}"
            for label, value in self.PAIRS
            if not any(label in row and token_in(row, value) for row in rows)
        ]
        assert missing == [], (
            "日本語ラベルと `--phase` 値の対応が Markdown 表の行として書かれていない: "
            f"{missing}\n"
            "（判定単位は行。ファイル全体への `in` 検査では対応表が無くても緑になるため）"
        )


# ---------------------------------------------------------------------------
# 性質 16（補足）: 配布 settings.json の permissions.allow
# ---------------------------------------------------------------------------


class TestSettingsAllowsArchiveScript:
    """設計 §1-2。allow が無いと利用先で `/start` のたびにプロンプトが出る。"""

    def test_permissions_allow_contains_the_archive_script_pattern(self):
        """性質 16: `permissions.allow` に設計 §1-2 の逐語パターンが含まれる。"""
        assert SETTINGS_PATH.is_file(), f"settings.json が見つからない: {SETTINGS_PATH}"
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        allow = settings.get("permissions", {}).get("allow", [])
        assert ARCHIVE_ALLOW_PATTERN in allow, (
            f"permissions.allow に {ARCHIVE_ALLOW_PATTERN!r} が無い。\n"
            f"現在の c3 run 系 allow: "
            f"{[entry for entry in allow if entry.startswith('Bash(c3 run')]}"
        )
