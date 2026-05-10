"""
tests/skills/test_start_skill_security_audit_phase.py

TDD Red フェーズ: start/SKILL.md に security-audit 承認後の
フェーズ F / G / H が正しく追加されることを検証するテスト。

この時点では SKILL.md はまだ変更されていないため、全テストが失敗する（正しい状態）。
"""
import re
from pathlib import Path

SKILL_PATH = Path(__file__).parents[2] / ".claude" / "skills" / "start" / "SKILL.md"


def _read_skill() -> str:
    """SKILL.md を読み込む。ファイルが存在しない場合は空文字を返す。"""
    if not SKILL_PATH.exists():
        return ""
    return SKILL_PATH.read_text(encoding="utf-8")


def _extract_section(content: str, heading: str) -> str:
    """
    指定された ## 見出しから次の ## 見出しまでのテキストを切り出す。

    Parameters
    ----------
    content : str
        SKILL.md の全文
    heading : str
        切り出したいセクションの見出し文字列（例: "## フェーズ F: 修正計画"）

    Returns
    -------
    str
        見出し行を含む、次の ## 見出しが出現するまでのテキスト。
        見出しが見つからない場合は空文字を返す。
    """
    pattern = re.compile(
        r"(^" + re.escape(heading) + r".*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(content)
    if match:
        return match.group(1)
    return ""


# ---------------------------------------------------------------------------
# テスト 1: Step 3 見出しの存在確認
# ---------------------------------------------------------------------------

def test_step3_heading_exists():
    """SKILL.md に '## Step 3: security-audit 承認後フェーズ' 見出しが存在する。"""
    content = _read_skill()
    heading = "## Step 3: security-audit 承認後フェーズ"
    assert heading in content, (
        f"見出し '{heading}' が SKILL.md に見つかりません。"
        " security-audit 承認後フェーズの説明セクションを追加してください。"
    )


# ---------------------------------------------------------------------------
# テスト 2: フェーズ F / G / H 見出しが順序通りに存在する
# ---------------------------------------------------------------------------

def test_phase_fgh_headings_exist():
    """フェーズ F・G・H の見出しがこの順序で SKILL.md に存在する。"""
    content = _read_skill()

    heading_f = "## フェーズ F: 修正計画"
    heading_g = "## フェーズ G: 実装"
    heading_h = "## フェーズ H: 最終レビュー"

    for heading in (heading_f, heading_g, heading_h):
        assert heading in content, (
            f"見出し '{heading}' が SKILL.md に見つかりません。"
        )

    pos_f = content.index(heading_f)
    pos_g = content.index(heading_g)
    pos_h = content.index(heading_h)

    assert pos_f < pos_g, (
        f"フェーズ F (pos={pos_f}) はフェーズ G (pos={pos_g}) より前に存在する必要があります。"
    )
    assert pos_g < pos_h, (
        f"フェーズ G (pos={pos_g}) はフェーズ H (pos={pos_h}) より前に存在する必要があります。"
    )


# ---------------------------------------------------------------------------
# テスト 3: フェーズ F セクションのキーワード確認
# ---------------------------------------------------------------------------

def test_phase_f_keywords():
    """フェーズ F セクションに修正分類に関するキーワードが全て含まれる。"""
    content = _read_skill()
    section_f = _extract_section(content, "## フェーズ F: 修正計画")

    assert section_f, (
        "フェーズ F セクションが見つかりません。SKILL.md に '## フェーズ F: 修正計画' を追加してください。"
    )

    required_keywords = ["High", "Medium", "Low", "複雑度", "許容・除外"]
    missing = [kw for kw in required_keywords if kw not in section_f]

    assert not missing, (
        f"フェーズ F セクションに以下のキーワードが不足しています: {missing}"
        " 修正優先度（High/Medium/Low）・複雑度・許容除外の判断基準を記載してください。"
    )


# ---------------------------------------------------------------------------
# テスト 4: フェーズ G の TDD 順序確認
# ---------------------------------------------------------------------------

def test_phase_g_tdd_order():
    """フェーズ G セクションに tester と developer が含まれ、tester が先に登場する。"""
    content = _read_skill()
    section_g = _extract_section(content, "## フェーズ G: 実装")

    assert section_g, (
        "フェーズ G セクションが見つかりません。SKILL.md に '## フェーズ G: 実装' を追加してください。"
    )

    assert "tester" in section_g, (
        "フェーズ G セクションに 'tester' が含まれていません。TDD の Red フェーズを示す記述を追加してください。"
    )
    assert "developer" in section_g, (
        "フェーズ G セクションに 'developer' が含まれていません。実装担当を示す記述を追加してください。"
    )

    pos_tester = section_g.index("tester")
    pos_developer = section_g.index("developer")

    assert pos_tester < pos_developer, (
        f"TDD 順序エラー: 'tester' (pos={pos_tester}) は 'developer' (pos={pos_developer}) "
        "より前に登場する必要があります（Red → Green の順）。"
    )


# ---------------------------------------------------------------------------
# テスト 5: フェーズ G の Stuck チェック確認
# ---------------------------------------------------------------------------

def test_phase_g_stuck_check():
    """フェーズ G セクションに systematic-debugger または 'Stuck チェック' が含まれる。"""
    content = _read_skill()
    section_g = _extract_section(content, "## フェーズ G: 実装")

    assert section_g, (
        "フェーズ G セクションが見つかりません。SKILL.md に '## フェーズ G: 実装' を追加してください。"
    )

    has_debugger = "systematic-debugger" in section_g
    has_stuck = "Stuck チェック" in section_g

    assert has_debugger or has_stuck, (
        "フェーズ G セクションに 'systematic-debugger' または 'Stuck チェック' が含まれていません。"
        " 実装が行き詰まった際の対処手順を記載してください。"
    )


# ---------------------------------------------------------------------------
# テスト 6: フェーズ H のレビュアー確認
# ---------------------------------------------------------------------------

def test_phase_h_reviewers():
    """フェーズ H セクションに code-reviewer・security-reviewer・並列 が含まれる。"""
    content = _read_skill()
    section_h = _extract_section(content, "## フェーズ H: 最終レビュー")

    assert section_h, (
        "フェーズ H セクションが見つかりません。SKILL.md に '## フェーズ H: 最終レビュー' を追加してください。"
    )

    assert "code-reviewer" in section_h, (
        "フェーズ H セクションに 'code-reviewer' が含まれていません。コードレビュアーの起動を記述してください。"
    )
    assert "security-reviewer" in section_h, (
        "フェーズ H セクションに 'security-reviewer' が含まれていません。セキュリティレビュアーの起動を記述してください。"
    )
    assert "並列" in section_h, (
        "フェーズ H セクションに '並列' が含まれていません。"
        " code-reviewer と security-reviewer を並列起動することを明示してください。"
    )


# ---------------------------------------------------------------------------
# テスト 7: 既存ステップが保持されているか
# ---------------------------------------------------------------------------

def test_existing_steps_preserved():
    """既存の Step 0 / Step 0.5 / Step 1 / Step 2 見出しが全て残存している。"""
    content = _read_skill()

    required_headings = [
        "## Step 0:",
        "## Step 0.5:",
        "## Step 1:",
        "## Step 2:",
    ]
    missing = [h for h in required_headings if h not in content]

    assert not missing, (
        f"以下の既存ステップ見出しが SKILL.md から失われています: {missing}"
        " 既存の Step を削除しないようにしてください。"
    )


# ---------------------------------------------------------------------------
# テスト 8: 各フェーズに AskUserQuestion が含まれるか
# ---------------------------------------------------------------------------

def test_approval_flow_in_each_phase():
    """フェーズ F・H に AskUserQuestion が含まれ、フェーズ G には含まれない。"""
    content = _read_skill()
    section_f = _extract_section(content, "## フェーズ F: 修正計画")
    section_g = _extract_section(content, "## フェーズ G: 実装")
    section_h = _extract_section(content, "## フェーズ H: 最終レビュー")
    assert "AskUserQuestion" in section_f, "フェーズ F に AskUserQuestion がない"
    assert "AskUserQuestion" not in section_g, "フェーズ G に AskUserQuestion が残っている（除去すること）"
    assert "AskUserQuestion" in section_h, "フェーズ H に AskUserQuestion がない"


# ---------------------------------------------------------------------------
# テスト 9: Step 2 テーブルの「承認後」行に注記が含まれるか
# ---------------------------------------------------------------------------

def test_step2_table_no_ambiguous_承認後_row():
    """
    Step 2 テーブルの security-audit / 承認後 行に
    「（自動」または「Step 3」が含まれること。
    かつ、行の遷移先説明が「（自動」を含む形で自動遷移であることを明示すること。
    """
    content = _read_skill()

    # テーブル行のパターン: | security-audit | 承認後 | ... |
    pattern = re.compile(
        r"^\|[^|]*security-audit[^|]*\|[^|]*承認後[^|]*\|([^|]+)\|",
        re.MULTILINE,
    )
    match = pattern.search(content)

    assert match, (
        "Step 2 テーブルに '| security-audit | 承認後 |' の行が見つかりません。"
    )

    transition_cell = match.group(1)

    has_auto = "（自動" in transition_cell
    has_step3 = "Step 3" in transition_cell

    # 注記として「（自動」が含まれることを必須とする（Step 3 だけでは不十分）
    assert has_auto, (
        f"security-audit / 承認後 行の遷移先セル '{transition_cell.strip()}' に "
        "'（自動' が含まれていません。"
        " 遷移が自動的に行われることを注記として明示してください。"
    )
    assert has_step3, (
        f"security-audit / 承認後 行の遷移先セル '{transition_cell.strip()}' に "
        "'Step 3' が含まれていません。"
        " 遷移先として 'Step 3' を明示してください。"
    )


# ---------------------------------------------------------------------------
# テスト 10: フェーズ G の Approval Flow タイミングが明示されているか
# ---------------------------------------------------------------------------

def test_phase_g_approval_timing_explicit():
    """
    フェーズ G セクションに AskUserQuestion が含まれない（自律実行）こと、
    かつフェーズ H への遷移記述が含まれること。
    """
    content = _read_skill()
    section_g = _extract_section(content, "## フェーズ G: 実装")

    assert section_g, (
        "フェーズ G セクションが見つかりません。SKILL.md に '## フェーズ G: 実装' を追加してください。"
    )

    assert "AskUserQuestion" not in section_g, \
        "フェーズ G に AskUserQuestion が残っている（TDD は承認なしに自律実行すること）"
    assert "フェーズ H" in section_g, \
        "フェーズ G の末尾にフェーズ H への遷移記述がない"


# ---------------------------------------------------------------------------
# テスト 11: フェーズ H にコミット手順の記述があるか
# ---------------------------------------------------------------------------

def test_phase_h_commit_instructions():
    """
    フェーズ H セクション内に「コミット」という語が含まれること。
    かつ、コミット作業の担当または手順として
    「git commit」または「コミットを提案」または「コミット操作」が含まれること。
    """
    content = _read_skill()
    section_h = _extract_section(content, "## フェーズ H: 最終レビュー")

    assert section_h, (
        "フェーズ H セクションが見つかりません。SKILL.md に '## フェーズ H: 最終レビュー' を追加してください。"
    )

    assert "コミット" in section_h, (
        "フェーズ H セクションに 'コミット' が含まれていません。"
        " コミット手順または担当を記述してください。"
    )

    has_git_commit = "git commit" in section_h
    has_propose = "コミットを提案" in section_h
    has_operation = "コミット操作" in section_h

    assert has_git_commit or has_propose or has_operation, (
        "フェーズ H セクションに 'git commit'・'コミットを提案'・'コミット操作' のいずれも含まれていません。"
        " コミット作業の担当（developer が行う等）または具体的なコミット手順を記述してください。"
    )


# ---------------------------------------------------------------------------
# テスト 12: フェーズ F でパス参照の指示があるか
# ---------------------------------------------------------------------------

def test_phase_f_path_only_instruction():
    """
    フェーズ F セクション内に「パスのみ」または「Read はエージェント」のいずれかが含まれること。
    意図: レポート内容を直接プロンプトに埋め込まず、パス参照にする指示の確認。
    """
    content = _read_skill()
    section_f = _extract_section(content, "## フェーズ F: 修正計画")

    assert section_f, (
        "フェーズ F セクションが見つかりません。SKILL.md に '## フェーズ F: 修正計画' を追加してください。"
    )

    has_path_only = "パスのみ" in section_f
    has_read_agent = "Read はエージェント" in section_f

    assert has_path_only or has_read_agent, (
        "フェーズ F セクションに 'パスのみ' または 'Read はエージェント' が含まれていません。"
        " planner へのプロンプトにはレポート内容を直接埋め込まず、"
        " ファイルパスのみを渡して Read はエージェント側で行う旨を明示してください。"
    )


# ---------------------------------------------------------------------------
# テスト 13: フェーズ G で plan-report のパス参照指示があるか
# ---------------------------------------------------------------------------

def test_phase_g_plan_report_path_reference():
    """
    フェーズ G セクション内に「plan-report」と「パス」または「Read」の両方が含まれること。
    意図: developer が plan-report を内容ではなくパス参照で受け取る指示の確認。
    """
    content = _read_skill()
    section_g = _extract_section(content, "## フェーズ G: 実装")

    assert section_g, (
        "フェーズ G セクションが見つかりません。SKILL.md に '## フェーズ G: 実装' を追加してください。"
    )

    assert "plan-report" in section_g, (
        "フェーズ G セクションに 'plan-report' が含まれていません。"
        " 修正タスクの参照元として plan-report を明示してください。"
    )

    has_path = "パス" in section_g
    has_read = "Read" in section_g

    assert has_path or has_read, (
        "フェーズ G セクションに 'パス' または 'Read' が含まれていません。"
        " tester・developer へのプロンプトには plan-report の内容を直接埋め込まず、"
        " ファイルパスを渡して Read はエージェント側で行う旨を明示してください。"
    )


# ---------------------------------------------------------------------------
# テスト 14: フェーズ H に並列レビュー失敗時のフォールバック記述があるか
# ---------------------------------------------------------------------------

def test_phase_h_parallel_review_fallback():
    """
    フェーズ H セクション内に「失敗」または「欠落」または「再起動」または「再実行」の
    いずれかが含まれること。
    意図: 並列レビューの片方が失敗した場合のフォールバック記述の確認。
    """
    content = _read_skill()
    section_h = _extract_section(content, "## フェーズ H: 最終レビュー")

    assert section_h, (
        "フェーズ H セクションが見つかりません。SKILL.md に '## フェーズ H: 最終レビュー' を追加してください。"
    )

    has_failure = "失敗" in section_h
    has_missing = "欠落" in section_h
    has_restart = "再起動" in section_h
    has_retry = "再実行" in section_h

    assert has_failure or has_missing or has_restart or has_retry, (
        "フェーズ H セクションに '失敗'・'欠落'・'再起動'・'再実行' のいずれも含まれていません。"
        " 並列レビューの片方が失敗した場合のフォールバック手順を記述してください。"
    )


# ---------------------------------------------------------------------------
# テスト 15: フェーズ G に設計判断（承認なし自律完走の理由）の記述があるか
# ---------------------------------------------------------------------------

def test_phase_g_design_rationale_noted():
    """フェーズ G セクションに設計判断（承認なし自律完走の理由）が記述されている。"""
    content = SKILL_PATH.read_text(encoding="utf-8")
    section_g = _extract_section(content, "## フェーズ G:")
    has_rationale = any(kw in section_g for kw in [
        "設計判断", "承認済み",
    ])
    assert has_rationale, \
        "フェーズ G に設計判断（承認なし自律完走の理由）の記述がない"
