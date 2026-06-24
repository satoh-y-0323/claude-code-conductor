"""tests/skills/test_session_guard.py

session_guard.py（mark / check）の挙動を subprocess で検証する。
旧 Bash ブロック（mkdir+printf / setup 判定 / init 判定）との等価性を固定し、
SKILL.md の置換成立・残骸排除・settings.json の allow 登録を不変条件として保証する。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.skills._skill_helpers import (
    WORKTREE_ROOT,
    read_init_session_skill,
    read_setup_skill,
    read_start_skill,
)

SCRIPT = WORKTREE_ROOT / ".claude" / "skills" / "init-session" / "scripts" / "session_guard.py"
SETTINGS_JSON = WORKTREE_ROOT / ".claude" / "settings.json"


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _run(subcmd: str, *, project_dir: Path, sid: str | None) -> subprocess.CompletedProcess:
    """session_guard.py を subprocess で実行して結果を返す。

    SCRIPT が存在しない場合は FileNotFoundError を raise する
    （pytestmark による全 SKIP を避け、FAILED で Red を表明する）。
    """
    if not SCRIPT.is_file():
        raise FileNotFoundError(f"session_guard.py not found: {SCRIPT}")

    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    if sid is None:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    else:
        env["CLAUDE_CODE_SESSION_ID"] = sid

    return subprocess.run(
        [sys.executable, str(SCRIPT), subcmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(project_dir),
    )


def _run_no_args(project_dir: Path) -> subprocess.CompletedProcess:
    """引数なしで session_guard.py を実行する。"""
    if not SCRIPT.is_file():
        raise FileNotFoundError(f"session_guard.py not found: {SCRIPT}")

    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    env.pop("CLAUDE_CODE_SESSION_ID", None)

    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(project_dir),
    )


def _run_unknown(subcmd: str, project_dir: Path) -> subprocess.CompletedProcess:
    """未知のサブコマンドで session_guard.py を実行する。"""
    if not SCRIPT.is_file():
        raise FileNotFoundError(f"session_guard.py not found: {SCRIPT}")

    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    env.pop("CLAUDE_CODE_SESSION_ID", None)

    return subprocess.run(
        [sys.executable, str(SCRIPT), subcmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(project_dir),
    )


def _make_flag(project_dir: Path, content: str, *, newline: str | None = None) -> Path:
    """init_session.flag を作成して Path を返す。newline 引数は write_text に渡す。"""
    state_dir = project_dir / ".claude" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    flag = state_dir / "init_session.flag"
    if newline is not None:
        flag.write_text(content, encoding="utf-8", newline=newline)
    else:
        flag.write_text(content, encoding="utf-8")
    return flag


# ---------------------------------------------------------------------------
# check サブコマンド（ケース 1〜5b）
# ---------------------------------------------------------------------------

class TestCheck:
    """check サブコマンドが INIT_DONE / INIT_NEEDED を正しく返す。"""

    def test_check_matching_sid_returns_init_done(self, tmp_path: Path) -> None:
        """flag の内容と CLAUDE_CODE_SESSION_ID が一致するとき INIT_DONE を返す。"""
        _make_flag(tmp_path, "S1")
        result = _run("check", project_dir=tmp_path, sid="S1")
        assert result.returncode == 0
        assert result.stdout.strip() == "INIT_DONE"

    def test_check_no_sid_env_returns_init_needed(self, tmp_path: Path) -> None:
        """CLAUDE_CODE_SESSION_ID が未設定（キーなし）のとき INIT_NEEDED を返す。"""
        _make_flag(tmp_path, "S1")
        result = _run("check", project_dir=tmp_path, sid=None)  # pop でキー消去
        assert result.returncode == 0
        assert result.stdout.strip() == "INIT_NEEDED"

    def test_check_no_flag_returns_init_needed(self, tmp_path: Path) -> None:
        """flag が存在しないとき INIT_NEEDED を返す。"""
        result = _run("check", project_dir=tmp_path, sid="S1")
        assert result.returncode == 0
        assert result.stdout.strip() == "INIT_NEEDED"

    def test_check_mismatched_sid_returns_init_needed(self, tmp_path: Path) -> None:
        """flag に S2 が書かれているが sid=S1 のとき INIT_NEEDED を返す。"""
        _make_flag(tmp_path, "S2")
        result = _run("check", project_dir=tmp_path, sid="S1")
        assert result.returncode == 0
        assert result.stdout.strip() == "INIT_NEEDED"

    def test_check_crlf_flag_returns_init_done(self, tmp_path: Path) -> None:
        """flag が CRLF 混入（S1\\r\\n）でも strip 後に一致すれば INIT_DONE を返す。"""
        _make_flag(tmp_path, "S1\r\n", newline="")  # CRLF を意図的に混入
        result = _run("check", project_dir=tmp_path, sid="S1")
        assert result.returncode == 0
        assert result.stdout.strip() == "INIT_DONE"

    def test_check_empty_sid_returns_init_needed(self, tmp_path: Path) -> None:
        """CLAUDE_CODE_SESSION_ID が空文字のとき INIT_NEEDED を返す（ケース 5b）。"""
        _make_flag(tmp_path, "S1")
        result = _run("check", project_dir=tmp_path, sid="")
        assert result.returncode == 0
        assert result.stdout.strip() == "INIT_NEEDED"


# ---------------------------------------------------------------------------
# mark サブコマンド（ケース 6〜11）
# ---------------------------------------------------------------------------

class TestMark:
    """mark サブコマンドが flag を書き込み SETUP_DONE / SETUP_NEEDED を返す。"""

    def test_mark_setup_done_when_coding_standards_present(self, tmp_path: Path) -> None:
        """.claude/rules/coding-standards.md が存在するとき SETUP_DONE を返す。"""
        rules_dir = tmp_path / ".claude" / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)
        (rules_dir / "coding-standards.md").write_text("", encoding="utf-8")

        result = _run("mark", project_dir=tmp_path, sid="anysid")
        assert result.returncode == 0
        assert result.stdout.strip() == "SETUP_DONE"

    def test_mark_setup_done_when_setup_done_flag_present(self, tmp_path: Path) -> None:
        """.claude/state/setup_done.flag が存在するとき SETUP_DONE を返す。"""
        state_dir = tmp_path / ".claude" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "setup_done.flag").write_text("", encoding="utf-8")

        result = _run("mark", project_dir=tmp_path, sid="anysid")
        assert result.returncode == 0
        assert result.stdout.strip() == "SETUP_DONE"

    def test_mark_setup_needed_when_no_markers(self, tmp_path: Path) -> None:
        """セットアップマーカーが両方とも存在しないとき SETUP_NEEDED を返す。"""
        result = _run("mark", project_dir=tmp_path, sid="anysid")
        assert result.returncode == 0
        assert result.stdout.strip() == "SETUP_NEEDED"

    def test_mark_writes_sid_to_flag(self, tmp_path: Path) -> None:
        """mark が CLAUDE_CODE_SESSION_ID を init_session.flag に書き込む副作用を検証する。"""
        result = _run("mark", project_dir=tmp_path, sid="S9")
        assert result.returncode == 0

        flag = tmp_path / ".claude" / "state" / "init_session.flag"
        assert flag.is_file(), "init_session.flag が作成されていない"
        assert flag.read_text(encoding="utf-8") == "S9"

    def test_mark_then_check_roundtrip(self, tmp_path: Path) -> None:
        """mark 後に check すると INIT_DONE になる（ループ回避ロジックの核）。"""
        mark_result = _run("mark", project_dir=tmp_path, sid="S10")
        assert mark_result.returncode == 0

        check_result = _run("check", project_dir=tmp_path, sid="S10")
        assert check_result.returncode == 0
        assert check_result.stdout.strip() == "INIT_DONE"

    def test_mark_creates_state_dir_if_absent(self, tmp_path: Path) -> None:
        """state ディレクトリが存在しなくても makedirs により mark が成功する。"""
        state_dir = tmp_path / ".claude" / "state"
        assert not state_dir.exists(), "前提: state ディレクトリは存在しない"

        result = _run("mark", project_dir=tmp_path, sid="anysid")
        assert result.returncode == 0
        assert state_dir.is_dir(), "state ディレクトリが作成されていない"


# ---------------------------------------------------------------------------
# CLI 引数エラー（ケース 12・13）
# ---------------------------------------------------------------------------

class TestCliErrors:
    """不正な引数に対して exit 2 と usage を返す。"""

    def test_no_args_exits_2_with_usage_in_stderr(self, tmp_path: Path) -> None:
        """引数なしで実行すると stdout 空・stderr に usage・exit 2 を返す。"""
        result = _run_no_args(tmp_path)
        assert result.returncode == 2
        assert result.stdout.strip() == ""
        assert "usage" in result.stderr.lower()

    def test_unknown_subcommand_exits_2_with_message(self, tmp_path: Path) -> None:
        """未知のサブコマンド 'foo' で実行すると stderr に unknown subcommand・exit 2 を返す。"""
        result = _run_unknown("foo", tmp_path)
        assert result.returncode == 2
        assert "unknown subcommand" in result.stderr


# ---------------------------------------------------------------------------
# 不変条件テスト: SKILL.md の置換成立・残骸排除（§4-4）
# ---------------------------------------------------------------------------

class TestInvariants:
    """SKILL.md への session_guard.py 移行と settings.json 登録の不変条件を保証する。"""

    def test_init_session_skill_has_no_tr_d_remnant(self) -> None:
        """init-session/SKILL.md に旧 bash の tr -d 残骸がないことを確認する。"""
        content = read_init_session_skill()
        assert "tr -d" not in content

    def test_init_session_skill_has_no_printf_s_remnant(self) -> None:
        """init-session/SKILL.md に旧 bash の printf '%s' 残骸がないことを確認する。"""
        content = read_init_session_skill()
        assert "printf '%s'" not in content

    def test_init_session_skill_has_no_mkdir_p_state_remnant(self) -> None:
        """init-session/SKILL.md に旧 bash の mkdir -p .claude/state 残骸がないことを確認する。"""
        content = read_init_session_skill()
        assert "mkdir -p .claude/state" not in content

    def test_start_skill_has_no_tr_d_remnant(self) -> None:
        """start/SKILL.md に旧 bash の tr -d 残骸がないことを確認する。"""
        content = read_start_skill()
        assert "tr -d" not in content

    def test_start_skill_has_no_printf_s_remnant(self) -> None:
        """start/SKILL.md に旧 bash の printf '%s' 残骸がないことを確認する。"""
        content = read_start_skill()
        assert "printf '%s'" not in content

    def test_start_skill_has_no_mkdir_p_state_remnant(self) -> None:
        """start/SKILL.md に旧 bash の mkdir -p .claude/state 残骸がないことを確認する。"""
        content = read_start_skill()
        assert "mkdir -p .claude/state" not in content

    def test_init_session_skill_has_session_guard_mark(self) -> None:
        """init-session/SKILL.md に 'session_guard.py mark' が存在することを確認する。"""
        content = read_init_session_skill()
        assert "session_guard.py mark" in content

    def test_start_skill_has_session_guard_check(self) -> None:
        """start/SKILL.md に 'session_guard.py check' が存在することを確認する。"""
        content = read_start_skill()
        assert "session_guard.py check" in content

    def test_settings_json_has_session_guard_allow(self) -> None:
        """settings.json の permissions.allow に session_guard.py* エントリが存在することを確認する。"""
        data = json.loads(SETTINGS_JSON.read_text(encoding="utf-8"))
        allow_entries = data.get("permissions", {}).get("allow", [])
        assert any(
            "session_guard.py" in entry for entry in allow_entries
        ), "settings.json の permissions.allow に session_guard.py* エントリが見つからない"

    # ------------------------------------------------------------------
    # CR L-03: 完全パス呼び出しアサーション（対称性向上）
    # ------------------------------------------------------------------

    def test_init_session_skill_has_full_mark_invocation(self) -> None:
        """init-session/SKILL.md に 'scripts/session_guard.py mark' が存在することを確認する（CR L-03）。"""
        content = read_init_session_skill()
        assert "scripts/session_guard.py mark" in content

    def test_start_skill_has_full_check_invocation(self) -> None:
        """start/SKILL.md に 'scripts/session_guard.py check' が存在することを確認する（CR L-03）。"""
        content = read_start_skill()
        assert "scripts/session_guard.py check" in content

    # ------------------------------------------------------------------
    # CR M-01 / L-04: setup/SKILL.md の委譲成立・残骸排除不変条件
    # ------------------------------------------------------------------

    def test_setup_skill_has_no_bash_flag_creation_remnant(self) -> None:
        """setup/SKILL.md に旧複合 bash 残骸（`mkdir -p .claude/state && : >`）がないことを確認する（CR M-01 / L-04）。

        impl-fixes-session-guard が /setup Phase 4 の bash ブロックを
        session_guard.py setup-mark に委譲した後にのみ Pass する（現状 Red が正常）。
        """
        content = read_setup_skill()
        assert "mkdir -p .claude/state && : >" not in content, (
            "setup/SKILL.md に旧複合 bash 残骸 'mkdir -p .claude/state && : >' が残っている"
        )

    def test_setup_skill_has_no_colon_redirect_remnant(self) -> None:
        """: > .claude/state/setup_done.flag 形式の残骸がないことを確認する（CR M-01 / L-04）。"""
        content = read_setup_skill()
        assert ": > .claude/state/setup_done.flag" not in content, (
            "setup/SKILL.md に旧 bash 残骸 ': > .claude/state/setup_done.flag' が残っている"
        )

    def test_setup_skill_has_setup_mark_invocation(self) -> None:
        """setup/SKILL.md に 'session_guard.py setup-mark' が存在することを確認する（CR M-01 / L-04）。"""
        content = read_setup_skill()
        assert "session_guard.py setup-mark" in content, (
            "setup/SKILL.md に 'session_guard.py setup-mark' が見つからない"
        )


# ---------------------------------------------------------------------------
# CR M-02: mark 空 sid 書き込み副作用
# ---------------------------------------------------------------------------

class TestMarkEmptySid:
    """mark が空 sid でも 0 バイト flag を書くことを固定する（CR M-02）。"""

    def test_mark_empty_sid_writes_empty_flag(self, tmp_path: Path) -> None:
        """sid="" のとき init_session.flag が空ファイル（0 バイト）で作成されることを確認する（CR M-02）。

        旧 bash の `printf '%s' "" > flag`（0 バイトファイル）との等価性を固定する意図。
        空文字 sid を渡しても flag 自体は作成され、内容は空文字（"" / 0 バイト）になる。
        """
        result = _run("mark", project_dir=tmp_path, sid="")
        assert result.returncode == 0
        flag = tmp_path / ".claude" / "state" / "init_session.flag"
        assert flag.is_file(), "sid='' のとき init_session.flag が作成されていない"
        assert flag.read_text(encoding="utf-8") == "", (
            "sid='' のとき flag の内容が空文字でない（旧 printf '%s' \"\" > flag との等価性が崩れている）"
        )


# ---------------------------------------------------------------------------
# CR M-01 / L-04: setup-mark サブコマンド挙動固定
# ---------------------------------------------------------------------------

class TestSetupMark:
    """setup-mark サブコマンドが setup_done.flag を書くことを固定する（CR M-01 / L-04）。

    impl-fixes-session-guard が session_guard.py に setup-mark を追加した後にのみ
    全テストが Pass する（現状 Red が正常）。
    """

    def test_setup_mark_creates_setup_done_flag(self, tmp_path: Path) -> None:
        """setup-mark が .claude/state/setup_done.flag を作成することを確認する（CR M-01）。"""
        result = _run("setup-mark", project_dir=tmp_path, sid="anysid")
        assert result.returncode == 0
        flag = tmp_path / ".claude" / "state" / "setup_done.flag"
        assert flag.is_file(), "setup-mark 後に setup_done.flag が存在しない"

    def test_setup_mark_creates_state_dir_if_absent(self, tmp_path: Path) -> None:
        """state ディレクトリが未存在でも setup-mark が makedirs(exist_ok=True) で成功することを確認する（CR M-01）。"""
        state_dir = tmp_path / ".claude" / "state"
        assert not state_dir.exists(), "前提: state ディレクトリは存在しない"

        result = _run("setup-mark", project_dir=tmp_path, sid="anysid")
        assert result.returncode == 0
        assert state_dir.is_dir(), "setup-mark が state ディレクトリを作成していない"

    def test_setup_mark_idempotent(self, tmp_path: Path) -> None:
        """setup-mark を 2 回呼んでも returncode==0（既存 flag 上書きで失敗しない）ことを確認する（CR M-01）。"""
        result1 = _run("setup-mark", project_dir=tmp_path, sid="anysid")
        assert result1.returncode == 0

        result2 = _run("setup-mark", project_dir=tmp_path, sid="anysid")
        assert result2.returncode == 0, "2 回目の setup-mark が失敗している（idempotent でない）"

    def test_setup_mark_no_stdout_decision(self, tmp_path: Path) -> None:
        """setup-mark が SETUP_DONE/SETUP_NEEDED/INIT_* 等の判定文字列を stdout に出さないことを確認する（CR L-04）。

        /setup Phase 4 はフラグを書くだけで判定出力は不要。stdout が空であることを保証する。
        """
        result = _run("setup-mark", project_dir=tmp_path, sid="anysid")
        assert result.returncode == 0
        assert result.stdout.strip() == "", (
            "setup-mark が判定文字列を stdout に出している（出さない設計が崩れている）"
        )


# ---------------------------------------------------------------------------
# SR M-1: _project_root() の resolve 整合テスト
# ---------------------------------------------------------------------------

def _run_with_raw_root(
    subcmd: str,
    *,
    raw_project_dir: str,
    sid: str | None,
) -> subprocess.CompletedProcess:
    """CLAUDE_PROJECT_DIR に任意の生文字列（'..' 入り等）を直接渡して session_guard.py を実行する。

    既存 _run は project_dir: Path を str(project_dir) で正規化して渡すため、
    '..' 成分を含む raw 値を渡せない。本ヘルパーは raw 文字列をそのまま環境変数に設定する。
    SR M-1（resolve 整合）テスト専用。既存 _run は壊さない。
    """
    if not SCRIPT.is_file():
        raise FileNotFoundError(f"session_guard.py not found: {SCRIPT}")

    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = raw_project_dir
    if sid is None:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    else:
        env["CLAUDE_CODE_SESSION_ID"] = sid

    return subprocess.run(
        [sys.executable, str(SCRIPT), subcmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


class TestProjectRootResolve:
    """_project_root() の resolve() 整合テスト（SR M-1）。

    CLAUDE_PROJECT_DIR に '..' 成分を含む値を渡しても意図ディレクトリ配下に
    flag が書かれることを固定する（パストラバーサル防御の退行検知）。

    注意: Windows では os.makedirs / os.path.join が '..' を自動正規化するため、
    subprocess テストでは resolve() の有無による差異が出ない。
    本テストは「意図したディレクトリに flag が書かれること」を保証するものであり、
    実装前後ともに Pass する（後方互換テスト）。
    resolve() の実装整合は impl-fixes-session-guard のコードレビューで確認する。
    """

    def test_project_root_resolves_dotdot_in_path(self, tmp_path: Path) -> None:
        """CLAUDE_PROJECT_DIR が '..' 成分を含んでいても正規化されることを確認する（SR M-1）。

        tmp_path/sub/../target のような '..' 入りパスを渡し、
        flag が (tmp_path/target)/.claude/state/init_session.flag に書かれることをアサートする。

        Windows では os.makedirs が '..' を含むパスを正規化するため、
        resolve() の有無にかかわらず Pass する（後方互換）。
        """
        target = tmp_path / "target"
        target.mkdir()
        sub = tmp_path / "sub"
        sub.mkdir()

        # '..' を含む raw パス: tmp_path/sub/../target → 正規化すると tmp_path/target
        raw_dir = str(sub / ".." / "target")

        result = _run_with_raw_root("mark", raw_project_dir=raw_dir, sid="resolve-test")
        assert result.returncode == 0

        # 意図したディレクトリ (target/) に flag が書かれることを確認する
        expected_flag = target / ".claude" / "state" / "init_session.flag"
        assert expected_flag.is_file(), (
            f"flag が意図したディレクトリ {expected_flag} に書かれていない"
        )


# ---------------------------------------------------------------------------
# CR-M-001: 定数単一真実源固定テスト
# ---------------------------------------------------------------------------

# subprocess ではなく import レベルで session_guard.py を読み込むためのパス別名。
# 既存の SCRIPT 定数（subprocess 用）とは別名にして既存テストを壊さない。
_SCRIPT_PATH = WORKTREE_ROOT / ".claude" / "skills" / "init-session" / "scripts" / "session_guard.py"


def _load_session_guard_module():
    """session_guard.py をモジュールとしてロードする（定数の値検査用）。

    既存テストは subprocess 経由で import 経路が無いため、本ヘルパーで import 経路を新設する。
    session_guard.py は冒頭で stdout/stderr.reconfigure を呼ぶが、
    import 時に print 副作用は無い（定数定義のみ評価）ので import は安全。
    """
    if not _SCRIPT_PATH.is_file():
        raise FileNotFoundError(f"session_guard.py not found: {_SCRIPT_PATH}")
    spec = importlib.util.spec_from_file_location("session_guard_under_test", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _get_setup_markers_second_element_ast_type() -> str:
    """AST 解析で SETUP_MARKERS_REL の 2 要素目のノード種別を返す。

    単一真実源化（SETUP_DONE_FLAG_REL 参照化）後: ast.Name
    別リテラル二重定義のまま（現状）: ast.Tuple

    この違いを利用して「参照に置き換えられているか」を検査する。

    Note: CPython はリテラルの等値タプルを同一オブジェクトとしてインターニングする場合があるため、
    is 比較だけでは二重定義か参照化かを確実に区別できない。AST 解析が唯一の確実な方法。
    """
    if not _SCRIPT_PATH.is_file():
        raise FileNotFoundError(f"session_guard.py not found: {_SCRIPT_PATH}")
    src = _SCRIPT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "SETUP_MARKERS_REL":
                    if isinstance(node.value, ast.Tuple) and len(node.value.elts) >= 2:
                        return type(node.value.elts[1]).__name__
    raise AssertionError("SETUP_MARKERS_REL の AST ノードが見つからない")


class TestConstantSingleSource:
    """CR-M-001: SETUP_MARKERS_REL[1] と SETUP_DONE_FLAG_REL の単一真実源を固定する。

    impl は SETUP_DONE_FLAG_REL を SETUP_MARKERS_REL より前に定義し、
    SETUP_MARKERS_REL の 2 要素目を SETUP_DONE_FLAG_REL の参照に置き換える前提。
    テスト自体は定義順に依存せず、AST 解析と最終値のみを検査する。

    Note: CPython はリテラルの等値タプルをインターニングするため is 比較では
    別リテラル二重定義と参照化を確実に区別できない。
    test_setup_markers_second_element_is_setup_done_flag_object は AST ベースで
    SETUP_MARKERS_REL[1] のノードが ast.Name（名前参照）であることを検査する。
    """

    def test_setup_markers_second_element_is_setup_done_flag_object(self) -> None:
        """SETUP_MARKERS_REL[1] が SETUP_DONE_FLAG_REL への名前参照（ast.Name）であること。

        単一真実源化されていれば AST ノード種別が 'Name'（変数参照）になる。
        別リテラルの二重定義のままなら 'Tuple'（直接リテラル）のままで Red。

        CPython のタプルインターニングにより is 比較は信頼できないため AST 解析を採用する。
        """
        node_type = _get_setup_markers_second_element_ast_type()
        assert node_type == "Name", (
            f"SETUP_MARKERS_REL[1] の AST ノード種別が 'Name' でなく '{node_type}'。"
            "SETUP_DONE_FLAG_REL への参照に置き換えられていない（別リテラル二重定義のまま）。"
        )

    def test_setup_done_flag_rel_value_unchanged(self) -> None:
        """挙動完全不変の保証: SETUP_DONE_FLAG_REL の値が (".claude","state","setup_done.flag") のままであること。"""
        mod = _load_session_guard_module()
        assert mod.SETUP_DONE_FLAG_REL == (".claude", "state", "setup_done.flag")

    def test_setup_markers_value_unchanged(self) -> None:
        """挙動完全不変の保証: SETUP_MARKERS_REL の値（順序・要素）が変わっていないこと。

        単一真実源化は参照に置き換えるだけで値は不変であるべき。
        """
        mod = _load_session_guard_module()
        assert mod.SETUP_MARKERS_REL == (
            (".claude", "rules", "coding-standards.md"),
            (".claude", "state", "setup_done.flag"),
        )
