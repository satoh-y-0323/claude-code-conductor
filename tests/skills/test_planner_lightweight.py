"""
tests/skills/test_planner_lightweight.py

v2.13.0 で planner.md を 172 行 → ~66 行に軽量化した検証。
並列実行設計指針・自動検査ルールは skills/dev-workflow/references/plan-design-guidelines.md に外出しされている。
"""
from pathlib import Path

from tests.skills._skill_helpers import WORKTREE_ROOT, extract_section


PLANNER_AGENT = WORKTREE_ROOT / ".claude" / "agents" / "planner.md"
PLAN_DESIGN_GUIDELINES = WORKTREE_ROOT / ".claude" / "skills" / "dev-workflow" / "references" / "plan-design-guidelines.md"

RULE15_HEADING = "## レビュー指摘を反映するときの方向検算（ルール 15）"
SELFCHECK_HEADING = "## 出力直前の自己チェックリスト"
RULE15_TABLE_COLUMNS = ("finding", "推奨", "本計画の指示", "同方向")
RULE15_TARGET_TERMS = ("code-review", "security-review", "design-critic")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _has_rule15_heading(text: str) -> bool:
    """テキスト中にルール 15 の見出し行が逐語で存在するか判定する純関数。"""
    return any(line.strip() == RULE15_HEADING for line in text.splitlines())


def _rule15_section(text: str) -> str:
    """ルール 15 見出しから次の `## ` 見出しまでのセクション本文を返す純関数。"""
    return extract_section(text, RULE15_HEADING)


def _has_rule15_table_header(text: str) -> bool:
    """ルール 15 セクション内に、4 列名がすべて同一行に含まれる `|` 行があるか判定する純関数。

    セクション内のどこかに 4 語が散在するだけでは True にならない
    （同一行内包含のみを合格とする）。
    """
    section = _rule15_section(text)
    for line in section.splitlines():
        if "|" not in line:
            continue
        if all(col in line for col in RULE15_TABLE_COLUMNS):
            return True
    return False


def _has_selfcheck_direction_item(text: str) -> bool:
    """自己チェックリストセクション内に `方向検算` を含む `- [ ]` 行があるか判定する純関数。"""
    section = extract_section(text, SELFCHECK_HEADING)
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith("- [ ]") and "方向検算" in stripped:
            return True
    return False


def _has_rule15_target_terms(text: str) -> bool:
    """ルール 15 セクション内に適用対象 3 語がすべて含まれるか判定する純関数。"""
    section = _rule15_section(text)
    return all(term in section for term in RULE15_TARGET_TERMS)


def _has_no_code_fence_in_rule15_section(text: str) -> bool:
    """ルール 15 セクション内にコードフェンス（``` で始まる行）が存在しないか判定する純関数。

    [CR-NEW]（code-review-report-20260804-163909.md）への fail-safe 対応。
    `_has_rule15_table_header` はフェンスドコードブロックを区別しないため、将来
    ルール 15 セクション内に説明用コード例（``` ブロック）を追加すると、実際の表が
    無くても表ヘッダ検査が緑になりうる。フェンス除外の状態機械は実装せず、
    「セクション内にフェンスが存在しないこと」をアサートする回帰ガードに留める。
    """
    section = _rule15_section(text)
    return not any(line.lstrip().startswith(("```", "~~~")) for line in section.splitlines())


def test_planner_agent_under_80_lines():
    """planner.md は D-012 準拠で 80 行以内に収まる。"""
    content = _read(PLANNER_AGENT)
    line_count = len(content.splitlines())
    assert line_count <= 80, (
        f"planner.md は {line_count} 行（80 行上限超過）。"
        " 処理手順は skills/dev-workflow/references/plan-design-guidelines.md に外出ししてください（D-012）。"
    )


def test_planner_references_plan_design_guidelines():
    """planner.md の Workflow Before で plan-design-guidelines.md を必読として参照する。

    rules/*.md の paths フロントマター自動注入に依存しない二重防御として明示 Read を入れる。
    """
    content = _read(PLANNER_AGENT)
    assert "plan-design-guidelines.md" in content, \
        "planner.md が plan-design-guidelines.md への参照を持っていない"
    # Workflow Before セクション内で参照されていることを確認
    workflow_idx = content.find("## Workflow")
    after_idx = content.find("**After:**", workflow_idx)
    assert workflow_idx >= 0, "## Workflow セクションが見つからない"
    workflow_section = content[workflow_idx: after_idx] if after_idx > workflow_idx else content[workflow_idx:]
    assert "plan-design-guidelines.md" in workflow_section, \
        "planner.md の Workflow セクション内に plan-design-guidelines.md への参照がない"


def test_plan_design_guidelines_exists_and_has_rules():
    """skills/dev-workflow/references/plan-design-guidelines.md が存在し、必須の設計指針キーワードと R2〜R6 を含む。

    ルールの総数・上限番号は検査しない（ルール追加で docstring が陳腐化するため）。
    検査対象は required_concepts に列挙したキーワードのみ。
    """
    content = _read(PLAN_DESIGN_GUIDELINES)
    assert content, "skills/dev-workflow/references/plan-design-guidelines.md が存在しない"
    required_concepts = (
        "depends_on の付け方",
        "TDD タスクは 3-wave に分解",
        "writes フィールド",
        "自己チェックリスト",
        "R2",
        "R3",
        "R4",
        "R5",
        "R6",
    )
    missing = [kw for kw in required_concepts if kw not in content]
    assert not missing, f"plan-design-guidelines.md に以下のセクション/キーワードが不足: {missing}"


def test_planner_no_longer_contains_extracted_sections():
    """planner.md に外出し済みのセクションが残っていないこと。"""
    content = _read(PLANNER_AGENT)
    forbidden_headings = (
        "## 並列実行のための設計指針",
        "### depends_on の付け方",
        "### TDD タスクは 3-wave に分解",
        "### writes フィールドの埋め方",
        "### 出力直前の自己チェックリスト",
        "### タスクあたりの所要時間制約",
        "### YAML フロントマターの落とし穴",
        "### 直列・並列交互パターンの取り扱い",
    )
    found = [h for h in forbidden_headings if h in content]
    assert not found, (
        f"planner.md に外出し済みのセクションが残っている: {found}. "
        "skills/dev-workflow/references/plan-design-guidelines.md に移動してください。"
    )


def test_planner_preserves_persona_sections():
    """planner.md にペルソナ定義の必須セクションが残っている。"""
    content = _read(PLANNER_AGENT)
    required_headings = (
        "## Core Mandate",
        "## Key Scope",
        "## Workflow",
        "## Tools & Constraints",
        "## Related Agents",
    )
    missing = [h for h in required_headings if h not in content]
    assert not missing, f"planner.md にペルソナ必須セクションが不足: {missing}"


def test_guidelines_has_direction_check_rule():
    """plan-design-guidelines.md にルール 15（方向検算）の見出しが逐語で存在する。"""
    content = _read(PLAN_DESIGN_GUIDELINES)
    assert _has_rule15_heading(content), (
        f"plan-design-guidelines.md に見出し {RULE15_HEADING!r} が見つからない"
    )


def test_direction_check_table_header_has_four_columns():
    """ルール 15 セクション内に 4 列名を同一行に含む表ヘッダ行が存在する。"""
    content = _read(PLAN_DESIGN_GUIDELINES)
    assert _has_rule15_table_header(content), (
        "ルール 15 セクション内に finding/推奨/本計画の指示/同方向 を"
        "同一行に含む表ヘッダ行が見つからない"
    )


def test_self_check_list_has_direction_check_item():
    """出力直前の自己チェックリストに『方向検算』を含む項目が存在する。"""
    content = _read(PLAN_DESIGN_GUIDELINES)
    assert _has_selfcheck_direction_item(content), (
        "自己チェックリストに『方向検算』を含む `- [ ]` 行が見つからない"
    )


def test_direction_check_rule_lists_target_routes():
    """ルール 15 セクション内に適用対象 3 語（code-review/security-review/design-critic）が揃っている。"""
    content = _read(PLAN_DESIGN_GUIDELINES)
    assert _has_rule15_target_terms(content), (
        "ルール 15 セクション内に code-review / security-review / design-critic の"
        "いずれかが不足している"
    )


def test_direction_check_rule_has_no_code_fence():
    """ルール 15 セクション内にコードフェンスが存在しない（[CR-NEW] への fail-safe 回帰ガード）。

    `_has_rule15_table_header` は行単位の `|` 照合であり、``` コードブロックの中身を
    区別しない。将来ルール 15 セクションに説明用のコード例（``` ブロック）を追加すると、
    実際の表本体が削除されていても表ヘッダ検査（test_direction_check_table_header_has_four_columns）
    が緑のまま残る可能性がある（fail-open クラス）。

    現時点ではルール 15 セクションにコードフェンスは存在しないため、本テストは
    Red 先行ではなく最初から緑（回帰ガード）。将来このテストが Red になった場合は、
    フェンスの追加そのものを避けるか、`_has_rule15_table_header` 側にフェンス除外の
    判定を追加するかを、そのタイミングで再検討すること。
    """
    content = _read(PLAN_DESIGN_GUIDELINES)
    assert _has_no_code_fence_in_rule15_section(content), (
        "ルール 15 セクション内にコードフェンス（``` で始まる行）が見つかった。"
        "表ヘッダ検査がフェンス内の記述に誤って合格していないか確認してください。"
    )
