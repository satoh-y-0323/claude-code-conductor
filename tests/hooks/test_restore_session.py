"""Characterization tests for .claude/hooks/restore_session.py

既存実装の挙動を固定するテスト群（後付け characterization test）。

テストケース:
1. find_latest_session: ディレクトリ不在 → None
2. find_latest_session: 空ディレクトリ → None
3. find_latest_session: 複数 .tmp ファイル → 名前昇順最大のフルパス
4. find_latest_session: .tmp 以外のファイルは無視
5. extract_section: 該当見出しなし → 空文字
6. extract_section: 次の ## で終わる → 中身を strip して返す
7. extract_section: 次の <!-- で終わる → 中身を strip して返す
8. extract_section: 見出しが末尾 → 末尾まで strip して返す
9. main 経由 (subprocess): セッションファイル無し → 何も出力せず exit 0
10. main 経由: 全セクション空のセッションファイル → 何も出力せず exit 0
11. main 経由: 残タスクのみあり → ヘッダ + 残タスクが stdout に出る
12. main 経由: 全セクション埋まり → ヘッダ + 3 セクション全部が stdout に出る
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
        """セッションファイルが存在するが全セクションが空のとき stdout は空で exit code = 0。"""
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


# ---------------------------------------------------------------------------
# 11. main 経由: 残タスクのみあり → ヘッダ + 残タスクが stdout に出る
# ---------------------------------------------------------------------------


class TestMainOutputTodosOnly:
    """main: 残タスクのみ埋まっているとき、ヘッダと残タスクが stdout に出る。"""

    def test_outputs_header_and_todos_only(self, tmp_path: Path) -> None:
        """残タスクセクションのみ存在するとき、ヘッダと残タスクが出力される。"""
        _, sessions_dir = _setup_tmp_structure(tmp_path)
        date_str = "20260507"
        session_file = sessions_dir / f"{date_str}.tmp"
        session_file.write_text(
            "## 残タスク\nタスクA\nタスクB\n\n## うまくいったアプローチ\n\n## 試みたが失敗したアプローチ\n",
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
        assert "タスクA" in output, f"残タスク内容 'タスクA' が stdout に含まれない。stdout: {output!r}"

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
            "## 残タスク\nタスクX\n\n## うまくいったアプローチ\n成功例Y\n\n## 試みたが失敗したアプローチ\n失敗例Z\n",
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
        assert "タスクX" in output, f"'タスクX' が stdout に含まれない。stdout: {output!r}"

        assert "## うまくいったアプローチ" in output, (
            f"## うまくいったアプローチ が stdout に含まれない。stdout: {output!r}"
        )
        assert "成功例Y" in output, f"'成功例Y' が stdout に含まれない。stdout: {output!r}"

        assert "## 試みたが失敗したアプローチ" in output, (
            f"## 試みたが失敗したアプローチ が stdout に含まれない。stdout: {output!r}"
        )
        assert "失敗例Z" in output, f"'失敗例Z' が stdout に含まれない。stdout: {output!r}"
