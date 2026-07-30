"""Characterization tests for .claude/hooks/restore_session.py

既存実装の挙動を固定するテスト群（後付け characterization test）＋新仕様テスト群。

テストクラス:
1. TestFindLatestSessionDirectoryAbsent: ディレクトリ不在 → None
2. TestFindLatestSessionEmptyDirectory: 空ディレクトリ → None
3. TestFindLatestSessionReturnsMaxFilename: 複数 .tmp ファイル → 名前昇順最大のフルパス
4. TestFindLatestSessionIgnoresNonTmpFiles: .tmp 以外のファイルは無視
5. TestExtractSectionNoMatch: 該当見出しなし → 空文字
6. TestExtractSectionEndedByNextHeading: 次の ## で終わる → 中身を strip して返す
7. TestExtractSectionEndedByComment: 次の <!-- で終わる → 中身を strip して返す
8. TestExtractSectionAtEndOfContent: 見出しが末尾 → 末尾まで strip して返す
9. TestMainNoOutput: セッションファイル無し・全セクション空 → 何も出力せず exit 0
10. TestMainOutputTodosOnly: 残タスクのみあり → ヘッダ + 残タスクが stdout に出る
11. TestMainOutputAllSections: 全セクション埋まり → ヘッダ + 3 セクション全部が stdout に出る
12. TestMainGenbaWorkflowNotice: 現在地の状態に応じたワークフロー復帰指示制御（AC-3）
13. TestMainTodoFilterExcludesCompleted: - [x] 完了行は残タスクに含まれない（AC-4）
14. TestMainApproachTailLines: アプローチ 16行以上のとき末尾 15 行のみ注入される（AC-5）
15. TestMainNoopWhenAllEmptyAndGenbaEmpty: 全セクション空 + 現在地空 → no-op（AC-2 / architecture §3.2 step4）
16. TestMainBackwardCompatNoPresentLocation: 現在地行なし（旧形式）→ クラッシュしない（後方互換 AC-2）
17. TestExtractGenba: 「現在地:」行の値を MULTILINE regex で正確に抽出する仕様を固定
18. TestTail: 末尾 n 行切り詰め・境界条件（n=0 反直感挙動）を仕様として固定
19. TestSanitizeGenba: _sanitize_genba の DEL/C1/U+2028/U+2029 除去・改行除去・`-->` 置換の確定仕様
20. TestMainSectionSanitize: 残タスク・成功・失敗 3 セクション出力の制御文字除去を仕様として固定
21. TestMainDateStrValidation: date_str の YYYYMMDD（8桁数字）形式バリデーションの確定仕様
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "restore_session.py"


def _load_module(monkeypatch: pytest.MonkeyPatch, sessions_dir: Path) -> types.ModuleType:
    """restore_session.py をモジュールとしてロードし、SESSIONS_DIR を差し替える。"""
    spec = importlib.util.spec_from_file_location("restore_session", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    # モジュール定数を差し替える
    monkeypatch.setattr(module, "SESSIONS_DIR", str(sessions_dir))
    return module


def _make_sessions_dir(tmp_path: Path) -> Path:
    """テスト用の sessions ディレクトリを tmp_path 配下に作成して返す。"""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    return sessions_dir


def _run_main_subprocess(sessions_dir: Path) -> subprocess.CompletedProcess:
    """restore_session.py を別プロセスで実行し、SESSIONS_DIR を環境変数で注入する。

    ただし restore_session.py は環境変数での SESSIONS_DIR 差し替えをサポートしていないため、
    sessions_dir のパスが .claude/hooks/../memory/sessions に対応するように
    tmp_path 配下に .claude/hooks/ 構造を作り、そこから起動する方式を使う。
    """
    # tmp 配下に .claude/hooks/ 構造を作り、restore_session.py のシンボリックリンクを置く代わりに
    # 元スクリプトをそのまま subprocess で起動するが、SESSIONS_DIR の計算が
    # スクリプトファイルの位置に依存するため、sessions_dir の親から逆算して
    # tmp 配下に .claude/memory/sessions を作る構造にする。
    # 実際のスクリプトを起動し、sessions_dir を stdin 経由では渡せないので、
    # スクリプトを tmp にコピーして適切な位置で起動する。
    claude_dir = sessions_dir.parent.parent  # tmp/.claude
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # スクリプト本体を tmp のフック位置にコピーする
    script_src = HOOK_PATH.read_text(encoding="utf-8")
    tmp_script = hooks_dir / "restore_session.py"
    tmp_script.write_text(script_src, encoding="utf-8")

    # session_utils.py も同じディレクトリにコピーする
    # （restore_session.py が extract_section を動的ロードするため必須）
    session_utils_src = HOOK_PATH.parent / "session_utils.py"
    if session_utils_src.is_file():
        (hooks_dir / "session_utils.py").write_text(
            session_utils_src.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    return subprocess.run(
        [sys.executable, str(tmp_script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _setup_tmp_structure(tmp_path: Path) -> tuple[Path, Path]:
    """tmp_path 配下に .claude/memory/sessions を作成し、(claude_dir, sessions_dir) を返す。"""
    sessions_dir = tmp_path / ".claude" / "memory" / "sessions"
    sessions_dir.mkdir(parents=True)
    return tmp_path / ".claude", sessions_dir


# ---------------------------------------------------------------------------
# 1 & 2. find_latest_session: ディレクトリ不在 / 空ディレクトリ
# ---------------------------------------------------------------------------


class TestFindLatestSessionDirectoryAbsent:
    """find_latest_session: ディレクトリが存在しない場合は None を返す。"""

    def test_returns_none_when_directory_does_not_exist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """存在しないパスを SESSIONS_DIR に設定すると None が返る。"""
        nonexistent = tmp_path / "nonexistent" / "sessions"
        module = _load_module(monkeypatch, nonexistent)
        result = module.find_latest_session()
        assert result is None, f"期待 None、実際 {result!r}"


class TestFindLatestSessionEmptyDirectory:
    """find_latest_session: 空ディレクトリなら None を返す。"""

    def test_returns_none_when_directory_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ファイルが 1 つもない sessions ディレクトリなら None が返る。"""
        sessions_dir = _make_sessions_dir(tmp_path)
        module = _load_module(monkeypatch, sessions_dir)
        result = module.find_latest_session()
        assert result is None, f"期待 None、実際 {result!r}"


# ---------------------------------------------------------------------------
# 3. find_latest_session: 複数ファイル → 名前昇順最大のフルパス
# ---------------------------------------------------------------------------


class TestFindLatestSessionReturnsMaxFilename:
    """find_latest_session: 複数 .tmp ファイルのうち名前昇順で最大のフルパスを返す。"""

    def test_returns_lexicographically_largest_tmp_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """古いファイルと新しいファイルが混在する場合、新しい方（昇順最大）を返す。"""
        sessions_dir = _make_sessions_dir(tmp_path)
        older = sessions_dir / "20260101.tmp"
        newer = sessions_dir / "20260507.tmp"
        older.write_text("old", encoding="utf-8")
        newer.write_text("new", encoding="utf-8")

        module = _load_module(monkeypatch, sessions_dir)
        result = module.find_latest_session()

        assert result == str(newer), (
            f"期待 {str(newer)!r}、実際 {result!r}"
        )


# ---------------------------------------------------------------------------
# 4. find_latest_session: .tmp 以外は無視
# ---------------------------------------------------------------------------


class TestFindLatestSessionIgnoresNonTmpFiles:
    """.tmp 以外のファイルは find_latest_session の対象外。"""

    def test_ignores_non_tmp_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """.md / .json などのファイルは無視され、.tmp が無ければ None を返す。"""
        sessions_dir = _make_sessions_dir(tmp_path)
        (sessions_dir / "20260507.md").write_text("markdown", encoding="utf-8")
        (sessions_dir / "20260507.json").write_text("{}", encoding="utf-8")

        module = _load_module(monkeypatch, sessions_dir)
        result = module.find_latest_session()
        assert result is None, f"期待 None（.tmp ファイル無し）、実際 {result!r}"

    def test_returns_tmp_when_mixed_with_other_extensions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """.tmp 以外と混在していても .tmp ファイルを正しく返す。"""
        sessions_dir = _make_sessions_dir(tmp_path)
        tmp_file = sessions_dir / "20260507.tmp"
        tmp_file.write_text("session", encoding="utf-8")
        (sessions_dir / "20260508.md").write_text("ignored", encoding="utf-8")

        module = _load_module(monkeypatch, sessions_dir)
        result = module.find_latest_session()
        assert result == str(tmp_file), (
            f"期待 {str(tmp_file)!r}（.tmp のみ対象）、実際 {result!r}"
        )


# ---------------------------------------------------------------------------
# 5. extract_section: 該当見出しなし → 空文字
# ---------------------------------------------------------------------------


class TestExtractSectionNoMatch:
    """extract_section: 該当する見出しが無ければ空文字を返す。"""

    def test_returns_empty_string_when_heading_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """存在しない見出しを指定すると空文字が返る。"""
        module = _load_module(monkeypatch, tmp_path)
        content = "## 別のセクション\nsome content\n"
        result = module.extract_section(content, "残タスク")
        assert result == "", f"期待 ''、実際 {result!r}"


# ---------------------------------------------------------------------------
# 6. extract_section: 次の ## で終わる
# ---------------------------------------------------------------------------


class TestExtractSectionEndedByNextHeading:
    """extract_section: 次の ## 見出しで本文が区切られる。"""

    def test_extracts_content_up_to_next_heading(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """## 残タスク の内容が次の ## で終わり、strip された値が返る。"""
        module = _load_module(monkeypatch, tmp_path)
        content = (
            "## 残タスク\n"
            "  タスク1\n"
            "  タスク2\n"
            "\n## うまくいったアプローチ\n"
            "アプローチ内容\n"
        )
        result = module.extract_section(content, "残タスク")
        assert result == "タスク1\n  タスク2", (
            f"期待 'タスク1\\n  タスク2'（strip済み）、実際 {result!r}"
        )


# ---------------------------------------------------------------------------
# 7. extract_section: 次の <!-- で終わる
# ---------------------------------------------------------------------------


class TestExtractSectionEndedByComment:
    """extract_section: 次の <!-- で本文が区切られる。"""

    def test_extracts_content_up_to_html_comment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """## 残タスク の内容が <!-- で終わり、strip された値が返る。"""
        module = _load_module(monkeypatch, tmp_path)
        content = (
            "## 残タスク\n"
            "タスクA\n"
            "\n<!-- C3:SESSION:JSON\n"
            "{}\n"
            "-->\n"
        )
        result = module.extract_section(content, "残タスク")
        assert result == "タスクA", (
            f"期待 'タスクA'（strip済み）、実際 {result!r}"
        )


# ---------------------------------------------------------------------------
# 8. extract_section: 見出しが末尾 → 末尾まで返す
# ---------------------------------------------------------------------------


class TestExtractSectionAtEndOfContent:
    """extract_section: 見出しがコンテンツ末尾にある場合、末尾まで strip して返す。"""

    def test_extracts_content_to_end_of_string(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """## 残タスク が最後のセクションで次の区切りが無い場合、末尾まで返す。"""
        module = _load_module(monkeypatch, tmp_path)
        content = (
            "## うまくいったアプローチ\n"
            "アプローチ内容\n"
            "\n## 残タスク\n"
            "  最後のタスク\n"
        )
        result = module.extract_section(content, "残タスク")
        assert result == "最後のタスク", (
            f"期待 '最後のタスク'（strip済み）、実際 {result!r}"
        )


# ---------------------------------------------------------------------------
# 9 & 10. main 経由 (subprocess): セッションファイル無し / 全セクション空
# ---------------------------------------------------------------------------


class TestMainNoOutput:
    """main: セッションファイルなし / 全セクション空のとき何も出力せず exit 0。"""

    def test_no_output_when_no_session_file(self, tmp_path: Path) -> None:
        """sessions ディレクトリが空のとき stdout は空で exit code = 0。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        # sessions_dir は空のまま

        result = _run_main_subprocess(sessions_dir)

        assert result.returncode == 0, f"期待 exit 0、実際 {result.returncode}"
        assert result.stdout == "", (
            f"期待 空の stdout、実際 {result.stdout!r}"
        )

    def test_no_output_when_all_sections_empty(self, tmp_path: Path) -> None:
        """セッションファイルが存在するが全セクションが空のとき stdout は空で exit code = 0。

        後方互換テスト（現在地行なしの旧フォーマット）: 本テストは「現在地:」行がない
        旧フォーマットで全セクションが空の場合を検証する。新テンプレート形式（現在地行あり）
        での no-op は test_no_output_new_template_format_when_all_sections_empty で検証する。
        """
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        session_file = sessions_dir / "20260507.tmp"
        session_file.write_text(
            "## 残タスク\n\n## うまくいったアプローチ\n\n## 試みたが失敗したアプローチ\n",
            encoding="utf-8",
        )

        result = _run_main_subprocess(sessions_dir)

        assert result.returncode == 0, f"期待 exit 0、実際 {result.returncode}"
        assert result.stdout.strip() == "", (
            f"期待 空の stdout、実際 {result.stdout!r}"
        )

    def test_no_output_new_template_format_when_all_sections_empty(
        self, tmp_path: Path
    ) -> None:
        """新テンプレート形式（SESSION:/AGENT:/DURATION:/現在地: 行あり）で全セクション空のとき no-op。

        H-01: 新テンプレート形式で「現在地:」行が存在するが値が空であり、
        かつ全セクション（残タスク・うまくいったアプローチ・失敗したアプローチ）も
        空のとき、stdout が空で exit 0（no-op）になること。

        これは最も発生頻度が高い「セッション開始直後・何も記録していない状態」のシナリオ。
        genba='', pending_todos=[], successes='', failures='' の early-exit 条件（architecture §3.2 step4）。
        """
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        session_file = sessions_dir / "20260614.tmp"
        session_file.write_text(
            "SESSION: 20260614\n"
            "AGENT: \n"
            "DURATION: \n"
            "現在地: \n"
            "\n"
            "## うまくいったアプローチ\n"
            "\n"
            "## 試みたが失敗したアプローチ\n"
            "\n"
            "## 残タスク\n",
            encoding="utf-8",
        )

        result = _run_main_subprocess(sessions_dir)

        assert result.returncode == 0, (
            f"新テンプレート形式・全セクション空のとき exit 0 が期待されるが exit "
            f"{result.returncode} が返った。"
        )
        assert result.stdout.strip() == "", (
            "新テンプレート形式・全セクション空のとき stdout は空（no-op）であるべき。"
            f"stdout: {result.stdout!r}"
        )


# ---------------------------------------------------------------------------
# 11. main 経由: 残タスクのみあり → ヘッダ + 残タスクが stdout に出る
# ---------------------------------------------------------------------------


class TestMainOutputTodosOnly:
    """main: 残タスクのみ埋まっているとき、ヘッダと残タスクが stdout に出る。"""

    def test_outputs_header_and_todos_only(self, tmp_path: Path) -> None:
        """残タスクセクションのみ存在するとき、ヘッダと残タスクが出力される。

        注: 新仕様（AC-4）では - [ ] 形式の行のみ注入するため、
        テストデータは - [ ] 形式に更新済み（旧散文テキストから移行）。
        """
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        date_str = "20260507"
        session_file = sessions_dir / f"{date_str}.tmp"
        session_file.write_text(
            "## 残タスク\n- [ ] タスクA\n- [ ] タスクB\n\n## うまくいったアプローチ\n\n## 試みたが失敗したアプローチ\n",
            encoding="utf-8",
        )

        result = _run_main_subprocess(sessions_dir)

        assert result.returncode == 0, f"期待 exit 0、実際 {result.returncode}"
        output = result.stdout

        # ヘッダが含まれること
        expected_header = f"[C3 セッション復元: {date_str} / 圧縮後リマインダー]"
        assert expected_header in output, (
            f"ヘッダ {expected_header!r} が stdout に含まれない。stdout: {output!r}"
        )

        # 残タスクセクション見出しと内容が含まれること
        assert "## 残タスク" in output, f"## 残タスク が stdout に含まれない。stdout: {output!r}"
        assert "- [ ] タスクA" in output, f"残タスク内容 '- [ ] タスクA' が stdout に含まれない。stdout: {output!r}"

        # 空のセクションは出力されないこと
        assert "## うまくいったアプローチ" not in output, (
            "空の ## うまくいったアプローチ が出力されている（空セクションは出力しない仕様）。"
        )
        assert "## 試みたが失敗したアプローチ" not in output, (
            "空の ## 試みたが失敗したアプローチ が出力されている（空セクションは出力しない仕様）。"
        )


# ---------------------------------------------------------------------------
# 12. main 経由: 全セクション埋まり → ヘッダ + 3 セクション全部が stdout に出る
# ---------------------------------------------------------------------------


class TestMainOutputAllSections:
    """main: 全セクションが埋まっているとき、ヘッダ + 3 セクション全部が stdout に出る。"""

    def test_outputs_header_and_all_three_sections(self, tmp_path: Path) -> None:
        """全 3 セクションが埋まっているとき、ヘッダと 3 セクション全部が出力される。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        date_str = "20260507"
        session_file = sessions_dir / f"{date_str}.tmp"
        session_file.write_text(
            "## 残タスク\n- [ ] タスクX\n\n## うまくいったアプローチ\n成功例Y\n\n## 試みたが失敗したアプローチ\n失敗例Z\n",
            encoding="utf-8",
        )

        result = _run_main_subprocess(sessions_dir)

        assert result.returncode == 0, f"期待 exit 0、実際 {result.returncode}"
        output = result.stdout

        # ヘッダが含まれること
        expected_header = f"[C3 セッション復元: {date_str} / 圧縮後リマインダー]"
        assert expected_header in output, (
            f"ヘッダ {expected_header!r} が stdout に含まれない。stdout: {output!r}"
        )

        # 3 セクション全ての見出しと内容が含まれること
        assert "## 残タスク" in output, f"## 残タスク が stdout に含まれない。stdout: {output!r}"
        # H-02: - [ ] タスクX の完全一致（部分一致 "タスクX" ではなくプレフィックス込みで検証）
        assert "- [ ] タスクX" in output, (
            f"'- [ ] タスクX' が stdout に含まれない。stdout: {output!r}"
        )
        # H-02: - [x] 完了行が出力に含まれないことを明示アサート
        assert "- [x]" not in output, (
            "完了行 '- [x]' が stdout に含まれている。未完了行のみ出力されるべき（AC-4）。"
            f"stdout: {output!r}"
        )

        assert "## うまくいったアプローチ" in output, (
            f"## うまくいったアプローチ が stdout に含まれない。stdout: {output!r}"
        )
        assert "成功例Y" in output, f"'成功例Y' が stdout に含まれない。stdout: {output!r}"

        assert "## 試みたが失敗したアプローチ" in output, (
            f"## 試みたが失敗したアプローチ が stdout に含まれない。stdout: {output!r}"
        )
        assert "失敗例Z" in output, f"'失敗例Z' が stdout に含まれない。stdout: {output!r}"


# ---------------------------------------------------------------------------
# 13-15. main 経由: 現在地フィールドによるワークフロー復帰指示制御（AC-3）
# ---------------------------------------------------------------------------


class TestMainGenbaWorkflowNotice:
    """main: 現在地フィールドの値に応じてワークフロー復帰指示の有無が変わる（AC-3）。

    architecture §3.3 で定義する復帰指示は「dev-workflow 進行中」「skill 経由で再開」
    「Approval Flow」等のキーワードを含み、出力の冒頭（ヘッダより前）に配置される。
    """

    def _make_session(
        self, sessions_dir: Path, genba: str, todos: str = ""
    ) -> None:
        """テスト用セッションファイルを作成する。"""
        content = (
            f"SESSION: 20260614\n"
            f"AGENT: \n"
            f"DURATION: \n"
            f"現在地: {genba}\n"
            f"\n"
            f"## うまくいったアプローチ\n"
            f"\n"
            f"## 試みたが失敗したアプローチ\n"
            f"\n"
            f"## 残タスク\n"
            f"{todos}\n"
        )
        (sessions_dir / "20260614.tmp").write_text(content, encoding="utf-8")

    def test_outputs_workflow_notice_when_genba_is_in_progress(
        self, tmp_path: Path
    ) -> None:
        """現在地が非空かつ「完了」でないとき、出力冒頭にワークフロー復帰指示が含まれる。

        復帰指示は「dev-workflow 進行中」「skill 経由で再開」「Approval Flow」等の
        キーワードを含む（architecture §3.3）。
        """
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        self._make_session(sessions_dir, genba="フェーズD 実装中", todos="- [ ] タスクA")

        result = _run_main_subprocess(sessions_dir)

        assert result.returncode == 0, f"期待 exit 0、実際 {result.returncode}"
        output = result.stdout

        # 復帰指示のキーワードが出力に含まれること
        assert "dev-workflow" in output, (
            "現在地が進行中のとき「dev-workflow」を含む復帰指示が出力されるべき。"
            f"stdout: {output!r}"
        )
        assert "skill" in output or "Approval Flow" in output or "再開" in output, (
            "現在地が進行中のとき skill 経由再開または Approval Flow の指示が出力されるべき。"
            f"stdout: {output!r}"
        )

        # 復帰指示は冒頭（ヘッダより前）に配置されること
        header = "[C3 セッション復元:"
        notice_pos = output.find("dev-workflow")
        header_pos = output.find(header)
        assert notice_pos < header_pos, (
            f"復帰指示（位置 {notice_pos}）はヘッダ「{header}」（位置 {header_pos}）より"
            f"前に配置されるべき。stdout: {output!r}"
        )

    def test_no_workflow_notice_when_genba_is_done(self, tmp_path: Path) -> None:
        """現在地が「完了」のとき、復帰指示が出力されない。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        self._make_session(sessions_dir, genba="完了", todos="- [ ] タスクA")

        result = _run_main_subprocess(sessions_dir)

        assert result.returncode == 0, f"期待 exit 0、実際 {result.returncode}"
        output = result.stdout
        # 「dev-workflow 進行中」のような強い復帰指示が出ないこと
        assert "dev-workflow 進行中" not in output, (
            "現在地が「完了」のとき復帰指示は出力されるべきでない。"
            f"stdout: {output!r}"
        )

    def test_no_workflow_notice_when_genba_is_empty(self, tmp_path: Path) -> None:
        """現在地が空のとき、復帰指示が出力されない。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        self._make_session(sessions_dir, genba="", todos="- [ ] タスクA")

        result = _run_main_subprocess(sessions_dir)

        assert result.returncode == 0, f"期待 exit 0、実際 {result.returncode}"
        output = result.stdout
        assert "dev-workflow 進行中" not in output, (
            "現在地が空のとき復帰指示は出力されるべきでない。"
            f"stdout: {output!r}"
        )


# ---------------------------------------------------------------------------
# 16. main 経由: - [x] 完了行は残タスクに含まれない（AC-4）
# ---------------------------------------------------------------------------


class TestMainTodoFilterExcludesCompleted:
    """main: 残タスクセクションの注入は - [ ] 未完了行のみで、- [x] 完了行を含まない（AC-4）。

    architecture §3.4 で定義する「- [ ] で始まる行のみ」フィルタに従う。
    """

    def test_completed_tasks_excluded_from_output(self, tmp_path: Path) -> None:
        """- [ ] と - [x] が混在する残タスクから、- [x] 行が出力に含まれない。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        session_file = sessions_dir / "20260614.tmp"
        session_file.write_text(
            "SESSION: 20260614\n"
            "AGENT: \n"
            "DURATION: \n"
            "現在地: \n"
            "\n"
            "## うまくいったアプローチ\n"
            "\n"
            "## 試みたが失敗したアプローチ\n"
            "\n"
            "## 残タスク\n"
            "- [ ] 未完了タスクA\n"
            "- [x] 完了済みタスクB\n"
            "- [ ] 未完了タスクC\n"
            "- [x] 完了済みタスクD\n",
            encoding="utf-8",
        )

        result = _run_main_subprocess(sessions_dir)

        assert result.returncode == 0, f"期待 exit 0、実際 {result.returncode}"
        output = result.stdout

        # 未完了行は含まれること
        assert "未完了タスクA" in output, (
            "- [ ] 行（未完了タスクA）が出力に含まれるべき。stdout: {output!r}"
        )
        assert "未完了タスクC" in output, (
            "- [ ] 行（未完了タスクC）が出力に含まれるべき。stdout: {output!r}"
        )

        # 完了行は含まれないこと（AC-4）
        assert "完了済みタスクB" not in output, (
            "- [x] 行（完了済みタスクB）は出力に含まれるべきでない（AC-4）。"
            f"stdout: {output!r}"
        )
        assert "完了済みタスクD" not in output, (
            "- [x] 行（完了済みタスクD）は出力に含まれるべきでない（AC-4）。"
            f"stdout: {output!r}"
        )

    def test_section_omitted_when_no_pending_todos(self, tmp_path: Path) -> None:
        """- [ ] 行がゼロ件のとき ## 残タスク セクション自体を出力しない。

        architecture §3.2 step5③: pending_todos が空なら残タスクセクションを出力しない。
        """
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        session_file = sessions_dir / "20260614.tmp"
        session_file.write_text(
            "SESSION: 20260614\n"
            "AGENT: \n"
            "DURATION: \n"
            "現在地: \n"
            "\n"
            "## うまくいったアプローチ\n"
            "成功アプローチXY\n"
            "\n"
            "## 試みたが失敗したアプローチ\n"
            "\n"
            "## 残タスク\n"
            "- [x] 完了済みのみタスクA\n"
            "- [x] 完了済みのみタスクB\n",
            encoding="utf-8",
        )

        result = _run_main_subprocess(sessions_dir)

        assert result.returncode == 0, f"期待 exit 0、実際 {result.returncode}"
        output = result.stdout

        # 残タスクセクション見出しが出力されないこと
        assert "## 残タスク" not in output, (
            "- [ ] 行がゼロのとき ## 残タスク セクションは出力されるべきでない。"
            f"stdout: {output!r}"
        )


# ---------------------------------------------------------------------------
# 17. main 経由: アプローチ末尾 N 行上限（AC-5）
# ---------------------------------------------------------------------------


class TestMainApproachTailLines:
    """main: うまくいったアプローチ / 試みたが失敗したアプローチは末尾 APPROACH_TAIL_LINES=15 行に上限化（AC-5）。

    architecture §3.5 で定義する _tail(text, n) ヘルパを用い、16行以上のときは末尾15行のみ注入。
    15行以下ならそのまま全行を注入する。
    """

    def _make_long_approach(self, n: int) -> str:
        """n 行のアプローチテキストを生成する（行1〜行Nで識別可能）。"""
        return "\n".join(f"アプローチ行{i:02d}" for i in range(1, n + 1))

    def test_approach_truncated_to_tail_15_lines_when_over_limit(
        self, tmp_path: Path
    ) -> None:
        """アプローチが 16 行以上のとき末尾 15 行のみ注入され、先頭行は出力されない（AC-5）。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        approach_text = self._make_long_approach(20)  # 20行

        session_file = sessions_dir / "20260614.tmp"
        session_file.write_text(
            "SESSION: 20260614\n"
            "AGENT: \n"
            "DURATION: \n"
            "現在地: \n"
            "\n"
            f"## うまくいったアプローチ\n"
            f"{approach_text}\n"
            "\n"
            "## 試みたが失敗したアプローチ\n"
            "\n"
            "## 残タスク\n",
            encoding="utf-8",
        )

        result = _run_main_subprocess(sessions_dir)

        assert result.returncode == 0, f"期待 exit 0、実際 {result.returncode}"
        output = result.stdout

        # 末尾15行（行06〜行20）が含まれること
        assert "アプローチ行20" in output, (
            "末尾15行の最後の行（アプローチ行20）が出力に含まれるべき。"
            f"stdout: {output!r}"
        )
        assert "アプローチ行06" in output, (
            "末尾15行の先頭行（20行中の行06）が出力に含まれるべき。"
            f"stdout: {output!r}"
        )

        # 先頭5行（行01〜行05）は含まれないこと（末尾15行に入らない）
        assert "アプローチ行01" not in output, (
            "20行中の行01は末尾15行に含まれない。出力されるべきでない（AC-5）。"
            f"stdout: {output!r}"
        )
        assert "アプローチ行05" not in output, (
            "20行中の行05は末尾15行に含まれない。出力されるべきでない（AC-5）。"
            f"stdout: {output!r}"
        )

    def test_approach_not_truncated_when_within_limit(self, tmp_path: Path) -> None:
        """アプローチが 15 行以下のとき全行そのまま出力される。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        approach_text = self._make_long_approach(15)  # 15行（上限ちょうど）

        session_file = sessions_dir / "20260614.tmp"
        session_file.write_text(
            "SESSION: 20260614\n"
            "AGENT: \n"
            "DURATION: \n"
            "現在地: \n"
            "\n"
            f"## うまくいったアプローチ\n"
            f"{approach_text}\n"
            "\n"
            "## 試みたが失敗したアプローチ\n"
            "\n"
            "## 残タスク\n",
            encoding="utf-8",
        )

        result = _run_main_subprocess(sessions_dir)

        assert result.returncode == 0, f"期待 exit 0、実際 {result.returncode}"
        output = result.stdout

        # 全15行が含まれること
        assert "アプローチ行01" in output, (
            "15行以下のとき先頭行（アプローチ行01）も出力されるべき。"
            f"stdout: {output!r}"
        )
        assert "アプローチ行15" in output, (
            "15行以下のとき末尾行（アプローチ行15）も出力されるべき。"
            f"stdout: {output!r}"
        )


# ---------------------------------------------------------------------------
# 18. main 経由: 全セクション空 + 現在地空 → no-op（AC-2 / architecture §3.2 step4）
# ---------------------------------------------------------------------------


class TestMainNoopWhenAllEmptyAndGenbaEmpty:
    """main: 全セクションが空かつ現在地も空のときは no-op（exit 0・無出力）。

    architecture §3.2 step4 の early-exit 判定に対応する。
    genba が進行中でない（空 or 完了）かつ pending_todos・successes・failures が
    すべて空のときは従来どおり exit 0 で何も出力しない。
    """

    def test_noop_when_all_empty_and_genba_empty(self, tmp_path: Path) -> None:
        """全セクション空 + 現在地空 → exit 0・無出力。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        session_file = sessions_dir / "20260614.tmp"
        session_file.write_text(
            "SESSION: 20260614\n"
            "AGENT: \n"
            "DURATION: \n"
            "現在地: \n"
            "\n"
            "## うまくいったアプローチ\n"
            "\n"
            "## 試みたが失敗したアプローチ\n"
            "\n"
            "## 残タスク\n",
            encoding="utf-8",
        )

        result = _run_main_subprocess(sessions_dir)

        assert result.returncode == 0, f"期待 exit 0、実際 {result.returncode}"
        assert result.stdout.strip() == "", (
            "全セクション空かつ現在地空のとき stdout は空であるべき（no-op）。"
            f"stdout: {result.stdout!r}"
        )

    def test_noop_when_all_empty_and_genba_done(self, tmp_path: Path) -> None:
        """全セクション空 + 現在地「完了」→ exit 0・無出力（no-op）。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        session_file = sessions_dir / "20260614.tmp"
        session_file.write_text(
            "SESSION: 20260614\n"
            "AGENT: \n"
            "DURATION: \n"
            "現在地: 完了\n"
            "\n"
            "## うまくいったアプローチ\n"
            "\n"
            "## 試みたが失敗したアプローチ\n"
            "\n"
            "## 残タスク\n",
            encoding="utf-8",
        )

        result = _run_main_subprocess(sessions_dir)

        assert result.returncode == 0, f"期待 exit 0、実際 {result.returncode}"
        assert result.stdout.strip() == "", (
            "全セクション空かつ現在地「完了」のとき stdout は空であるべき（no-op）。"
            f"stdout: {result.stdout!r}"
        )


# ---------------------------------------------------------------------------
# 19. main 経由: 現在地行なし（旧形式）→ クラッシュしない（後方互換 AC-2）
# ---------------------------------------------------------------------------


class TestMainBackwardCompatNoPresentLocation:
    """main: 「現在地:」行が存在しない旧形式のセッションファイルでもクラッシュしない（AC-2）。

    architecture §2.3 の後方互換設計: extract_genba が None→空文字を返し
    進行中判定が false になる（復帰指示を出さない）。既存の残タスク注入等は従来どおり動作する。
    """

    def test_no_crash_with_old_format_session(self, tmp_path: Path) -> None:
        """現在地行なしの旧形式セッションを渡してもエラーが発生しない。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        session_file = sessions_dir / "20260614.tmp"
        # 現在地: 行なしの旧形式
        session_file.write_text(
            "SESSION: 20260614\n"
            "AGENT: \n"
            "DURATION: \n"
            "\n"
            "## うまくいったアプローチ\n"
            "\n"
            "## 試みたが失敗したアプローチ\n"
            "\n"
            "## 残タスク\n"
            "- [ ] 旧形式タスクA\n",
            encoding="utf-8",
        )

        result = _run_main_subprocess(sessions_dir)

        # クラッシュしないこと（exit 0）
        assert result.returncode == 0, (
            "旧形式セッション（現在地行なし）でクラッシュした。後方互換が壊れている（AC-2）。"
            f"returncode: {result.returncode}\nstderr: {result.stderr!r}"
        )

        # 復帰指示が出ないこと（現在地行なし → 空扱い → 進行中でない）
        assert "dev-workflow 進行中" not in result.stdout, (
            "旧形式セッション（現在地行なし）で復帰指示が出力されている。"
            "現在地なし＝空扱い＝ワークフロー外とすべき（AC-2）。"
            f"stdout: {result.stdout!r}"
        )

        # 残タスクは従来どおり出力されること（L-03: - [ ] プレフィックス込みの完全一致）
        assert "- [ ] 旧形式タスクA" in result.stdout, (
            "旧形式セッションの残タスクが '- [ ] 旧形式タスクA' の形式で出力されていない。"
            "現在地行なしでも残タスク注入は従来どおり動作すべき（- [ ] プレフィックス含む）。"
            f"stdout: {result.stdout!r}"
        )


# ---------------------------------------------------------------------------
# 20-22. extract_genba の単体テスト（F4 / CR M-03）
# ---------------------------------------------------------------------------

_LS = chr(0x2028)  # Line Separator（U+2028）
_PS = chr(0x2029)  # Paragraph Separator（U+2029）


class TestExtractGenba:
    """extract_genba() のモジュールレベル単体テスト（CR M-03）。

    architecture §2.3 に従い、「現在地:」行の値を抽出する純粋関数の
    境界条件・副作用なしを検証する。
    """

    def _load(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> object:
        import importlib.util
        import types

        spec = importlib.util.spec_from_file_location("restore_session", HOOK_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[attr-defined]
        monkeypatch.setattr(module, "SESSIONS_DIR", str(tmp_path))
        return module

    def test_extracts_normal_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """「現在地:」行から正常な値を取り出せること。"""
        module = self._load(monkeypatch, tmp_path)
        content = "SESSION: 20260614\n現在地: フェーズD 実装中\n## 残タスク\n"
        assert module.extract_genba(content) == "フェーズD 実装中"

    def test_returns_empty_when_no_genba_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """「現在地:」行が存在しない場合、空文字列を返す（後方互換）。"""
        module = self._load(monkeypatch, tmp_path)
        content = "SESSION: 20260614\nAGENT: \n## 残タスク\n"
        assert module.extract_genba(content) == ""

    def test_returns_empty_when_value_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """「現在地: 」行の値が空のとき空文字列を返す。"""
        module = self._load(monkeypatch, tmp_path)
        content = "SESSION: 20260614\n現在地: \n## 残タスク\n"
        assert module.extract_genba(content) == ""

    def test_trims_trailing_whitespace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """値の末尾空白がトリムされること。"""
        module = self._load(monkeypatch, tmp_path)
        content = "現在地:   フェーズB   \n## 残タスク\n"
        assert module.extract_genba(content) == "フェーズB"

    def test_returns_first_match_when_multiple_genba_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """「現在地:」行が複数回出現した場合、最初のマッチを返す（MULTILINE 先勝ち）。"""
        module = self._load(monkeypatch, tmp_path)
        content = "現在地: フェーズD\n何か\n現在地: フェーズE\n"
        result = module.extract_genba(content)
        assert result == "フェーズD", (
            f"複数の「現在地:」行がある場合は最初の値を返すべき。実際: {result!r}"
        )

    def test_tab_separated_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """「現在地:\\t値」のタブ区切りでも値を抽出できること。"""
        module = self._load(monkeypatch, tmp_path)
        content = "現在地:\tフェーズB\n## 残タスク\n"
        assert module.extract_genba(content) == "フェーズB"


# ---------------------------------------------------------------------------
# 23-26. _tail の単体テスト（F4 / CR M-03 / M-04）
# ---------------------------------------------------------------------------


class TestTail:
    """_tail(text, n) のモジュールレベル単体テスト（CR M-03 / M-04）。

    n=0 の反直感挙動（lines[-0:] = 全体を返す）を仕様として固定し、
    境界条件を網羅する。
    """

    def _load(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> object:
        import importlib.util

        spec = importlib.util.spec_from_file_location("restore_session", HOOK_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[attr-defined]
        monkeypatch.setattr(module, "SESSIONS_DIR", str(tmp_path))
        return module

    def test_returns_last_n_lines_when_over_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """行数が n を超えるとき末尾 n 行のみ返す。"""
        module = self._load(monkeypatch, tmp_path)
        text = "\n".join(f"行{i:02d}" for i in range(1, 21))  # 20行
        result = module._tail(text, 15)
        result_lines = result.splitlines()
        assert len(result_lines) == 15, f"期待 15 行、実際 {len(result_lines)} 行"
        assert result_lines[0] == "行06", f"先頭行は '行06' であるべき、実際: {result_lines[0]!r}"
        assert result_lines[-1] == "行20", f"末尾行は '行20' であるべき、実際: {result_lines[-1]!r}"

    def test_returns_full_text_when_within_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """行数が n 以下のときテキスト全体をそのまま返す（切り詰めない）。"""
        module = self._load(monkeypatch, tmp_path)
        text = "行01\n行02\n行03"  # 3行（n=15以下）
        result = module._tail(text, 15)
        assert result == text, f"n 以下のとき全体を返すべき、実際: {result!r}"

    def test_returns_empty_string_when_text_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """空文字列の場合は空文字列を返す。"""
        module = self._load(monkeypatch, tmp_path)
        result = module._tail("", 15)
        assert result == "", f"空入力は空出力であるべき、実際: {result!r}"

    def test_n_zero_returns_full_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """n=0 のとき lines[-0:] = lines[0:] = 全体を返す（反直感挙動・仕様として固定）。

        Python では -0 == 0 であり lines[-0:] は lines[0:]（全体）と等価。
        呼び出し元は APPROACH_TAIL_LINES=15（固定定数）のため実害はないが、
        この挙動を仕様として文書化・固定する（CR M-04）。
        """
        module = self._load(monkeypatch, tmp_path)
        text = "行01\n行02\n行03"
        result = module._tail(text, 0)
        # n=0 のとき全体を返す（lines[-0:] == lines[:] == 全体）
        assert result == text, (
            f"n=0 のとき全行を返す仕様（lines[-0:] = 全体）。実際: {result!r}"
        )

    def test_n_exactly_equals_line_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """行数が n と等しいとき全体をそのまま返す（境界: len(lines) > n が False）。"""
        module = self._load(monkeypatch, tmp_path)
        text = "行01\n行02\n行03"  # 3行
        result = module._tail(text, 3)
        assert result == text, f"n == 行数のとき全体を返すべき（切り詰めない）、実際: {result!r}"


# ---------------------------------------------------------------------------
# 27-30. _sanitize_genba の単体テスト（F4 / CR M-03）
# ---------------------------------------------------------------------------


class TestSanitizeGenba:
    """_sanitize_genba(value) のモジュールレベル単体テスト（CR M-03）。

    F2 で sanitize_value 共通化後の確定仕様として DEL/C1/U+2028/U+2029 を除去する。
    """

    def _load(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> object:
        import importlib.util

        spec = importlib.util.spec_from_file_location("restore_session", HOOK_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[attr-defined]
        monkeypatch.setattr(module, "SESSIONS_DIR", str(tmp_path))
        return module

    def test_replaces_comment_closer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'-->' を '-- >' に置換して HTML コメントブロックの破壊を防ぐ。"""
        module = self._load(monkeypatch, tmp_path)
        result = module._sanitize_genba("フェーズD --> 完了")
        assert "-->" not in result, f"'-->' が残存している。実際: {result!r}"
        assert "-- >" in result, f"'-- >' への置換がされていない。実際: {result!r}"

    def test_removes_control_characters(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """既存の制御文字除去範囲（\\x00-\\x08/\\x0b-\\x0c/\\x0e-\\x1f）が機能すること。"""
        module = self._load(monkeypatch, tmp_path)
        value = "フェーズD\x01\x08\x0b\x1f実装中"
        result = module._sanitize_genba(value)
        assert "\x01" not in result
        assert "\x08" not in result
        assert "\x1f" not in result
        assert "フェーズD" in result
        assert "実装中" in result

    def test_removes_newlines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """改行文字（\\n/\\r）を除去すること。"""
        module = self._load(monkeypatch, tmp_path)
        assert "\n" not in module._sanitize_genba("フェーズD\n実装中")
        assert "\r" not in module._sanitize_genba("フェーズD\r実装中")

    def test_removes_del_character(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEL（\\x7f）が除去されること（F2 sanitize_value 共通化後の確定仕様）。"""
        module = self._load(monkeypatch, tmp_path)
        result = module._sanitize_genba("フェーズD\x7f実装中")
        assert "\x7f" not in result, (
            "DEL (\\x7f) が除去されていない。回帰を検出した場合は session_utils.py の sanitize_value を確認すること。"
            f"実際: {result!r}"
        )

    def test_removes_c1_control_characters(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C1 制御文字（\\x80-\\x9f、CSI=\\x9b 等）が除去されること（F2 sanitize_value 共通化後の確定仕様）。"""
        module = self._load(monkeypatch, tmp_path)
        result = module._sanitize_genba("フェーズ\x9b[31mD 実装中")
        assert "\x9b" not in result, (
            "C1 制御文字 CSI (\\x9b) が除去されていない。回帰を検出した場合は session_utils.py の sanitize_value を確認すること。"
            f"実際: {result!r}"
        )

    def test_removes_unicode_line_separator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """U+2028（Line Separator）が除去されること（F2 sanitize_value 共通化後の確定仕様）。"""
        module = self._load(monkeypatch, tmp_path)
        result = module._sanitize_genba("フェーズD" + _LS + "実装中")
        assert _LS not in result, (
            "U+2028 (Line Separator) が除去されていない。回帰を検出した場合は session_utils.py の sanitize_value を確認すること。"
            f"実際: {result!r}"
        )

    def test_removes_unicode_paragraph_separator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """U+2029（Paragraph Separator）が除去されること（F2 sanitize_value 共通化後の確定仕様）。"""
        module = self._load(monkeypatch, tmp_path)
        result = module._sanitize_genba("フェーズD" + _PS + "実装中")
        assert _PS not in result, (
            "U+2029 (Paragraph Separator) が除去されていない。回帰を検出した場合は session_utils.py の sanitize_value を確認すること。"
            f"実際: {result!r}"
        )


# ---------------------------------------------------------------------------
# 31-34. 3セクション出力のサニタイズテスト（F3 / SR M-2）
# ---------------------------------------------------------------------------


class TestMainSectionSanitize:
    """main: 3セクション出力（残タスク・成功・失敗）に含まれる制御文字が除去されること（SR M-2）。

    F3 で残タスク・成功・失敗 3 セクションすべてに sanitize_value を適用した確定仕様。
    """

    def _make_session_with_control_chars(
        self,
        sessions_dir: Path,
        todo_line: str,
        success_line: str,
        failure_line: str,
    ) -> None:
        """制御文字を含む 3 セクションを持つセッションファイルを作成する。"""
        content = (
            "SESSION: 20260614\n"
            "AGENT: \n"
            "DURATION: \n"
            "現在地: フェーズD 実装中\n"
            "\n"
            f"## うまくいったアプローチ\n"
            f"{success_line}\n"
            "\n"
            f"## 試みたが失敗したアプローチ\n"
            f"{failure_line}\n"
            "\n"
            f"## 残タスク\n"
            f"{todo_line}\n"
        )
        (sessions_dir / "20260614.tmp").write_text(content, encoding="utf-8")

    def test_control_char_in_todo_line_is_removed_from_output(
        self, tmp_path: Path
    ) -> None:
        """残タスク行に ESC（\\x1b）が含まれる場合、出力から除去されること（SR M-2）。

        F3 で sanitize_value を残タスク行に適用した確定仕様。
        """
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        todo_line = "- [ ] タスクA\x1b[31m\x1b[0m"  # ANSI エスケープ埋め込み
        self._make_session_with_control_chars(
            sessions_dir,
            todo_line=todo_line,
            success_line="成功例",
            failure_line="失敗例",
        )

        result = _run_main_subprocess(sessions_dir)
        assert result.returncode == 0
        output = result.stdout

        # ESC が出力に残らないこと
        assert "\x1b" not in output, (
            "残タスク行の ESC (\\x1b) が stdout に素通しされている（SR M-2）。"
            "回帰を検出した場合は session_utils.py の sanitize_value および restore_session.py の main 関数を確認すること。"
            f"stdout repr: {output!r}"
        )
        # - [ ] プレフィックスは保持されること（フィルタ前のラインに有効な - [ ] があるため）
        assert "- [ ]" in output, (
            "サニタイズ後も '- [ ]' プレフィックスは保持されるべき。"
            f"stdout: {output!r}"
        )

    def test_c1_char_in_todo_line_is_removed_from_output(
        self, tmp_path: Path
    ) -> None:
        """残タスク行に C1 制御文字（\\x9b / CSI）が含まれる場合、出力から除去されること（SR M-2）。

        F3 で sanitize_value を残タスク行に適用した確定仕様。
        """
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        todo_line = "- [ ] タスクA\x9b"  # C1 CSI 埋め込み
        self._make_session_with_control_chars(
            sessions_dir,
            todo_line=todo_line,
            success_line="成功例",
            failure_line="失敗例",
        )

        result = _run_main_subprocess(sessions_dir)
        assert result.returncode == 0
        output = result.stdout

        assert "\x9b" not in output, (
            "残タスク行の C1 制御文字 CSI (\\x9b) が stdout に素通しされている（SR M-2）。"
            "回帰を検出した場合は session_utils.py の sanitize_value および restore_session.py の main 関数を確認すること。"
            f"stdout repr: {output!r}"
        )

    def test_unicode_ls_in_todo_line_is_not_injected_via_splitlines(
        self, tmp_path: Path
    ) -> None:
        """残タスク行に U+2028 が含まれる場合、Python の splitlines() で行区切りされ
        U+2028 自体が stdout に出ないことを確認する（splitlines 動作の記録テスト）。

        注: Python の str.splitlines() は U+2028 を行区切りとして扱うため、
        extract_section → splitlines() の経路で U+2028 は行区切り文字として除去される。
        これは「サニタイズで除去」ではなく「splitlines の暗黙的行区切り」による除去であり、
        MEMORY.md「U+2028/U+2029 を含む JSONL テストの落とし穴」と同じ現象。
        U+2028 のサニタイズ確定仕様テストは TestSanitizeGenba::test_removes_unicode_line_separator
        および TestSanitizeValue::test_removes_unicode_line_separator で行う。
        """
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        # U+2028 は splitlines() で行区切りになるため、行への注入は無害（期待通り PASS する）
        todo_line = "- [ ] タスクA" + _LS
        self._make_session_with_control_chars(
            sessions_dir,
            todo_line=todo_line,
            success_line="成功例",
            failure_line="失敗例",
        )

        result = _run_main_subprocess(sessions_dir)
        assert result.returncode == 0

        # U+2028 は splitlines() で行区切りとして除去される（期待通りの PASS）
        raw_bytes = result.stdout.encode("utf-8")
        # U+2028 の UTF-8 表現は E2 80 A8
        # splitlines 動作により既に除去されているため、このアサートは通過する（記録目的）
        assert b"\xe2\x80\xa8" not in raw_bytes, (
            "U+2028 が stdout に含まれている（splitlines で除去される想定だが素通しした）。"
            f"stdout repr: {result.stdout!r}"
        )

    def test_normal_todo_line_preserved_after_sanitize(self, tmp_path: Path) -> None:
        """正常な '- [ ] タスクA' 行は制御文字を含まないため出力が不変であること（過剰除去しない）。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        self._make_session_with_control_chars(
            sessions_dir,
            todo_line="- [ ] 通常タスクA",
            success_line="通常の成功例",
            failure_line="通常の失敗例",
        )

        result = _run_main_subprocess(sessions_dir)
        assert result.returncode == 0
        output = result.stdout

        assert "- [ ] 通常タスクA" in output, (
            "正常な残タスク行が出力に含まれていない（過剰除去の疑い）。"
            f"stdout: {output!r}"
        )
        assert "通常の成功例" in output, (
            "正常な成功例が出力に含まれていない（過剰除去の疑い）。"
            f"stdout: {output!r}"
        )
        assert "通常の失敗例" in output, (
            "正常な失敗例が出力に含まれていない（過剰除去の疑い）。"
            f"stdout: {output!r}"
        )

    def test_control_char_in_success_line_is_removed_from_output(
        self, tmp_path: Path
    ) -> None:
        """「うまくいったアプローチ」セクション内の行に ESC（\\x1b）が含まれる場合、
        出力から除去されること（SR M-2 / CR L-01）。

        main() の④処理（successes 行ごとに sanitize_value 適用）により、
        成功セクション内の制御文字はサニタイズされてから出力される。
        セクション本文は保持されつつ、制御文字のみが除去されることを検証する。
        """
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        self._make_session_with_control_chars(
            sessions_dir,
            todo_line="- [ ] 通常タスク",
            success_line="成功例\x1b[32mカラーテキスト\x1b[0m",
            failure_line="通常の失敗例",
        )

        result = _run_main_subprocess(sessions_dir)
        assert result.returncode == 0
        output = result.stdout

        assert "\x1b" not in output, (
            "「うまくいったアプローチ」行の ESC (\\x1b) が stdout に残存している（SR M-2）。"
            f"stdout repr: {output!r}"
        )
        assert "カラーテキスト" in output, (
            "ESC 除去後も成功セクションの本文（カラーテキスト）は出力に含まれるべき。"
            f"stdout: {output!r}"
        )
        assert "## うまくいったアプローチ" in output, (
            "成功セクション見出しが出力に含まれていない。"
            f"stdout: {output!r}"
        )

    def test_c1_char_in_success_line_is_removed_from_output(
        self, tmp_path: Path
    ) -> None:
        """「うまくいったアプローチ」セクション内の行に C1 制御文字（\\x85 / NEL）が含まれる場合、
        出力から除去されること（SR M-2 / CR L-01）。

        main() の④処理（successes 行ごとに sanitize_value 適用）により、
        C1 制御文字（\\x80-\\x9f の範囲）はサニタイズされる。
        """
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        self._make_session_with_control_chars(
            sessions_dir,
            todo_line="- [ ] 通常タスク",
            success_line="成功例\x85NEL注入",
            failure_line="通常の失敗例",
        )

        result = _run_main_subprocess(sessions_dir)
        assert result.returncode == 0
        output = result.stdout

        assert "\x85" not in output, (
            "「うまくいったアプローチ」行の C1 制御文字 NEL (\\x85) が stdout に残存している（SR M-2）。"
            f"stdout repr: {output!r}"
        )
        assert "NEL注入" in output, (
            "\\x85 除去後も成功セクションの本文（NEL注入）は出力に含まれるべき。"
            f"stdout: {output!r}"
        )

    def test_control_char_in_failure_line_is_removed_from_output(
        self, tmp_path: Path
    ) -> None:
        """「試みたが失敗したアプローチ」セクション内の行に ESC（\\x1b）が含まれる場合、
        出力から除去されること（SR M-2 / CR L-01）。

        main() の⑤処理（failures 行ごとに sanitize_value 適用）により、
        失敗セクション内の制御文字はサニタイズされてから出力される。
        """
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        self._make_session_with_control_chars(
            sessions_dir,
            todo_line="- [ ] 通常タスク",
            success_line="通常の成功例",
            failure_line="失敗例\x1b[31mエラーテキスト\x1b[0m",
        )

        result = _run_main_subprocess(sessions_dir)
        assert result.returncode == 0
        output = result.stdout

        assert "\x1b" not in output, (
            "「試みたが失敗したアプローチ」行の ESC (\\x1b) が stdout に残存している（SR M-2）。"
            f"stdout repr: {output!r}"
        )
        assert "エラーテキスト" in output, (
            "ESC 除去後も失敗セクションの本文（エラーテキスト）は出力に含まれるべき。"
            f"stdout: {output!r}"
        )
        assert "## 試みたが失敗したアプローチ" in output, (
            "失敗セクション見出しが出力に含まれていない。"
            f"stdout: {output!r}"
        )

    def test_c1_char_in_failure_line_is_removed_from_output(
        self, tmp_path: Path
    ) -> None:
        """「試みたが失敗したアプローチ」セクション内の行に C1 制御文字（\\x9b / CSI）が含まれる場合、
        出力から除去されること（SR M-2 / CR L-01）。

        main() の⑤処理（failures 行ごとに sanitize_value 適用）により、
        C1 制御文字（\\x80-\\x9f の範囲）はサニタイズされる。
        DEL（\\x7f）や C1（\\x80-\\x9f）を含む文字列が失敗セクションに入った場合の
        仕様を固定する regression guard テスト（カバレッジ穴埋め / CR L-01）。
        """
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        self._make_session_with_control_chars(
            sessions_dir,
            todo_line="- [ ] 通常タスク",
            success_line="通常の成功例",
            failure_line="失敗例\x9b[0mCSI注入",
        )

        result = _run_main_subprocess(sessions_dir)
        assert result.returncode == 0
        output = result.stdout

        assert "\x9b" not in output, (
            "「試みたが失敗したアプローチ」行の C1 制御文字 CSI (\\x9b) が stdout に残存している（SR M-2）。"
            f"stdout repr: {output!r}"
        )
        assert "CSI注入" in output, (
            "\\x9b 除去後も失敗セクションの本文（CSI注入）は出力に含まれるべき。"
            f"stdout: {output!r}"
        )


# ---------------------------------------------------------------------------
# 35-38. date_str の YYYYMMDD 形式バリデーション（F9 / SR L-3）
# ---------------------------------------------------------------------------


class TestMainDateStrValidation:
    """main: date_str が YYYYMMDD（8桁数字）形式でない場合は exit 0 でスキップ（SR L-3）。

    任意の文字列がファイル名経由で date_str に混入した場合にヘッダへの注入を防ぐ確定仕様。
    """

    def test_normal_8digit_date_processes_normally(self, tmp_path: Path) -> None:
        """正常な 8 桁数字のファイル名（例: 20260614.tmp）は従来どおり処理されること。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        session_file = sessions_dir / "20260614.tmp"
        session_file.write_text(
            "SESSION: 20260614\n"
            "AGENT: \n"
            "DURATION: \n"
            "現在地: \n"
            "\n"
            "## うまくいったアプローチ\n"
            "成功例\n"
            "\n"
            "## 試みたが失敗したアプローチ\n"
            "\n"
            "## 残タスク\n",
            encoding="utf-8",
        )

        result = _run_main_subprocess(sessions_dir)
        assert result.returncode == 0
        # ヘッダに date_str が出ること（正常処理）
        assert "20260614" in result.stdout, (
            "正常な 8 桁日付がヘッダに出力されていない。後方互換が壊れている。"
            f"stdout: {result.stdout!r}"
        )

    def test_invalid_date_str_with_extra_chars_is_skipped(self, tmp_path: Path) -> None:
        """8 桁より長いファイル名（例: 20260614abc.tmp）は exit 0 でスキップされること（SR L-3）。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        session_file = sessions_dir / "20260614abc.tmp"
        session_file.write_text(
            "## うまくいったアプローチ\n成功例\n\n## 試みたが失敗したアプローチ\n\n## 残タスク\n",
            encoding="utf-8",
        )

        result = _run_main_subprocess(sessions_dir)
        assert result.returncode == 0, (
            "異常なファイル名（8桁以外）は exit 0 でスキップされるべき（SR L-3）。"
            f"returncode: {result.returncode}\nstdout: {result.stdout!r}"
        )
        assert result.stdout.strip() == "", (
            "異常なファイル名のとき stdout は空であるべき（スキップ）。"
            f"stdout: {result.stdout!r}"
        )

    def test_invalid_date_str_non_digit_is_skipped(self, tmp_path: Path) -> None:
        """8 桁でも数字以外の文字を含むファイル名は exit 0 でスキップされること（SR L-3）。

        例: 2026061X.tmp（最後の桁がXなど）。
        """
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        session_file = sessions_dir / "2026061X.tmp"
        session_file.write_text(
            "## うまくいったアプローチ\n成功例\n\n## 試みたが失敗したアプローチ\n\n## 残タスク\n",
            encoding="utf-8",
        )

        result = _run_main_subprocess(sessions_dir)
        assert result.returncode == 0, (
            "非数字を含むファイル名（2026061X.tmp）は exit 0 でスキップされるべき（SR L-3）。"
            f"returncode: {result.returncode}"
        )
        assert result.stdout.strip() == "", (
            "非数字を含むファイル名のとき stdout は空であるべき（スキップ）。"
            f"stdout: {result.stdout!r}"
        )


# ---------------------------------------------------------------------------
# 以降は stdout 10,000 文字上限対応（残タスクの文字数予算 + fail-loud マーカー）の
# 追加テスト群。architecture-report-20260730-220207.md §2-1 / §2-2 / §2-3 / §5 に対応する。
# 既存 53 ケースは 1 行も変更していない（末尾追記のみ）。
# ---------------------------------------------------------------------------

import os
import re


# ---------------------------------------------------------------------------
# 39-45. _fit_items の境界値テスト（architecture §5-1 / §2-2 の境界条件表と 1:1）
# ---------------------------------------------------------------------------


class TestFitItems:
    """_fit_items(items, budget) のモジュールレベル単体テスト（architecture §2-2）。

    「合計」の定義は len('\\n'.join(items)) = Σlen(item) + (件数 - 1)。
    仕様（architecture §2-2 のアルゴリズム）:
      - 先頭から順に採用し、予算に入らない項目に出会った時点で **break** する
        （continue ではない = 返り値は必ず先頭からの連続した前置部分になる）
      - 項目の途中では絶対に切らない
      - budget <= 0 のときは必ず空リストを返す

    境界を main() 経由で作ると固定部長に依存して脆くなるため、<= 境界の厳密固定は
    ヘルパー単体で行う（既存 TestTail と同じくモジュールを直接ロードして呼ぶスタイル）。
    """

    def test_total_exactly_equals_budget_keeps_all_items(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A-1: 合計がちょうど budget と等しいとき全件採用される（判定は > なので等号は収まる側）。"""
        module = _load_module(monkeypatch, tmp_path)
        items = ["aaa", "bbbb", "cc"]
        budget = len("\n".join(items))  # 3 + 4 + 2 + 改行 2 = 11
        result = module._fit_items(items, budget)
        assert result == items, (
            f"合計 == budget（{budget}）のとき全 {len(items)} 件が採用されるべき。実際: {result!r}"
        )

    def test_total_one_over_budget_drops_last_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A-2: 合計が budget + 1 のとき末尾 1 件だけが落ち、残りは全採用される。"""
        module = _load_module(monkeypatch, tmp_path)
        items = ["aaa", "bbbb", "cc"]
        budget = len("\n".join(items)) - 1  # 合計 == budget + 1 の状況
        result = module._fit_items(items, budget)
        assert result == items[:-1], (
            f"合計 == budget + 1 のとき末尾 1 件のみ落ちるべき（期待 {items[:-1]!r}）。実際: {result!r}"
        )

    def test_budget_zero_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A-3: budget == 0 のとき必ず空リストを返す。

        注意（architecture §2-7 / 取り違え防止）: 同じ「切り詰め」でも _tail の n=0 は
        lines[-0:] == 全体という反直感挙動で **全行を返す**（既存 TestTail::test_n_zero_returns_full_text）。
        _fit_items はその逆で、budget 0 は「0 件しか入らない」を意味し必ず 0 件を返す。
        """
        module = _load_module(monkeypatch, tmp_path)
        result = module._fit_items(["- [ ] タスクA", "- [ ] タスクB"], 0)
        assert result == [], (
            "budget == 0 のときは空リストを返すべき（_tail の n=0 とは逆の境界意味）。"
            f"実際: {result!r}"
        )

    def test_negative_budget_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A-4: budget < 0（負値）でも安全に空リストを返す。

        注意（architecture §2-7 / 取り違え防止）: _tail の n=0 が全体を返すのとは **逆** に、
        _fit_items は budget が 0 でも負でも必ず 0 件を返す。
        固定部が上限に迫って budget が負になるケース（本ファイル B-6 相当）で
        クラッシュしないことの土台になる。
        """
        module = _load_module(monkeypatch, tmp_path)
        result = module._fit_items(["- [ ] タスクA"], -1)
        assert result == [], (
            f"budget < 0 のときは空リストを返すべき（負値でも安全）。実際: {result!r}"
        )

    def test_first_item_alone_over_budget_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A-5: items[0] が単独で budget を超えるとき空リストを返す（項目分割禁止の帰結）。"""
        module = _load_module(monkeypatch, tmp_path)
        items = ["x" * 10, "y"]
        result = module._fit_items(items, 5)
        assert result == [], (
            "先頭項目が単独で budget を超えるときは空リストを返すべき（項目の途中では切らない）。"
            f"実際: {result!r}"
        )

    def test_empty_items_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A-6: items == [] のとき空リストを返す。"""
        module = _load_module(monkeypatch, tmp_path)
        result = module._fit_items([], 100)
        assert result == [], f"空入力は空出力であるべき。実際: {result!r}"

    def test_stops_at_first_oversized_item_without_skipping_ahead(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A-7: 入らない項目に出会ったら break する（後続の小さい項目を拾いに行かない）。

        2 件目が巨大・3 件目が極小のとき、continue 実装なら 3 件目を拾ってしまう。
        break 実装であること（返り値が先頭からの連続した前置部分であること）を機械固定する。
        """
        module = _load_module(monkeypatch, tmp_path)
        items = ["a" * 10, "b" * 5000, "c"]
        result = module._fit_items(items, 20)
        assert result == [items[0]], (
            "入らない項目に出会った時点で break し、後続の小さい項目（3 件目）を拾わないべき。"
            f"期待 {[items[0]]!r}、実際: {result!r}"
        )


# ---------------------------------------------------------------------------
# 46-52. main 経由: 残タスクの文字数予算と fail-loud マーカー（architecture §5-2）
#
# 上限 assert の計測方法（本サイクルの確定裁定・後任向けメモ）:
#   - _run_main_subprocess は subprocess.run(..., text=True, encoding="utf-8") を使う
#     （本ファイル 96-101 行）。text モードの universal newlines により、Windows の
#     \r\n は **読み取り時に既に \n へ正規化されている**
#   - したがって .replace('\r\n', '\n') は恒等変換（no-op）であり、付けると
#     「テストが CRLF を見ている」という誤解を生むだけで意味がない
#   - さらに実装側は sys.stdout.reconfigure(encoding='utf-8', newline='\n') を採用し、
#     Windows 実出力からも CRLF が構造的に消える。よって「テストが見ている len(stdout)」＝
#     「ハーネスが数える文字数」が 3 OS で厳密一致する
# ⇒ 上限 assert は必ず生の len(result.stdout) 1 本で行う（正規化を挟まない）。
# ---------------------------------------------------------------------------


class TestMainOutputBudget:
    """main: stdout 10,000 文字上限に収めるための残タスク予算と fail-loud マーカー。

    マーカー文言は将来の微修正で無関係に赤くならないよう、逐語全文ではなく
    安定部分（'件のうち先頭' / 正規表現 '全 (\\d+) 件のうち先頭 (\\d+) 件' / パス文字列）で検査する。
    """

    # マーカー文言の安定部分（過検知テストにも使う）
    MARKER_STABLE = "件のうち先頭"
    # 総件数・表示件数を抽出する正規表現（architecture §2-4 の逐語文言の数値部）
    MARKER_RE = re.compile(r"全 (\d+) 件のうち先頭 (\d+) 件")

    def _write_session(
        self,
        sessions_dir: Path,
        *,
        todos: list[str] | None = None,
        successes: list[str] | None = None,
        failures: list[str] | None = None,
        date_str: str = "20260730",
    ) -> Path:
        """テスト用セッションファイルを作成してパスを返す（既存フィクスチャと同じ並び順）。"""
        todos = todos or []
        successes = successes or []
        failures = failures or []
        content = (
            f"SESSION: {date_str}\n"
            "AGENT: \n"
            "DURATION: \n"
            "現在地: \n"
            "\n"
            "## うまくいったアプローチ\n"
            + ("\n".join(successes) + "\n" if successes else "")
            + "\n## 試みたが失敗したアプローチ\n"
            + ("\n".join(failures) + "\n" if failures else "")
            + "\n## 残タスク\n"
            + ("\n".join(todos) + "\n" if todos else "")
        )
        session_file = sessions_dir / f"{date_str}.tmp"
        session_file.write_text(content, encoding="utf-8")
        return session_file

    def _make_todos(self, count: int, size: int) -> list[str]:
        """1 件 size 文字ちょうどの '- [ ] ' 行を count 件つくる。"""
        out = []
        for i in range(count):
            prefix = f"- [ ] T{i:02d} "
            out.append(prefix + "A" * (size - len(prefix)))
        return out

    def test_no_marker_when_todos_fit_in_budget(self, tmp_path: Path) -> None:
        """B-1: 残タスクが予算未満のとき全件出力され、マーカーは出ない（過検知しない・requirements §7-3）。

        注: このケースは上限値を必要としないため MAX_OUTPUT_CHARS を参照しない
        （参照すると実装前に AttributeError で赤くなり、「実装前から緑であるべき」条件と噛み合わない）。
        """
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        todos = ["- [ ] 小タスクA", "- [ ] 小タスクB", "- [ ] 小タスクC"]
        self._write_session(sessions_dir, todos=todos)

        result = _run_main_subprocess(sessions_dir)

        assert result.returncode == 0, (
            f"期待 exit 0、実際 {result.returncode}\nstderr: {result.stderr!r}"
        )
        stdout = result.stdout

        assert "## 残タスク" in stdout, (
            f"残タスクセクションが出力されるべき。stdout: {stdout!r}"
        )
        for todo in todos:
            assert todo in stdout, (
                f"予算未満のときは全件出力されるべき（{todo!r} が欠落）。stdout: {stdout!r}"
            )
        assert self.MARKER_STABLE not in stdout, (
            "予算内に全件収まるときは切り詰めマーカーを出してはいけない（過検知禁止）。"
            f"stdout: {stdout!r}"
        )

    def test_oversized_todos_are_truncated_with_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B-2: 1 件 2,000 文字 × 20 件のとき上限内に収まり、マーカーが件数とパスを伴って出る。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        todos = self._make_todos(20, 2000)
        self._write_session(sessions_dir, todos=todos)

        module = _load_module(monkeypatch, sessions_dir)
        max_chars = module.MAX_OUTPUT_CHARS

        result = _run_main_subprocess(sessions_dir)
        assert result.returncode == 0, (
            f"期待 exit 0、実際 {result.returncode}\nstderr: {result.stderr!r}"
        )
        stdout = result.stdout

        # (a) 上限内（生の len(stdout) 1 本で判定する。理由は本セクション冒頭のコメント参照）
        assert len(stdout) <= max_chars, (
            f"stdout が上限 {max_chars} 文字を超えた（実際 {len(stdout)} 文字）。"
        )

        # (b) マーカーが出ること
        assert self.MARKER_STABLE in stdout, (
            "予算を超えて切り詰めたときは fail-loud マーカーを出すべき（沈黙禁止）。"
            f"stdout: {stdout!r}"
        )

        # (c) 総件数 20 と表示件数が抽出できること
        m = self.MARKER_RE.search(stdout)
        assert m is not None, (
            "マーカーから '全 N 件のうち先頭 M 件' の数値が抽出できない。"
            f"stdout: {stdout!r}"
        )
        total, shown = int(m.group(1)), int(m.group(2))
        assert total == len(todos), (
            f"総件数は {len(todos)} であるべき。実際: {total}"
        )
        assert 0 < shown < total, (
            f"このフィクスチャでは一部だけが表示されるはず（0 < shown < {total}）。実際: {shown}"
        )

        # (d) セッションファイルの絶対パスが含まれること（OS 依存セパレータをハードコードしない）
        expected_path = os.path.normpath(
            os.path.join(str(sessions_dir), "20260730.tmp")
        )
        assert expected_path in stdout, (
            f"マーカーに全文参照先の絶対パス {expected_path!r} が含まれるべき。stdout: {stdout!r}"
        )

        # (e) 実際に出た - [ ] 行数が表示件数と一致すること
        todo_lines = [ln for ln in stdout.splitlines() if ln.startswith("- [ ]")]
        assert len(todo_lines) == shown, (
            f"出力中の '- [ ]' 行数（{len(todo_lines)}）がマーカーの表示件数（{shown}）と一致しない。"
        )

    def test_limit_kept_when_approach_sections_also_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B-3: アプローチ 2 セクションにも本文があるとき、固定部長を織り込んで上限内に収まる。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        todos = self._make_todos(20, 2000)
        successes = [f"S{i:02d} " + "C" * 296 for i in range(5)]  # 各 300 文字 × 5 行
        failures = [f"F{i:02d} " + "D" * 296 for i in range(5)]  # 各 300 文字 × 5 行
        self._write_session(
            sessions_dir, todos=todos, successes=successes, failures=failures
        )

        module = _load_module(monkeypatch, sessions_dir)
        max_chars = module.MAX_OUTPUT_CHARS

        result = _run_main_subprocess(sessions_dir)
        assert result.returncode == 0, (
            f"期待 exit 0、実際 {result.returncode}\nstderr: {result.stderr!r}"
        )
        stdout = result.stdout

        # 上限判定は生の len(stdout) 1 本（理由は本セクション冒頭のコメント参照）
        assert len(stdout) <= max_chars, (
            f"アプローチ 2 セクションの実長が予算計算（fixed_len）に反映されていない。"
            f"上限 {max_chars} に対し実際 {len(stdout)} 文字。"
        )
        assert self.MARKER_STABLE in stdout, (
            f"切り詰めたときはマーカーを出すべき。stdout 先頭 500 文字: {stdout[:500]!r}"
        )

    def test_marker_is_placed_right_after_heading(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B-4: マーカーは '## 残タスク' 見出しの直後・本文（最初の - [ ] 行）より前に置かれる（ADR-2）。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        todos = self._make_todos(20, 2000)
        self._write_session(sessions_dir, todos=todos)

        # 上限値そのものは使わないが、実装済みモジュールのロード可否は B-2 側で担保される
        _load_module(monkeypatch, sessions_dir)

        result = _run_main_subprocess(sessions_dir)
        assert result.returncode == 0, (
            f"期待 exit 0、実際 {result.returncode}\nstderr: {result.stderr!r}"
        )
        stdout = result.stdout

        assert self.MARKER_STABLE in stdout, (
            f"切り詰めたときはマーカーを出すべき。stdout: {stdout[:500]!r}"
        )
        heading_pos = stdout.index("## 残タスク")
        marker_pos = stdout.index(self.MARKER_STABLE)
        first_todo_pos = stdout.index("- [ ]")

        assert heading_pos < marker_pos, (
            f"マーカー（位置 {marker_pos}）は '## 残タスク' 見出し（位置 {heading_pos}）より後にあるべき。"
        )
        assert marker_pos < first_todo_pos, (
            f"マーカー（位置 {marker_pos}）は最初の '- [ ]' 行（位置 {first_todo_pos}）より前にあるべき"
            "（末尾配置はハーネス truncate でマーカー自体が溢れるため禁止・ADR-2）。"
        )

    def test_single_item_over_budget_shows_zero_items_with_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B-5: 1 項目が単独で予算超のとき、表示件数 0 のマーカーだけが出て沈黙しない。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        todos = ["- [ ] X" + "B" * 11993]  # 12,000 文字ちょうどの 1 件
        assert len(todos[0]) == 12000, "フィクスチャ前提: 1 件が 12,000 文字であること"
        self._write_session(sessions_dir, todos=todos)

        module = _load_module(monkeypatch, sessions_dir)
        max_chars = module.MAX_OUTPUT_CHARS

        result = _run_main_subprocess(sessions_dir)
        assert result.returncode == 0, (
            f"期待 exit 0、実際 {result.returncode}\nstderr: {result.stderr!r}"
        )
        stdout = result.stdout

        assert self.MARKER_STABLE in stdout, (
            "1 項目が単独で予算超のときも（沈黙せず）マーカーを出すべき。"
            f"stdout: {stdout!r}"
        )
        m = self.MARKER_RE.search(stdout)
        assert m is not None, f"マーカーの数値が抽出できない。stdout: {stdout!r}"
        assert int(m.group(1)) == 1, f"総件数は 1 であるべき。実際: {m.group(1)}"
        assert int(m.group(2)) == 0, (
            f"表示件数は 0 であるべき（項目の途中では切らない）。実際: {m.group(2)}"
        )

        todo_lines = [ln for ln in stdout.splitlines() if ln.startswith("- [ ]")]
        assert todo_lines == [], (
            f"'- [ ]' 行は 1 つも出力されないべき。実際: {todo_lines!r}"
        )
        # 上限判定は生の len(stdout) 1 本（理由は本セクション冒頭のコメント参照）
        assert len(stdout) <= max_chars, (
            f"stdout が上限 {max_chars} 文字を超えた（実際 {len(stdout)} 文字）。"
        )

    def test_no_crash_when_fixed_part_alone_exceeds_limit(self, tmp_path: Path) -> None:
        """B-6: 固定部だけで上限に迫るとき（budget2 < 0）でもクラッシュせず、マーカーは必ず出る。

        意図的に len(stdout) <= MAX_OUTPUT_CHARS を assert **しない**（assert 漏れではない）:
        本設計の上限保証は budget2 = budget_body - len(reserve) - 1 が 0 以上のときにのみ
        成立する。budget2 < 0 になるのは固定部（ワークフロー復帰指示・ヘッダ・アプローチ 2
        セクション）だけで上限に迫る場合で、これは requirements §6-4 が
        「④⑤アプローチ 2 セクション自体が単独で 10,000 文字に迫る極端なケースまでは
        今回のスコープで保証しない」と明示的にスコープ外宣言した領域と一致する。
        この領域で固定するのは「クラッシュしない・exit 0・マーカーは必ず出る」の 3 点のみ。

        ④は 15 行（APPROACH_TAIL_LINES=15 では切り詰められない行数）× 各 700 文字。
        """
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        successes = [f"S{i:02d} " + "C" * 696 for i in range(15)]  # 各 700 文字 × 15 行
        assert all(len(s) == 700 for s in successes), "フィクスチャ前提: 各行 700 文字"
        todos = ["- [ ] 残タスクA", "- [ ] 残タスクB", "- [ ] 残タスクC"]
        self._write_session(sessions_dir, todos=todos, successes=successes)

        result = _run_main_subprocess(sessions_dir)

        assert result.returncode == 0, (
            "固定部だけで上限に迫るケースでも例外を出さず exit 0 であるべき。"
            f"returncode: {result.returncode}\nstderr: {result.stderr!r}"
        )
        assert self.MARKER_STABLE in result.stdout, (
            "固定部が予算を食い潰して残タスクを出せないときも、沈黙せずマーカーを出すべき。"
            f"stdout 先頭 500 文字: {result.stdout[:500]!r}"
        )

    def test_section_omitted_when_no_pending_todos_even_with_huge_fixed_part(
        self, tmp_path: Path
    ) -> None:
        """B-7: - [ ] 行が 0 件なら、固定部が巨大（budget_body < 0）でも残タスクセクションを出さない。

        DC-GP-001 への回帰固定。既存 test_section_omitted_when_no_pending_todos は
        固定部が小さいケースしか見ていないため、budget_body が負になる条件でも
        現行の `if pending_todos:` ガードが効いていることをここで追加固定する。
        入力は B-6 と同じ（④が 15 行 × 各 700 文字）で、- [ ] 行だけを取り除いたもの。

        注: このケースは上限値を必要としないため MAX_OUTPUT_CHARS を参照しない
        （参照すると実装前に AttributeError で赤くなり、「実装前から緑であるべき」条件と噛み合わない）。
        """
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        successes = [f"S{i:02d} " + "C" * 696 for i in range(15)]  # 各 700 文字 × 15 行
        self._write_session(sessions_dir, todos=[], successes=successes)

        result = _run_main_subprocess(sessions_dir)

        assert result.returncode == 0, (
            f"期待 exit 0、実際 {result.returncode}\nstderr: {result.stderr!r}"
        )
        stdout = result.stdout

        assert "## 残タスク" not in stdout, (
            "- [ ] 行が 0 件のとき ## 残タスク セクションは出力されるべきでない"
            "（固定部が巨大でも同じ）。stdout 先頭 500 文字: {!r}".format(stdout[:500])
        )
        assert self.MARKER_STABLE not in stdout, (
            "残タスクが 0 件のときは切り詰めマーカーも出すべきでない（過検知禁止）。"
            f"stdout 先頭 500 文字: {stdout[:500]!r}"
        )


# ---------------------------------------------------------------------------
# 53. main 経由: アプローチセクションの切り詰め通知（DC-GP-005）
# ---------------------------------------------------------------------------


class TestMainApproachTruncationNotice:
    """main: _tail で切り詰めたアプローチ見出しに切り詰め通知サフィックスが付く（DC-GP-005）。

    切り詰めが起きていないセクションにはサフィックスを付けない（過検知しない）。
    """

    def test_success_heading_gets_suffix_and_failure_heading_does_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B-8: ④が 20 行（上限超）なら見出しにサフィックス、⑤が 10 行（上限内）なら付かない。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        successes = [f"成功行{i:02d}" for i in range(1, 21)]  # 20 行（APPROACH_TAIL_LINES 超）
        failures = [f"失敗行{i:02d}" for i in range(1, 11)]  # 10 行（超えない）

        content = (
            "SESSION: 20260730\n"
            "AGENT: \n"
            "DURATION: \n"
            "現在地: \n"
            "\n"
            "## うまくいったアプローチ\n"
            + "\n".join(successes)
            + "\n\n## 試みたが失敗したアプローチ\n"
            + "\n".join(failures)
            + "\n\n## 残タスク\n"
        )
        (sessions_dir / "20260730.tmp").write_text(content, encoding="utf-8")

        module = _load_module(monkeypatch, sessions_dir)
        tail_n = module.APPROACH_TAIL_LINES  # 15 をテストにハードコードしない

        result = _run_main_subprocess(sessions_dir)
        assert result.returncode == 0, (
            f"期待 exit 0、実際 {result.returncode}\nstderr: {result.stderr!r}"
        )
        out_lines = result.stdout.splitlines()

        success_headings = [
            ln for ln in out_lines if ln.startswith("## うまくいったアプローチ")
        ]
        assert len(success_headings) == 1, (
            f"成功アプローチ見出しは 1 行だけ出るべき。実際: {success_headings!r}"
        )
        heading = success_headings[0]
        # 文言全体を逐語 assert せず、安定部分のみ検査する
        assert "（末尾" in heading, (
            f"切り詰めが起きた見出しには切り詰め通知が付くべき。実際: {heading!r}"
        )
        assert f"{tail_n} 行" in heading, (
            f"切り詰め通知には表示行数（APPROACH_TAIL_LINES={tail_n}）が入るべき。実際: {heading!r}"
        )
        assert f"全 {len(successes)} 行）" in heading, (
            f"切り詰め通知には元の総行数（{len(successes)}）が入るべき。実際: {heading!r}"
        )

        failure_headings = [
            ln for ln in out_lines if ln.startswith("## 試みたが失敗したアプローチ")
        ]
        assert len(failure_headings) == 1, (
            f"失敗アプローチ見出しは 1 行だけ出るべき。実際: {failure_headings!r}"
        )
        assert failure_headings[0] == "## 試みたが失敗したアプローチ", (
            "切り詰めが起きていないセクションの見出しにサフィックスを付けてはいけない（過検知禁止）。"
            f"実際: {failure_headings[0]!r}"
        )


# ---------------------------------------------------------------------------
# 54-56. _cap_genba の単体テスト（サイクル 2・SR-NEW）
# ---------------------------------------------------------------------------


class TestCapGenba:
    """_cap_genba(value, path, limit=MAX_GENBA_CHARS) のモジュールレベル単体テスト。

    現在地（genba）値を制限文字数でキャップし、
    キャップした場合のみ fail-loud 表示を付ける（SR-NEW）。
    """

    def _load(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> object:
        import importlib.util

        spec = importlib.util.spec_from_file_location("restore_session", HOOK_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[attr-defined]
        monkeypatch.setattr(module, "SESSIONS_DIR", str(tmp_path))
        return module

    def test_value_within_limit_is_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C-1: len(value) == MAX_GENBA_CHARS（ちょうど）のとき、戻り値が入力と完全一致し、
        切り詰め表示を含まない。判定は > 側に寄せる（ちょうどは収まる側）。
        """
        module = self._load(monkeypatch, tmp_path)
        limit = module.MAX_GENBA_CHARS
        value = "フェーズD 実装中" + "X" * (limit - len("フェーズD 実装中"))
        assert len(value) == limit

        result = module._cap_genba(value, "/path/to/session")
        assert result == value
        assert "…[現在地は全" not in result

    def test_value_one_over_limit_is_capped_with_display(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C-2: len(value) == MAX_GENBA_CHARS + 1 のとき、先頭 MAX_GENBA_CHARS 文字 + 切り詰め表示。"""
        module = self._load(monkeypatch, tmp_path)
        limit = module.MAX_GENBA_CHARS
        value = "フェーズD 実装中" + "X" * (limit - len("フェーズD 実装中") + 1)
        assert len(value) == limit + 1

        result = module._cap_genba(value, "/path/to/session")
        assert "…[現在地は全" in result
        assert len(result) == limit + 1

    def test_short_value_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C-3: 短い値のとき、入力と完全一致・切り詰め表示なし。"""
        module = self._load(monkeypatch, tmp_path)
        value = "フェーズD 実装中"

        result = module._cap_genba(value, "/path/to/session")
        assert result == value
        assert "…[現在地は全" not in result


# ---------------------------------------------------------------------------
# 57-59. main 経由: 現在地キャップとマーカー生存（サイクル 2・SR-NEW）
# ---------------------------------------------------------------------------


class TestMainGenbaCapAndMarker:
    """main: 現在地値をキャップし、マーカーが 10,000 文字境界内に生存することを確認（SR-NEW）。"""

    MARKER_STABLE = "件のうち先頭"
    MARKER_RE = re.compile(r"全 (\d+) 件のうち先頭 (\d+) 件")

    def _write_session_with_genba(
        self, sessions_dir: Path, genba: str, todos: list[str] | None = None,
        date_str: str = "20260730"
    ) -> Path:
        todos = todos or []
        content = (
            f"SESSION: {date_str}\n"
            "AGENT: \n"
            "DURATION: \n"
            f"現在地: {genba}\n"
            "\n"
            "## うまくいったアプローチ\n"
            "\n"
            "## 試みたが失敗したアプローチ\n"
            "\n"
            "## 残タスク\n"
            + ("\n".join(todos) + "\n" if todos else "")
        )
        session_file = sessions_dir / f"{date_str}.tmp"
        session_file.write_text(content, encoding="utf-8")
        return session_file

    def _make_todos(self, count: int, size: int) -> list[str]:
        out = []
        for i in range(count):
            prefix = f"- [ ] T{i:02d} "
            out.append(prefix + "A" * (size - len(prefix)))
        return out

    def test_genba_15000_chars_marker_within_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C-4: 現在地 15,000 文字 + 残タスク数十件のとき、マーカーが 10,000 境界内。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        genba = "フェーズD実装中" + "X" * 14985
        assert len(genba) == 15000

        todos = self._make_todos(20, 2000)
        self._write_session_with_genba(sessions_dir, genba, todos)

        module = _load_module(monkeypatch, sessions_dir)
        max_chars = module.MAX_OUTPUT_CHARS

        result = _run_main_subprocess(sessions_dir)
        assert result.returncode == 0
        stdout = result.stdout

        assert self.MARKER_STABLE in stdout
        marker_idx = stdout.index(self.MARKER_STABLE)
        assert marker_idx < max_chars

    def test_genba_at_limit_not_truncated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C-5: 現在地が MAX_GENBA_CHARS ちょうど + 残タスク数件のとき、値が逐語出力＋切り詰め表示なし。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        module = _load_module(monkeypatch, sessions_dir)
        limit = module.MAX_GENBA_CHARS

        genba = "フェーズD" + "実" * (limit - len("フェーズD"))
        assert len(genba) == limit

        todos = ["- [ ] タスクA", "- [ ] タスクB", "- [ ] タスクC"]
        self._write_session_with_genba(sessions_dir, genba, todos)

        result = _run_main_subprocess(sessions_dir)
        assert result.returncode == 0
        stdout = result.stdout

        assert genba in stdout
        assert "…[現在地は全" not in stdout

    def test_genba_over_limit_shows_truncation_notice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C-6: 現在地が MAX_GENBA_CHARS + 1 + 残タスク数件のとき、切り詰め表示と3つの情報を含む。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        module = _load_module(monkeypatch, sessions_dir)
        limit = module.MAX_GENBA_CHARS

        genba = "フェーズD" + "実" * (limit - len("フェーズD") + 1)
        assert len(genba) == limit + 1

        todos = ["- [ ] タスクA", "- [ ] タスクB"]
        self._write_session_with_genba(sessions_dir, genba, todos)

        result = _run_main_subprocess(sessions_dir)
        assert result.returncode == 0
        stdout = result.stdout

        assert "…[現在地は全" in stdout
        assert f"全 {limit + 1} 文字中" in stdout
        assert f"先頭 {limit} 文字" in stdout

        expected_path = os.path.normpath(
            os.path.join(str(sessions_dir), "20260730.tmp")
        )
        assert expected_path in stdout
