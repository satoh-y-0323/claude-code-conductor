"""FR-4: ``c3 init`` のテンプレート展開経路（``cli_init._copytree``）のリンク防御.

## 対象

``src/c3/cli_init.py`` の ``_copytree(src, dst, *, root=None)``。
``c3 init`` が bundled template を利用先へ展開する唯一の経路であり、
``entry.is_dir()`` / ``entry.is_file()`` / ``shutil.copy2`` はいずれも
**リンクを暗黙に辿る（dereference する）**。

## 群の分類（この 1 ファイルに A 群と B 群が同居する）

- **A 群 = Red 群**: 是正前の ``_copytree`` で **赤になるべき** 性質。
  リンク entry をスキップし、スキップ 1 件につき stderr に 1 行警告する契約。
- **B 群 = 回帰ガード群**: 是正前から **緑であるべき** 性質。
  リンク防御を入れた結果として「全件スキップ」「exit code 変化」「件数表示のずれ」
  といった縮退を起こしていないことを検知する対照。
  **B 群は最初から Pass することが正しい。** Red 化する方向へ書き換えてはならない
  （書き換えると全件スキップ縮退が正の仕様として凍結される）。

各テストの docstring 冒頭に ``[A群]`` / ``[B群]`` を明示する。

## 警告の契約（A 群が要求するもの）

スキップした entry **1 件につき stderr 1 行**。その行は

1. スキップした entry を**パスで識別できる**（少なくとも entry 名を含む）
2. **理由がリンク由来であると分かる**（``symlink`` / ``junction`` / ``link`` /
   ``reparse`` のいずれかの語を含む・大小文字不問）

を満たす。文面そのものは実装の自由とし、上記 2 性質のみを検査する。

## プラットフォーム差（本ファイル作成時に実測・2026-08-15 / Windows 11）

- junction は ``Path.is_symlink()`` が **False**・``Path.is_dir()`` が **True**
  → 素朴な ``is_symlink()`` ガードでは弾けない
- 非特権 Windows では ``os.symlink`` が **WinError 1314** (privilege not held)
  で失敗する → symlink 系ケースは ``skipif win32``
- 存在しないターゲットへの junction は ``FileNotFoundError`` で作れない
  → dangling リンクのケースは POSIX 限定

したがって **Windows のローカル実行で Red を実測できるのは junction ケースのみ**であり、
symlink 系ケースは skip される。これは想定内であり、初回の実行は次回 push 時の
3 OS CI（ubuntu / macos）になる。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import pytest

from c3 import cli_init

# リンク作成ヘルパーは tests/conftest.py に集約（tests/test_stop_agent_memory_warn.py と共有）。
# tests/__init__.py が実在するパッケージ構成のため絶対 import で参照する。
from tests.conftest import _make_link

# ---------------------------------------------------------------------------
# skip 条件（理由文字列は test-report の申告と対応させる）
# ---------------------------------------------------------------------------

_WIN32 = sys.platform == "win32"

_SKIP_ON_WIN32 = pytest.mark.skipif(
    _WIN32,
    reason=(
        "非特権 Windows では os.symlink が WinError 1314 で失敗するため symlink を作れない"
        "（実測 2026-08-15）。このケースの初回実行は 3 OS CI の ubuntu / macos になる。"
    ),
)

_WIN32_ONLY = pytest.mark.skipif(
    not _WIN32,
    reason="NTFS ジャンクション固有の挙動（is_symlink() が False）を検査するため win32 限定",
)

# 警告行が「リンク由来である」と読み取れることの判定に使う語彙。
# 文面は実装の自由とし、リンク性に言及していることだけを要求する。
_LINK_REASON_RE = re.compile(r"symlink|junction|link|reparse", re.IGNORECASE)


# ---------------------------------------------------------------------------
# 共有ヘルパー
# ---------------------------------------------------------------------------


def _rel_files(root: Path) -> set[str]:
    """``root`` 配下に実際に出力された「中身のある entry」の相対パス集合.

    dangling リンクは ``is_file()`` が False のため入らない。
    「リンクそのものが出力されていないこと」の判定には使わず、
    そちらは ``os.path.lexists`` で行う（本モジュール内の各テスト参照）。
    """
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _warn_lines(stderr: str) -> list[str]:
    """stderr を空行を除いた行リストにする（警告の「1 件 1 行」を数えるため）."""
    return [line for line in stderr.splitlines() if line.strip()]


def _assert_single_skip_warning(stderr: str, entry_name: str) -> None:
    """スキップ 1 件につき stderr 1 行・パスと理由を含む、という契約を検査する."""
    lines = _warn_lines(stderr)
    assert len(lines) == 1, (
        f"スキップ 1 件に対し stderr は 1 行であるべきだが {len(lines)} 行だった: {lines!r}"
    )
    line = lines[0]
    assert entry_name in line, (
        f"警告行がスキップした entry をパスで識別できていない（{entry_name!r} を含まない）: {line!r}"
    )
    assert _LINK_REASON_RE.search(line), (
        "警告行がリンク由来の理由に言及していない"
        f"（symlink/junction/link/reparse のいずれも含まない）: {line!r}"
    )


def _build_plain_tree(src: Path) -> None:
    """リンクを含まない標準的なテンプレート相当ツリーを作る（B 群の基準形）.

    ``c3._excludes.should_skip`` の EXCLUDE / KEEP の両方と、
    「全部スキップされた結果 空になったディレクトリを畳む」挙動を通る形にする。
    """
    src.mkdir(parents=True, exist_ok=True)
    (src / "top.md").write_text("top", encoding="utf-8")

    (src / "agents").mkdir()
    (src / "agents" / "architect.md").write_text("framework", encoding="utf-8")
    (src / "agents" / "developer.md").write_text("framework", encoding="utf-8")

    (src / "docs").mkdir()
    (src / "docs" / "guide.md").write_text("doc", encoding="utf-8")

    (src / "reports").mkdir()
    (src / "reports" / ".gitkeep").write_text("", encoding="utf-8")  # KEEP
    (src / "reports" / "plan-report-x.md").write_text("plan", encoding="utf-8")  # EXCLUDE

    (src / "memory").mkdir()
    (src / "memory" / ".gitkeep").write_text("", encoding="utf-8")  # KEEP
    (src / "memory" / "sessions").mkdir()
    # EXCLUDE のみのディレクトリ → 空になって畳まれる
    (src / "memory" / "sessions" / "20260427.tmp").write_text("s", encoding="utf-8")


# ``_build_plain_tree`` の出力として期待する集合（B 群の golden）。
_PLAIN_TREE_EXPECTED: frozenset[str] = frozenset(
    {
        "top.md",
        "agents/architect.md",
        "agents/developer.md",
        "docs/guide.md",
        "reports/.gitkeep",
        "memory/.gitkeep",
    }
)


def _rmtree_iterative(root: Path) -> None:
    """再帰を使わずにツリーを消す（自己参照ループで深く掘られた残骸の後始末用）.

    ``shutil.rmtree`` は再帰実装のため、数百段のネストで RecursionError に
    なりうる。``os.walk(topdown=False)`` は反復実装なので深さに耐える。
    リンクは ``os.walk`` の既定（followlinks=False）で辿らない。
    """
    if not os.path.lexists(root):
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            try:
                os.remove(os.path.join(dirpath, name))
            except OSError:
                pass
        for name in dirnames:
            path = os.path.join(dirpath, name)
            try:
                if os.path.islink(path):
                    os.remove(path)
                else:
                    os.rmdir(path)
            except OSError:
                pass
    try:
        os.rmdir(root)
    except OSError:
        pass


# ===========================================================================
# A 群 = Red 群（是正前の _copytree で赤になるべきもの）
# ===========================================================================


class TestLinkEntriesAreSkippedWithWarning:
    """[A群] リンク entry はコピー先に出力されず、1 件につき stderr 1 行の警告が出る.

    是正前の期待値（＝赤の正体）:

    - ファイルへの symlink: ``entry.is_file()`` が True → ``shutil.copy2`` が
      既定で follow_symlinks=True のためリンク先の**中身が実ファイルとして複製**される
    - ディレクトリへの symlink / junction: ``entry.is_dir()`` が True → 配下を**再帰コピー**する
    - いずれの場合も警告は一切出ない（沈黙）
    """

    @_SKIP_ON_WIN32
    def test_file_symlink_entry_is_not_copied_and_warns(self, tmp_path, capsys):
        """[A群] ファイルへの symlink が dereference 展開されないこと."""
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.md"
        secret.write_text("SECRET-PAYLOAD", encoding="utf-8")

        src = tmp_path / "src"
        src.mkdir()
        (src / "normal.md").write_text("normal", encoding="utf-8")
        _make_link(secret, src / "linked.md")

        dst = tmp_path / "dst"
        cli_init._copytree(src, dst)

        assert not os.path.lexists(dst / "linked.md"), (
            "symlink entry がコピー先に出力されている"
            f"（中身: {(dst / 'linked.md').read_text(encoding='utf-8') if (dst / 'linked.md').is_file() else '<not a file>'!r}）"
        )
        # 全件スキップ縮退の対照: 隣の通常ファイルは必ずコピーされる
        assert (dst / "normal.md").is_file(), "リンクでない隣接ファイルまでスキップしている"

        _assert_single_skip_warning(capsys.readouterr().err, "linked.md")

    @_SKIP_ON_WIN32
    def test_dir_symlink_entry_is_not_copied_and_warns(self, tmp_path, capsys):
        """[A群] ディレクトリへの symlink 配下が再帰コピーされないこと."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("SECRET-PAYLOAD", encoding="utf-8")

        src = tmp_path / "src"
        src.mkdir()
        (src / "normal.md").write_text("normal", encoding="utf-8")
        _make_link(outside, src / "linked_dir")

        dst = tmp_path / "dst"
        copied = cli_init._copytree(src, dst)

        assert not os.path.lexists(dst / "linked_dir"), (
            f"dir symlink entry がコピー先に出力されている: {_rel_files(dst)!r}"
        )
        assert "linked_dir/secret.md" not in _rel_files(dst), "リンク先の中身が展開されている"
        assert (dst / "normal.md").is_file(), "リンクでない隣接ファイルまでスキップしている"
        assert copied == 1, f"コピー件数はリンクを除いた 1 件であるべき: {copied}"

        _assert_single_skip_warning(capsys.readouterr().err, "linked_dir")

    @_WIN32_ONLY
    def test_junction_entry_is_not_copied_and_warns(self, tmp_path, capsys):
        """[A群] NTFS ジャンクション配下が再帰コピーされないこと.

        junction は ``Path.is_symlink()`` が False を返すため、
        ``is_symlink()`` だけのガードでは弾けない（下の 1 行 assert が対照）。
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("SECRET-PAYLOAD", encoding="utf-8")

        src = tmp_path / "src"
        src.mkdir()
        (src / "normal.md").write_text("normal", encoding="utf-8")
        junction = src / "linked_dir"
        _make_link(outside, junction)
        assert not junction.is_symlink()

        dst = tmp_path / "dst"
        copied = cli_init._copytree(src, dst)

        assert not os.path.lexists(dst / "linked_dir"), (
            f"junction entry がコピー先に出力されている: {_rel_files(dst)!r}"
        )
        assert "linked_dir/secret.md" not in _rel_files(dst), "junction 先の中身が展開されている"
        assert (dst / "normal.md").is_file(), "リンクでない隣接ファイルまでスキップしている"
        assert copied == 1, f"コピー件数はリンクを除いた 1 件であるべき: {copied}"

        _assert_single_skip_warning(capsys.readouterr().err, "linked_dir")


class TestSelfReferencingLoopDoesNotRecurse:
    """[A群] 自己参照ループを構成するリンクで無限再帰しない."""

    @_SKIP_ON_WIN32
    def test_self_referencing_symlink_does_not_recurse(self, tmp_path, capsys):
        """[A群] ``loop/self -> loop`` で RecursionError / OSError を起こさないこと.

        是正前の期待値（＝赤の正体）: ``entry.is_dir()`` がリンクを辿るため
        ``loop/self/self/self/...`` を掘り続け、再帰上限（RecursionError）か
        パス長上限（OSError ENAMETOOLONG）のどちらか先に当たった方で異常終了する。

        後始末: 是正前は数百段のディレクトリを実際に掘るため、``shutil.rmtree``
        （再帰実装）では消せないことがある。反復版で必ず片付ける。
        """
        src = tmp_path / "src"
        src.mkdir()
        (src / "normal.md").write_text("normal", encoding="utf-8")
        loop_dir = src / "loop"
        loop_dir.mkdir()
        _make_link(loop_dir, loop_dir / "self")

        # リンクが実際にループを構成している（辿れば 2 段目に到達できる）
        assert (loop_dir / "self" / "self").is_dir()

        dst = tmp_path / "dst"
        try:
            try:
                cli_init._copytree(src, dst)
            except RecursionError as exc:
                pytest.fail(f"自己参照リンクを辿って無限再帰した: {exc!r}")
            except OSError as exc:
                pytest.fail(f"自己参照リンクを辿ってパス長・IO 上限に達した: {exc!r}")

            assert not os.path.lexists(dst / "loop" / "self"), (
                "自己参照リンク entry がコピー先に出力されている"
            )
            assert (dst / "normal.md").is_file(), "リンクでない隣接ファイルまでスキップしている"

            _assert_single_skip_warning(capsys.readouterr().err, "self")
        finally:
            _rmtree_iterative(dst)


class TestDanglingLinkWarns:
    """[A群] 壊れた（dangling）リンクが無音で消えない."""

    @_SKIP_ON_WIN32
    def test_dangling_symlink_emits_warning(self, tmp_path, capsys):
        """[A群] リンク先が存在しない symlink でも stderr 警告が出ること.

        是正前の期待値（＝赤の正体）: dangling symlink は ``is_dir()`` も
        ``is_file()`` も False のため、``_copytree`` のどの分岐にも入らず
        **完全に沈黙してスキップ**される（＝スキップした事実が利用者に届かない）。

        Windows で実行しない理由: 存在しないターゲットへの junction は
        ``FileNotFoundError`` で作成自体ができない（実測 2026-08-15）。
        """
        src = tmp_path / "src"
        src.mkdir()
        (src / "normal.md").write_text("normal", encoding="utf-8")
        _make_link(tmp_path / "no_such_target", src / "broken")

        assert os.path.lexists(src / "broken"), "dangling リンクが作れていない"
        assert not (src / "broken").exists(), "ターゲットが存在してしまっている"

        dst = tmp_path / "dst"
        cli_init._copytree(src, dst)

        _assert_single_skip_warning(capsys.readouterr().err, "broken")


# ===========================================================================
# B 群 = 回帰ガード群（是正前から緑・アサーションを反転させてはならない）
# ===========================================================================


class TestPlainTreeOutputIsUnchanged:
    """[B群] リンクを含まない正常系で出力集合が不変."""

    def test_plain_tree_output_set_matches_golden(self, tmp_path, capsys):
        """[B群] EXCLUDE / KEEP / 空ディレクトリ畳みを含む出力集合が golden と一致する.

        リンク防御の導入で「リンクでないものまでスキップする」縮退が起きたら、
        ここが集合差として落ちる。
        """
        src = tmp_path / "src"
        _build_plain_tree(src)

        dst = tmp_path / "dst"
        copied = cli_init._copytree(src, dst)

        assert _rel_files(dst) == set(_PLAIN_TREE_EXPECTED)
        assert copied == len(_PLAIN_TREE_EXPECTED), (
            f"戻り値のコピー件数が出力ファイル数と一致しない: {copied}"
        )
        # 中身が全部 EXCLUDE だったディレクトリは畳まれる（従来挙動）
        assert not (dst / "memory" / "sessions").exists()
        # リンクが 1 つも無いので警告も出ない
        assert _warn_lines(capsys.readouterr().err) == []


class TestSourceRootReachedViaLinkIsUnchanged:
    """[B群] コピー元ルート自体をリンク経由のパスで渡しても出力集合が不変.

    ``root`` 側の realpath 忘れによる**全件スキップ縮退**を検知する対照。
    「entry の実パスがコピー元ルート配下か」で判定する実装は、ルート自身が
    リンク経由のパスだと全 entry が「配下でない」と誤判定され、出力が空になる。

    junction 版（win32）と symlink 版（非 win32）に分け、
    **どのプラットフォームでも必ず 1 本が実行される**ようにしている。
    """

    @staticmethod
    def _assert_invariant(tmp_path: Path, link_root: Path, capsys) -> None:
        real_src = tmp_path / "real_src"
        _build_plain_tree(real_src)

        dst_direct = tmp_path / "dst_direct"
        copied_direct = cli_init._copytree(real_src, dst_direct)
        direct_files = _rel_files(dst_direct)
        capsys.readouterr()  # 直接経路の出力は比較対象外

        _make_link(real_src, link_root)
        if sys.platform == "win32":
            assert not link_root.is_symlink()

        dst_via_link = tmp_path / "dst_via_link"
        copied_via_link = cli_init._copytree(link_root, dst_via_link)
        via_link_files = _rel_files(dst_via_link)

        assert direct_files == set(_PLAIN_TREE_EXPECTED), "直接経路の基準がそもそも崩れている"
        assert via_link_files == direct_files, (
            "コピー元ルートをリンク経由で渡すと出力集合が変わる"
            f"（全件スキップ縮退の疑い）: via_link={sorted(via_link_files)!r}"
        )
        assert via_link_files, "リンク経由のコピー結果が空（全件スキップ縮退）"
        assert copied_via_link == copied_direct, (
            f"コピー件数が経路で変わる: direct={copied_direct} via_link={copied_via_link}"
        )
        # ルートがリンクでも、配下にリンクが無い以上スキップ警告は出ない
        assert _warn_lines(capsys.readouterr().err) == []

    @_WIN32_ONLY
    def test_root_via_junction_output_is_unchanged(self, tmp_path, capsys):
        """[B群] コピー元ルートを junction 経由で渡しても出力集合が不変."""
        self._assert_invariant(tmp_path, tmp_path / "link_src", capsys)

    @_SKIP_ON_WIN32
    def test_root_via_symlink_output_is_unchanged(self, tmp_path, capsys):
        """[B群] コピー元ルートを symlink 経由で渡しても出力集合が不変."""
        self._assert_invariant(tmp_path, tmp_path / "link_src", capsys)


class TestInitExitCodeAndPrintedCount:
    """[B群] ``c3 init`` の exit code と stdout の件数表示."""

    def test_exit_code_zero_and_printed_count_matches_actual_files(
        self, tmp_path, monkeypatch, capsys
    ):
        """[B群] exit code は 0 のまま・stdout の件数は実際にコピーした数と一致する.

        テンプレートにリンクを 1 つ含める（win32 は junction / POSIX は symlink）。
        リンクを辿る現行実装でも、リンクをスキップする是正後でも、
        「表示件数 == コピー先に実在する通常ファイル数」は不変でなければならない。
        リンク防御をエラー終了（exit != 0）や件数の二重計上で実装したら落ちる。
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "linked_child.md").write_text("child", encoding="utf-8")

        template = tmp_path / "template"
        template.mkdir()
        (template / "top.md").write_text("top", encoding="utf-8")
        (template / "agents").mkdir()
        (template / "agents" / "architect.md").write_text("framework", encoding="utf-8")
        _make_link(outside, template / "linked_dir")

        monkeypatch.setattr("c3.cli_init.templates_dir", lambda: template)

        target = tmp_path / "target"
        rc = cli_init.handle(
            argparse.Namespace(
                target=target, force=False, platform="claude", git=False, no_git=True
            )
        )
        assert rc == 0, "リンクを含むテンプレートで exit code が 0 でなくなった"

        out = capsys.readouterr().out
        match = re.search(r"initialized .* \((\d+) files copied\)", out)
        assert match, f"stdout に件数表示が見つからない: {out!r}"
        printed = int(match.group(1))

        dest = target / ".claude"
        actual = len(_rel_files(dest))
        assert printed == actual, (
            f"stdout の件数表示 {printed} が実際の出力ファイル数 {actual} と一致しない: "
            f"{sorted(_rel_files(dest))!r}"
        )
        # 縮退ガード: 通常ファイルは必ず出ている
        assert (dest / "top.md").is_file()
        assert (dest / "agents" / "architect.md").is_file()


class TestDanglingLinkIsNotCopied:
    """[B群] dangling リンクがコピー先に出力されないこと（警告の有無は A 群で扱う）."""

    @_SKIP_ON_WIN32
    def test_dangling_symlink_is_not_output(self, tmp_path, capsys):
        """[B群] 是正前から緑（``is_dir()``/``is_file()`` 双方 False で分岐に入らない）.

        是正でこの entry を「とりあえずコピーする」方向へ倒したら落ちる。

        Windows で実行しない理由: 存在しないターゲットへの junction は
        ``FileNotFoundError`` で作成自体ができない（実測 2026-08-15）。
        """
        src = tmp_path / "src"
        src.mkdir()
        (src / "normal.md").write_text("normal", encoding="utf-8")
        _make_link(tmp_path / "no_such_target", src / "broken")

        dst = tmp_path / "dst"
        copied = cli_init._copytree(src, dst)
        capsys.readouterr()  # 警告の有無は A 群の担当

        assert not os.path.lexists(dst / "broken"), "dangling リンクがコピー先に出力されている"
        assert (dst / "normal.md").is_file(), "リンクでない隣接ファイルまでスキップしている"
        assert copied == 1, f"コピー件数は通常ファイルの 1 件であるべき: {copied}"
