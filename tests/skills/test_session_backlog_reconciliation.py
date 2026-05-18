"""
tests/skills/test_session_backlog_reconciliation.py

引き継ぎバックログ照合メカニズムが SKILL.md に正しく記述されていることを検証する。

検証対象:
- init-session/SKILL.md: Step 1.5 の追加と更新ルールの種別 A/B 区別
- dev-workflow/SKILL.md: フェーズ E 共通の「引き継ぎバックログの照合」ステップ
- taxonomy.md: memory/ セクションの責務分担明示
"""
from tests.skills._skill_helpers import (
    WORKTREE_ROOT,
    extract_section,
    read_dev_workflow_skill,
    read_init_session_skill,
)

# taxonomy.md は SKILL.md ではないため Path を直接保持する。
TAXONOMY_DOC = WORKTREE_ROOT / ".claude" / "docs" / "taxonomy.md"


def _read_taxonomy() -> str:
    return TAXONOMY_DOC.read_text(encoding="utf-8") if TAXONOMY_DOC.exists() else ""


# ---------------------------------------------------------------------------
# init-session/SKILL.md の検証
# ---------------------------------------------------------------------------

def test_init_session_has_step_1_5():
    """init-session/SKILL.md に Step 1.5 セクションが存在する。"""
    content = read_init_session_skill()
    assert "## Step 1.5:" in content, (
        "init-session/SKILL.md に '## Step 1.5:' 見出しが見つかりません。"
        " 残タスクと git log を照合する Step 1.5 を追加してください。"
    )


def test_init_session_step_1_5_has_keyword_match_logic():
    """Step 1.5 に git log とキーワード照合の手順が含まれる。"""
    content = read_init_session_skill()
    section = extract_section(content, "## Step 1.5:")

    assert section, "Step 1.5 セクションが見つかりません。"

    required = ["git log", "キーワード照合", "完了している可能性のあるタスク"]
    missing = [kw for kw in required if kw not in section]
    assert not missing, (
        f"Step 1.5 に以下のキーワードが不足しています: {missing}"
        " git log との照合と検出結果の表示手順を記載してください。"
    )


def test_init_session_step_1_5_no_auto_update():
    """Step 1.5 は自動 [x] 化しない（ユーザー承認必須）旨が明記されている。"""
    content = read_init_session_skill()
    section = extract_section(content, "## Step 1.5:")

    assert "自動で `[x]` 化はしない" in section or "自動で[x]化はしない" in section, (
        "Step 1.5 で '自動で [x] 化はしない' 旨が明記されていません。"
        " 誤検知防止のため AskUserQuestion でユーザー承認を取る手順を記載してください。"
    )


def test_init_session_update_rule_distinguishes_a_and_b():
    """更新ルール部に種別 A（ワークフローフェーズ）と種別 B（引き継ぎバックログ）の区別がある。"""
    content = read_init_session_skill()
    section = extract_section(content, "## session ファイルの更新ルール")

    assert section, "更新ルールセクションが見つかりません。"

    required = [
        "ワークフローフェーズ項目",
        "引き継ぎバックログ項目",
    ]
    missing = [kw for kw in required if kw not in section]
    assert not missing, (
        f"更新ルールに種別 A/B の区別キーワードが不足しています: {missing}"
    )


# ---------------------------------------------------------------------------
# dev-workflow/SKILL.md の検証
# ---------------------------------------------------------------------------

def test_dev_workflow_has_backlog_reconciliation_section():
    """dev-workflow/SKILL.md に '## 引き継ぎバックログの照合' セクションが存在する。"""
    content = read_dev_workflow_skill()
    heading = "## 引き継ぎバックログの照合"
    assert heading in content, (
        f"dev-workflow/SKILL.md に '{heading}' セクションが見つかりません。"
        " フェーズ E 共通ステップとして追加してください。"
    )


def test_dev_workflow_backlog_section_has_required_steps():
    """共通ステップにキーワード照合・AskUserQuestion・コミット直前の指示が含まれる。"""
    content = read_dev_workflow_skill()
    section = extract_section(content, "## 引き継ぎバックログの照合")

    assert section, "共通ステップセクションが見つかりません。"

    required = [
        "キーワード照合",
        "AskUserQuestion",
        "コミット",
        "ワークフローフェーズ",
    ]
    missing = [kw for kw in required if kw not in section]
    assert not missing, (
        f"共通ステップに以下のキーワードが不足しています: {missing}"
    )


def test_dev_workflow_phase_e_references_backlog_reconciliation():
    """フェーズ E のコミット提案部から共通ステップへの参照がある。"""
    content = read_dev_workflow_skill()

    occurrences = content.count("引き継ぎバックログの照合")
    assert occurrences >= 3, (
        f"'引き継ぎバックログの照合' の参照が {occurrences} 件しかありません。"
        " フェーズ E（指摘なし時）と E（全許容完了時）の 2 箇所から共通ステップ見出しを参照してください。"
    )


# ---------------------------------------------------------------------------
# taxonomy.md の検証
# ---------------------------------------------------------------------------

def test_taxonomy_memory_section_clarifies_responsibility():
    """taxonomy.md の memory/ 説明に Hook と LLM の責務分担が明記されている。"""
    content = _read_taxonomy()
    section = extract_section(content, "### `memory/`")

    assert section, "taxonomy.md の memory/ セクションが見つかりません。"

    required = [
        "スケルトンを自動生成",
        "LLM",
        "ユーザーは原則として手動編集しない",
    ]
    missing = [kw for kw in required if kw not in section]
    assert not missing, (
        f"taxonomy.md の memory/ 説明に以下のキーワードが不足しています: {missing}"
        " Hook と LLM の責務分担を明示してください。"
    )
