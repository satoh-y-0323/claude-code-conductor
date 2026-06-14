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
