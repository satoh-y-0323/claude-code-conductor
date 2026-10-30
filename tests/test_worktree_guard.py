"""Tests for .claude/hooks/worktree_guard.py

## 作動条件（hook 入力の agent_type）

ガードの作動条件は hook 入力 JSON の `agent_type` である。旧作動条件の環境変数
`PO_WORKTREE_GUARD` は hook のプロセスに届く経路が無く、ガードは一度も作動して
いなかったため置き換えた（要件で承認された振る舞いの変更）。

「cwd」はすべて hook プロセスの cwd（＝subprocess の cwd）を指す。hook 入力
JSON の `cwd` フィールドは判定に使わない。

1. agent_type が wt_* の 3 種 + cwd が `.claude/worktrees/<名前>` 配下 +
   Write/Edit の対象が worktree ルートの外 → exit 2・`[WorktreeGuard BLOCK]`
2. 同 agent で cwd がルートのサブディレクトリでも、対象が同じルート配下なら exit 0
3. agent_type が無い／3 種以外 → exit 0・stderr 空
4. 対象 agent なのに cwd から worktree ルートを導けない → exit 2（fail-closed）
5. 入れ子は最も内側の `.claude/worktrees/<名前>` をルートとする
6. C3_WORKTREE_GUARD_DISABLE=1 なら exit 0
7. PO_WORKTREE_GUARD は結果に影響しない
8. 入力 JSON の cwd は判定に使わない
9. 定数 WT_AGENT_TYPES が `.claude/agents/wt_*.md` の `name:` 集合と一致する
10. BLOCK メッセージのサニタイズ（ANSI / RLO）
11. decide_containment の二層 AND 判定・fail-closed・NUL（下記参照）

ブロック期待と許可期待の両方を置き、常に許可／常にブロックのどちらの手抜き実装
でも赤になるようにしている。(7) 以外のケースでは環境変数 PO_WORKTREE_GUARD を
立てない（`_run_guard` の既定は立てない）。

## 11. 二層 AND 設計（decide_containment）

許可には次の両方の合格が必要（どちらか一方でも不合格なら block）:

    第 1 層: 正規化文字列（realpath 後に NFC 正規化）の prefix 判定
    第 2 層: ファイルシステム実体による許可範囲配下確認
             （対象の実在する最深祖先から根方向へ祖先鎖を辿り、いずれかの祖先
               ——最深祖先自身を含む——が許可範囲と samefile なら配下。対象自身が
               許可範囲と同一の場合も許可）

判定は純関数 `worktree_guard.decide_containment()` に集約し、実体同一性判定
（および実在判定）を引数注入することで単体テスト可能にする。戻り値は
**ブロック判定**（True = block / False = allow）。

hook モジュールの読み込みは**テスト関数内**で行う（モジュールレベル import に
すると、subprocess 方式のテストまで collection error に巻き込まれる）。

ソースに NFD リテラルを直書きせず `unicodedata.normalize()` で実行時に生成し、
アサーションメッセージ中のパスは `ascii()` で退避する（cp932 環境でのエンコード
不能を避けるため）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent

# Absolute path to the hook under test
_HOOK = _REPO_ROOT / ".claude" / "hooks" / "worktree_guard.py"

# Absolute path to the template copy of the hook
_TEMPLATE_HOOK = (
    _REPO_ROOT / "src" / "c3" / "_template" / ".claude" / "hooks" / "worktree_guard.py"
)

_AGENTS_DIR = _REPO_ROOT / ".claude" / "agents"

# ガード対象の agent_type（(9) で .claude/agents/wt_*.md と機械照合する）
_WT_AGENTS = ("wt_developer", "wt_tester", "wt_systematic-debugger")
_WT_AGENT = "wt_developer"

# ガード対象外の agent_type（None = キー自体を入れない）
_NON_WT_AGENTS = (None, "", "developer", "tester", "wt_unknown", "general-purpose")

_BLOCK_MARK = "[WorktreeGuard BLOCK]"


def _make_worktree(base: Path, name: str = "agent-test") -> Path:
    """`base/.claude/worktrees/<name>/` を作って返す（worktree ルート）."""
    worktree = base / ".claude" / "worktrees" / name
    worktree.mkdir(parents=True, exist_ok=True)
    return worktree


def _payload(
    file_path: str,
    *,
    agent_type: str | None = _WT_AGENT,
    tool_name: str = "Write",
    cwd_field: str | None = None,
) -> dict:
    """hook 入力 JSON を組み立てる（agent_type=None ならキーを入れない）."""
    payload: dict = {"tool_name": tool_name, "tool_input": {"file_path": file_path}}
    if tool_name == "Edit":
        payload["tool_input"].update({"old_string": "a", "new_string": "b"})
    if agent_type is not None:
        payload["agent_type"] = agent_type
    if cwd_field is not None:
        payload["cwd"] = cwd_field
    return payload


def _run_guard(
    payload: dict,
    *,
    cwd: str | None = None,
    hook: Path = _HOOK,
    extra_env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    """Run worktree_guard.py as a subprocess, feeding *payload* via stdin.

    環境は SYSTEMROOT / PATH のみを引き継ぐ最小構成（PO_WORKTREE_GUARD /
    C3_WORKTREE_GUARD_DISABLE は既定で立てない）。追加の環境変数は *extra_env*
    で明示する。

    `input_text` を渡すと *payload* の代わりにその文字列をそのまま stdin へ送る
    （非 ASCII を `\\uXXXX` エスケープせず UTF-8 のまま送りたいケース用）。
    """
    env: dict[str, str] = {}
    # Windows では subprocess に SYSTEMROOT を継承させないと sys.executable 起動が失敗する
    for key in ("SYSTEMROOT", "PATH"):
        if key in os.environ:
            env[key] = os.environ[key]
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(hook)],
        input=input_text if input_text is not None else json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=cwd,
    )


def _assert_blocked(result: subprocess.CompletedProcess, context: str) -> None:
    assert result.returncode == 2, (
        f"[{context}] block(exit 2) が期待値だが exit={result.returncode}\n"
        f"stderr: {ascii(result.stderr)}"
    )
    assert _BLOCK_MARK in result.stderr, (
        f"[{context}] stderr に {_BLOCK_MARK} が無い: {ascii(result.stderr)}"
    )
    assert "Traceback" not in result.stderr, (
        f"[{context}] 未捕捉例外が漏れた: {ascii(result.stderr)}"
    )


def _assert_allowed(result: subprocess.CompletedProcess, context: str, *, silent: bool = False) -> None:
    assert result.returncode == 0, (
        f"[{context}] 許可(exit 0) が期待値だが exit={result.returncode}\n"
        f"stderr: {ascii(result.stderr)}"
    )
    if silent:
        assert not result.stderr.strip(), (
            f"[{context}] stderr は空が期待値: {ascii(result.stderr)}"
        )


# ---------------------------------------------------------------------------
# (1) 対象 agent + worktree 配下の cwd + ルート外への書き込み → block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent_type", _WT_AGENTS)
@pytest.mark.parametrize("tool_name", ["Write", "Edit"])
def test_wt_agent_write_outside_worktree_is_blocked(
    tmp_path: Path, agent_type: str, tool_name: str
) -> None:
    """(1) wt_* agent が worktree ルート外へ Write/Edit → exit 2 + BLOCK."""
    worktree = _make_worktree(tmp_path)
    outside = tmp_path / "outside_file.txt"
    result = _run_guard(
        _payload(str(outside), agent_type=agent_type, tool_name=tool_name),
        cwd=str(worktree),
    )
    _assert_blocked(result, f"(1) {agent_type}/{tool_name}")


def test_wt_agent_write_absolute_path_to_main_repo_is_blocked(tmp_path: Path) -> None:
    """(1) worktree の外（main repo 相当）への絶対パス書き込みは block."""
    worktree = _make_worktree(tmp_path)
    outside_abs = tmp_path / "main_repo_path" / "src" / "file.py"
    result = _run_guard(_payload(str(outside_abs)), cwd=str(worktree))
    _assert_blocked(result, "(1) main repo absolute path")


def test_wt_agent_relative_path_escaping_worktree_is_blocked(tmp_path: Path) -> None:
    """(1) 相対パスで worktree ルートの外へ出る書き込みは block."""
    worktree = _make_worktree(tmp_path)
    result = _run_guard(_payload("../../../escape.txt"), cwd=str(worktree))
    _assert_blocked(result, "(1) relative escape")


def test_wt_agent_write_to_sibling_worktree_is_blocked(tmp_path: Path) -> None:
    """(1) 隣の worktree（同じ `.claude/worktrees` 配下・別名）への書き込みは block.

    ルートは `.claude/worktrees/<名前>` であり `.claude/worktrees` ではない。
    """
    worktree = _make_worktree(tmp_path, "agent-a")
    sibling = _make_worktree(tmp_path, "agent-b")
    result = _run_guard(_payload(str(sibling / "file.txt")), cwd=str(worktree))
    _assert_blocked(result, "(1) sibling worktree")


# ---------------------------------------------------------------------------
# (2) 対象 agent + worktree ルート配下への書き込み → allow（cwd がサブディレクトリでも）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent_type", _WT_AGENTS)
def test_wt_agent_write_inside_worktree_is_allowed(tmp_path: Path, agent_type: str) -> None:
    """(2) cwd = ルート、対象 = ルート配下 → exit 0."""
    worktree = _make_worktree(tmp_path)
    result = _run_guard(
        _payload(str(worktree / "subdir" / "file.txt"), agent_type=agent_type),
        cwd=str(worktree),
    )
    _assert_allowed(result, f"(2) {agent_type} inside root")


def test_wt_agent_relative_path_inside_worktree_is_allowed(tmp_path: Path) -> None:
    """(2) 相対パスでもルート配下に解決されれば exit 0."""
    worktree = _make_worktree(tmp_path)
    result = _run_guard(_payload("subdir/file.txt"), cwd=str(worktree))
    _assert_allowed(result, "(2) relative inside root")


@pytest.mark.parametrize("tool_name", ["Write", "Edit"])
def test_wt_agent_cwd_in_subdir_write_elsewhere_in_root_is_allowed(
    tmp_path: Path, tool_name: str
) -> None:
    """(2) cwd がルートのサブディレクトリ、対象が同じルートの別サブディレクトリ → exit 0.

    許可範囲は cwd ではなく worktree ルートである。
    """
    worktree = _make_worktree(tmp_path)
    cwd = worktree / "src" / "pkg"
    cwd.mkdir(parents=True)
    other = worktree / "tests"
    other.mkdir()
    result = _run_guard(
        _payload(str(other / "test_x.py"), tool_name=tool_name), cwd=str(cwd)
    )
    _assert_allowed(result, f"(2) cwd subdir -> root sibling ({tool_name})")


def test_wt_agent_cwd_in_subdir_relative_to_root_is_allowed(tmp_path: Path) -> None:
    """(2) cwd がサブディレクトリで、`..` でルート直下へ戻る相対パス → exit 0."""
    worktree = _make_worktree(tmp_path)
    cwd = worktree / "src"
    cwd.mkdir()
    result = _run_guard(_payload("../README.md"), cwd=str(cwd))
    _assert_allowed(result, "(2) cwd subdir relative ..")


def test_wt_agent_cwd_in_subdir_write_outside_root_is_blocked(tmp_path: Path) -> None:
    """(1)(2) の境界: cwd がサブディレクトリでも、ルートの外への書き込みは block."""
    worktree = _make_worktree(tmp_path)
    cwd = worktree / "src"
    cwd.mkdir()
    result = _run_guard(_payload(str(tmp_path / "outside.txt")), cwd=str(cwd))
    _assert_blocked(result, "(1)(2) cwd subdir -> outside root")


def test_wt_agent_write_into_existing_subdirectory_is_allowed(tmp_path: Path) -> None:
    """(2)・回帰: 実在するサブディレクトリ配下への書き込みは許可される.

    第 2 層による過剰ブロックの検出（「最深祖先＝ルート自身」以外の経路）。
    """
    worktree = _make_worktree(tmp_path)
    sub = worktree / "sub"
    sub.mkdir()
    result = _run_guard(_payload(str(sub / "file.txt")), cwd=str(worktree))
    _assert_allowed(result, "(2) existing subdir")


# ---------------------------------------------------------------------------
# (3) agent_type が無い／対象外 → 常に no-op
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent_type", _NON_WT_AGENTS)
def test_non_wt_agent_is_not_guarded_even_inside_worktree(
    tmp_path: Path, agent_type: str | None
) -> None:
    """(3) cwd が worktree 配下・対象がルート外でも、対象外 agent なら exit 0・stderr 空."""
    worktree = _make_worktree(tmp_path)
    result = _run_guard(
        _payload(str(tmp_path / "outside_file.txt"), agent_type=agent_type),
        cwd=str(worktree),
    )
    _assert_allowed(result, f"(3) agent_type={agent_type!r}", silent=True)


def test_non_wt_agent_outside_worktrees_is_not_guarded(tmp_path: Path) -> None:
    """(3) main セッション（agent_type 無し・cwd が worktree 外）→ exit 0・stderr 空."""
    result = _run_guard(
        _payload("/some/outside/path/file.txt", agent_type=None), cwd=str(tmp_path)
    )
    _assert_allowed(result, "(3) main session", silent=True)


def test_template_hook_non_wt_agent_is_not_guarded(tmp_path: Path) -> None:
    """(3) Template copy も同じ挙動（配布実体が生成済みの環境のみ）."""
    if not _TEMPLATE_HOOK.exists():
        pytest.skip(f"Template hook not found at {_TEMPLATE_HOOK}; skipping.")
    result = _run_guard(
        _payload("/some/outside/path/file.txt", agent_type=None),
        cwd=str(tmp_path),
        hook=_TEMPLATE_HOOK,
    )
    _assert_allowed(result, "(3) template", silent=True)


# ---------------------------------------------------------------------------
# (4) 対象 agent なのに cwd から worktree ルートを導けない → block（fail-closed）
# ---------------------------------------------------------------------------


def test_wt_agent_cwd_without_worktrees_is_blocked(tmp_path: Path) -> None:
    """(4) cwd に `.claude/worktrees` が無い → 対象が cwd 配下でも exit 2."""
    result = _run_guard(_payload(str(tmp_path / "file.txt")), cwd=str(tmp_path))
    _assert_blocked(result, "(4) no .claude/worktrees")


def test_wt_agent_cwd_with_worktrees_not_under_dot_claude_is_blocked(tmp_path: Path) -> None:
    """(4) `worktrees` の直前が `.claude` でない → ルートを導けず exit 2."""
    cwd = tmp_path / "other" / "worktrees" / "agent-x"
    cwd.mkdir(parents=True)
    result = _run_guard(_payload(str(cwd / "file.txt")), cwd=str(cwd))
    _assert_blocked(result, "(4) worktrees not under .claude")


def test_wt_agent_cwd_is_worktrees_dir_itself_is_blocked(tmp_path: Path) -> None:
    """(4) cwd が `.claude/worktrees` 自身（直後に名前の要素が無い）→ exit 2."""
    cwd = tmp_path / ".claude" / "worktrees"
    cwd.mkdir(parents=True)
    result = _run_guard(_payload(str(cwd / "file.txt")), cwd=str(cwd))
    _assert_blocked(result, "(4) cwd == .claude/worktrees")


# ---------------------------------------------------------------------------
# (5) 入れ子は最も内側の `.claude/worktrees/<名前>` をルートとする
# ---------------------------------------------------------------------------


def _make_nested(tmp_path: Path) -> tuple[Path, Path]:
    outer = _make_worktree(tmp_path, "outer")
    inner = _make_worktree(outer, "inner")
    return outer, inner


def test_nested_worktree_write_into_outer_is_blocked(tmp_path: Path) -> None:
    """(5) 内側 worktree の cwd から外側 worktree 内（内側の外）への書き込み → exit 2."""
    outer, inner = _make_nested(tmp_path)
    cwd = inner / "sub"
    cwd.mkdir()
    result = _run_guard(_payload(str(outer / "file.txt")), cwd=str(cwd))
    _assert_blocked(result, "(5) nested -> outer")


def test_nested_worktree_write_into_inner_is_allowed(tmp_path: Path) -> None:
    """(5) 内側 worktree のサブディレクトリ cwd から内側ルート配下への書き込み → exit 0."""
    _outer, inner = _make_nested(tmp_path)
    cwd = inner / "sub"
    cwd.mkdir()
    result = _run_guard(_payload(str(inner / "other" / "file.txt")), cwd=str(cwd))
    _assert_allowed(result, "(5) nested -> inner")


# ---------------------------------------------------------------------------
# (6) C3_WORKTREE_GUARD_DISABLE=1 → 無効化
# ---------------------------------------------------------------------------


def test_disable_env_turns_guard_off(tmp_path: Path) -> None:
    """(6) (1) の入力でも C3_WORKTREE_GUARD_DISABLE=1 なら exit 0."""
    worktree = _make_worktree(tmp_path)
    result = _run_guard(
        _payload(str(tmp_path / "outside_file.txt")),
        cwd=str(worktree),
        extra_env={"C3_WORKTREE_GUARD_DISABLE": "1"},
    )
    _assert_allowed(result, "(6) disable env")


# ---------------------------------------------------------------------------
# (7) PO_WORKTREE_GUARD は結果に影響しない
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("po_env", [None, "1"], ids=["po_unset", "po_set"])
def test_po_env_does_not_affect_wt_agent_block(tmp_path: Path, po_env: str | None) -> None:
    """(7) (1) の入力は PO_WORKTREE_GUARD の有無にかかわらず exit 2."""
    worktree = _make_worktree(tmp_path)
    extra = {"PO_WORKTREE_GUARD": po_env} if po_env is not None else None
    result = _run_guard(
        _payload(str(tmp_path / "outside_file.txt")), cwd=str(worktree), extra_env=extra
    )
    _assert_blocked(result, f"(7) PO_WORKTREE_GUARD={po_env!r}")


@pytest.mark.parametrize("po_env", [None, "1"], ids=["po_unset", "po_set"])
def test_po_env_does_not_affect_non_wt_agent_allow(tmp_path: Path, po_env: str | None) -> None:
    """(7) (3) の入力は PO_WORKTREE_GUARD の有無にかかわらず exit 0・stderr 空."""
    worktree = _make_worktree(tmp_path)
    extra = {"PO_WORKTREE_GUARD": po_env} if po_env is not None else None
    result = _run_guard(
        _payload(str(tmp_path / "outside_file.txt"), agent_type=None),
        cwd=str(worktree),
        extra_env=extra,
    )
    _assert_allowed(result, f"(7) PO_WORKTREE_GUARD={po_env!r}", silent=True)


# ---------------------------------------------------------------------------
# (8) 入力 JSON の cwd は判定に使わない
# ---------------------------------------------------------------------------


def test_payload_cwd_field_is_ignored_for_block(tmp_path: Path) -> None:
    """(8) 入力 JSON の cwd を worktree の外にしても、プロセス cwd 基準で (1) と同じ block."""
    worktree = _make_worktree(tmp_path)
    fake_cwd = tmp_path / "fake_outside"
    fake_cwd.mkdir()
    result = _run_guard(
        _payload(str(fake_cwd / "file.txt"), cwd_field=str(fake_cwd)), cwd=str(worktree)
    )
    _assert_blocked(result, "(8) payload cwd ignored (block)")


def test_payload_cwd_field_is_ignored_for_allow(tmp_path: Path) -> None:
    """(8) 入力 JSON の cwd を worktree の外にしても、プロセス cwd 基準で (2) と同じ allow."""
    worktree = _make_worktree(tmp_path)
    fake_cwd = tmp_path / "fake_outside"
    fake_cwd.mkdir()
    result = _run_guard(
        _payload(str(worktree / "file.txt"), cwd_field=str(fake_cwd)), cwd=str(worktree)
    )
    _assert_allowed(result, "(8) payload cwd ignored (allow)")


# ---------------------------------------------------------------------------
# (9) WT_AGENT_TYPES と .claude/agents/wt_*.md の name: 集合の一致
# ---------------------------------------------------------------------------


def _frontmatter_name(md: Path) -> str | None:
    text = md.read_text(encoding="utf-8")
    m = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
    if m is None:
        return None
    for line in m.group(1).splitlines():
        nm = re.match(r"^name:\s*(.+?)\s*$", line)
        if nm:
            return nm.group(1).strip("'\"")
    return None


def test_wt_agent_types_matches_agent_definitions() -> None:
    """(9) WT_AGENT_TYPES（frozenset[str]）が wt_*.md の name: 集合と一致する."""
    md_files = sorted(_AGENTS_DIR.glob("wt_*.md"))
    assert md_files, f"wt_*.md が見つからない: {_AGENTS_DIR}"
    names = {_frontmatter_name(p) for p in md_files}
    assert None not in names, "frontmatter の name: を読めない wt_*.md がある"

    module = _load_guard_module()
    wt_types = getattr(module, "WT_AGENT_TYPES", None)
    assert wt_types is not None, (
        "[Red] worktree_guard.WT_AGENT_TYPES が未定義（agent_type による作動条件が未実装）"
    )
    assert isinstance(wt_types, frozenset), (
        f"WT_AGENT_TYPES は frozenset であるべきだが {type(wt_types)!r}"
    )
    assert all(isinstance(x, str) for x in wt_types)
    assert set(wt_types) == names, (
        f"WT_AGENT_TYPES={sorted(wt_types)} と agents 定義 {sorted(names)} が不一致"
    )


# ---------------------------------------------------------------------------
# (10) BLOCK メッセージのサニタイズ
# ---------------------------------------------------------------------------


def test_block_message_sanitizes_ansi_escapes(tmp_path: Path) -> None:
    """file_path に ANSI escape が含まれていても stderr にそのまま出力されない."""
    worktree = _make_worktree(tmp_path)
    ansi_injected_path = str(tmp_path / "outside\x1b[31mINJECTED\x1b[0m.txt")
    result = _run_guard(_payload(ansi_injected_path), cwd=str(worktree))
    _assert_blocked(result, "ANSI")
    assert "\x1b" not in result.stderr, (
        "[sec-Low] Block message must not contain raw ANSI escape sequences. "
        f"stderr preview: {ascii(result.stderr[:300])}"
    )


# U+202E (RIGHT-TO-LEFT OVERRIDE)。ソースへ生の制御文字・エスケープ表記を書かず
# chr() で生成する（表示上は不可視のため、リテラル直書きは差分レビューで追えない）。
_RLO = chr(0x202E)


def test_block_message_sanitizes_bidi_override(tmp_path: Path) -> None:
    """[CR-M-001/SR-NEW-1] file_path の U+202E が BLOCK メッセージに生のまま出ない.

    RLO はターミナル上で以降の文字列を右から左へ描画させ、実際の書き込み先を
    偽装できるため除去対象に含める。
    """
    worktree = _make_worktree(tmp_path)
    injected = str(tmp_path / f"outside{_RLO}INJECTED.txt")
    result = _run_guard(_payload(injected), cwd=str(worktree))
    _assert_blocked(result, "RLO")
    assert _RLO not in result.stderr, (
        "BLOCK メッセージに生の U+202E (RIGHT-TO-LEFT OVERRIDE) が残っている: "
        f"{ascii(result.stderr)}"
    )
    # 可読部分は残り、どのパスが拒否されたか分かること（過剰除去の検出）
    assert "outside" in result.stderr and "INJECTED" in result.stderr, (
        f"対象パスの可読部分が消えている: {ascii(result.stderr)}"
    )


# ===========================================================================
# (11) NFC 正規化 / 二層 AND 判定（decide_containment）
# ===========================================================================

# NFC と NFD で表現が異なる文字（ダ = U+30C0 / U+30BF + U+3099）。
# NFD リテラルをソースへ直書きせず実行時に生成する（モジュール docstring 参照）。
_NFC_MARK = unicodedata.normalize("NFC", "ダ")
_NFD_MARK = unicodedata.normalize("NFD", _NFC_MARK)


def _load_guard_module():
    """worktree_guard.py をテスト関数内でモジュールとして読み込む。

    モジュールレベル import にしないこと（subprocess 方式テストを collection
    error に巻き込まないため）。`_hook_utils` を import するため hooks/ を
    一時的に sys.path へ載せる。
    """
    import importlib.util

    hooks_dir = str(_HOOK.parent)
    added = hooks_dir not in sys.path
    if added:
        sys.path.insert(0, hooks_dir)
    try:
        spec = importlib.util.spec_from_file_location(
            "worktree_guard_under_test", _HOOK
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if added and hooks_dir in sys.path:
            sys.path.remove(hooks_dir)
    return module


def _require_decide_containment():
    """`decide_containment` を取り出す。"""
    module = _load_guard_module()
    decide = getattr(module, "decide_containment", None)
    assert decide is not None, (
        "worktree_guard.decide_containment がモジュールレベルに公開されていない"
    )
    return decide


class _NormalizationInsensitiveFs:
    """正規化非感受 FS（macOS APFS/HFS+ 相当）を模したスタブ。

    NFC 正規化後の文字列が一致するパスを「同一実体」とみなす。
    `decide_containment` へ注入して、実 FS の挙動に依存せず表現差の扱いを検証する。
    """

    def __init__(self, existing: list[str]) -> None:
        self._existing = {self._key(p) for p in existing}

    @staticmethod
    def _key(path: str) -> str:
        return unicodedata.normalize("NFC", str(path))

    def exists(self, path: str) -> bool:
        return self._key(path) in self._existing

    def same_entity(self, a: str, b: str) -> bool:
        return self.exists(a) and self.exists(b) and self._key(a) == self._key(b)


def _worktree_paths(tmp_path: Path, mark: str) -> str:
    """`<tmp>/.claude/worktrees/agent-<mark>` の文字列パスを組み立てる（作成はしない）。"""
    return str(tmp_path / ".claude" / "worktrees" / f"agent-{mark}")


def _ancestors_of(path: str) -> list[str]:
    """`path` とその全祖先の文字列パス一覧（実在集合の組み立て用）。"""
    p = Path(path)
    return [str(p), *[str(parent) for parent in p.parents]]


# ---------------------------------------------------------------------------
# 11-1. 関数レベル
# ---------------------------------------------------------------------------


def test_decide_containment_allows_representation_difference_of_same_entity(
    tmp_path: Path,
) -> None:
    """表現差のみ・実体同一なら許可される（block=False）。"""
    assert _NFC_MARK != _NFD_MARK, (
        "テスト前提が崩れている: NFC と NFD で表現が異なる文字を使うこと "
        f"(NFC={ascii(_NFC_MARK)}, NFD={ascii(_NFD_MARK)})"
    )
    decide = _require_decide_containment()
    cwd = _worktree_paths(tmp_path, _NFC_MARK)
    nfd_worktree = _worktree_paths(tmp_path, _NFD_MARK)
    resolved = str(Path(nfd_worktree) / "sub" / "file.txt")
    fs = _NormalizationInsensitiveFs(_ancestors_of(cwd))

    blocked = decide(
        resolved, cwd, same_entity=fs.same_entity, path_exists=fs.exists
    )
    assert blocked is False, (
        "表現差のみで実体が同一なら許可されるべき（block=False）だが "
        f"{blocked!r} が返った (resolved={ascii(resolved)}, cwd={ascii(cwd)})"
    )


def test_decide_containment_blocks_when_same_entity_raises_oserror(
    tmp_path: Path,
) -> None:
    """実体同一性判定が OSError を送出したら block（fail-closed）。"""
    decide = _require_decide_containment()
    cwd = _worktree_paths(tmp_path, "test")
    resolved = str(Path(cwd) / "sub" / "file.txt")

    def _raises(_a: str, _b: str) -> bool:
        raise OSError("simulated filesystem failure")

    blocked = decide(
        resolved, cwd, same_entity=_raises, path_exists=lambda _p: True
    )
    assert blocked is True, (
        f"実体判定が OSError のときは block されるべきだが {blocked!r} が返った"
    )


def test_decide_containment_allows_target_identical_to_cwd(tmp_path: Path) -> None:
    """対象自身が許可範囲と同一なら許可される。"""
    decide = _require_decide_containment()
    cwd = _worktree_paths(tmp_path, "test")
    fs = _NormalizationInsensitiveFs(_ancestors_of(cwd))

    blocked = decide(cwd, cwd, same_entity=fs.same_entity, path_exists=fs.exists)
    assert blocked is False, (
        f"対象が許可範囲自身なら許可されるべきだが {blocked!r} が返った"
    )


def test_decide_containment_blocks_when_first_layer_fails(tmp_path: Path) -> None:
    """第 1 層（正規化文字列 prefix）不合格なら第 2 層合格でも block。"""
    decide = _require_decide_containment()
    cwd = _worktree_paths(tmp_path, "test")
    resolved = str(tmp_path / "outside_file.txt")

    blocked = decide(
        resolved, cwd, same_entity=lambda _a, _b: True, path_exists=lambda _p: True
    )
    assert blocked is True, (
        "第 1 層不合格（worktree 外の文字列パス）は第 2 層が合格しても block される"
        f"べきだが {blocked!r} が返った (resolved={ascii(resolved)})"
    )


def test_decide_containment_blocks_when_second_layer_fails(tmp_path: Path) -> None:
    """第 2 層（FS 実体による配下確認）不合格なら block。"""
    decide = _require_decide_containment()
    cwd = _worktree_paths(tmp_path, "test")
    resolved = str(Path(cwd) / "sub" / "file.txt")

    blocked = decide(
        resolved, cwd, same_entity=lambda _a, _b: False, path_exists=lambda _p: True
    )
    assert blocked is True, (
        "第 2 層不合格（実体が配下でない）なら block されるべきだが "
        f"{blocked!r} が返った"
    )


# ---------------------------------------------------------------------------
# 11-2. プロセスレベル（対象 agent の入力で判定経路に到達させる）
# ---------------------------------------------------------------------------


def test_nul_byte_in_file_path_is_blocked(tmp_path: Path) -> None:
    """file_path に NUL を含む payload は block(exit 2) される（fail-closed）。"""
    worktree = _make_worktree(tmp_path)
    result = _run_guard(_payload(str(worktree / "pa\x00th.txt")), cwd=str(worktree))
    assert result.returncode == 2, (
        f"NUL 混入パスは block(exit 2) されるべきだが exit={result.returncode}\n"
        f"stderr: {ascii(result.stderr)}"
    )
    assert "Traceback" not in result.stderr, (
        f"NUL 混入パスで未捕捉例外が漏れた: {ascii(result.stderr)}"
    )


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason=(
        "正規化非感受 FS（macOS APFS/HFS+）でのみ NFD/NFC 表現差が同一実体になる。"
        "Windows / Linux では NFD 名は別実体（存在しない）ため第 2 層が不合格になり "
        "block が期待値（test_nfd_form_path_to_nfc_worktree_stays_blocked_off_darwin）。"
    ),
)
def test_nfd_form_path_to_nfc_worktree_is_allowed_on_darwin(tmp_path: Path) -> None:
    """[darwin 限定] 同一実体を NFD 表現で参照した書き込みは許可される。"""
    worktree = _make_worktree(tmp_path, f"agent-{_NFC_MARK}")
    nfd_target = str(
        tmp_path / ".claude" / "worktrees" / f"agent-{_NFD_MARK}" / "file.txt"
    )
    payload = _payload(nfd_target)
    result = _run_guard(
        payload,
        cwd=str(worktree),
        input_text=json.dumps(payload, ensure_ascii=False),
    )
    assert result.returncode == 0, (
        f"同一実体への NFD 表現参照は許可されるべきだが exit={result.returncode}\n"
        f"stderr: {ascii(result.stderr)}"
    )


def test_write_to_existing_nfd_named_outside_directory_is_blocked(
    tmp_path: Path,
) -> None:
    """NFD 名で実在する worktree 外ディレクトリへの書き込みは block。"""
    worktree = _make_worktree(tmp_path)
    outside_dir = tmp_path / f"outside-{_NFD_MARK}"
    outside_dir.mkdir()
    target = str(outside_dir / "file.txt")
    payload = _payload(target)
    result = _run_guard(
        payload,
        cwd=str(worktree),
        input_text=json.dumps(payload, ensure_ascii=False),
    )
    assert result.returncode == 2, (
        f"worktree 外(NFD 名)への書き込みが block されなかった: "
        f"exit={result.returncode} (target={ascii(target)})"
    )


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason=(
        "darwin は正規化非感受 FS のため NFD 名が同一実体になり許可が期待値"
        "（test_nfd_form_path_to_nfc_worktree_is_allowed_on_darwin で固定）。"
    ),
)
def test_nfd_form_path_to_nfc_worktree_stays_blocked_off_darwin(
    tmp_path: Path,
) -> None:
    """Windows / Linux では NFD 表現の参照は block（NFD 名は別実体）。"""
    worktree = _make_worktree(tmp_path, f"agent-{_NFC_MARK}")
    nfd_target = str(
        tmp_path / ".claude" / "worktrees" / f"agent-{_NFD_MARK}" / "file.txt"
    )
    payload = _payload(nfd_target)
    result = _run_guard(
        payload,
        cwd=str(worktree),
        input_text=json.dumps(payload, ensure_ascii=False),
    )
    assert result.returncode == 2, (
        f"NFD 名は実在しない（別実体）ため block が期待値だが "
        f"exit={result.returncode}\nstderr: {ascii(result.stderr)}"
    )


# ===========================================================================
# (12) 入力の形が想定外のとき
#
# (12-1)〜(12-3) は現行でも成り立つ振る舞いの回帰の対照（Write/Edit 以外・
# 判定不能な stdin・空の file_path）。(12-4)(12-5) は型が想定外の入力で未捕捉
# 例外（exit 1）を漏らさないこと: agent_type が文字列でなければ対象外（exit 0）、
# 対象 agent で tool_input / file_path の型が想定外なら fail-closed（exit 2）。
# ===========================================================================


def _assert_no_traceback(result: subprocess.CompletedProcess, context: str) -> None:
    assert "Traceback" not in result.stderr, (
        f"[{context}] 未捕捉例外が漏れた: {ascii(result.stderr)}"
    )


@pytest.mark.parametrize("tool_name", ["Read", "Bash", "NotebookEdit", "Glob"])
def test_wt_agent_non_write_edit_tool_is_not_guarded(tmp_path: Path, tool_name: str) -> None:
    """(12-1) 対象 agent でも Write/Edit 以外のツールは exit 0（ルート外のパスでも）."""
    worktree = _make_worktree(tmp_path)
    result = _run_guard(
        _payload(str(tmp_path / "outside_file.txt"), tool_name=tool_name),
        cwd=str(worktree),
    )
    _assert_allowed(result, f"(12-1) tool_name={tool_name}")
    _assert_no_traceback(result, f"(12-1) tool_name={tool_name}")


@pytest.mark.parametrize(
    "raw_stdin",
    ["{not json", "", "   ", "[]", '"wt_developer"', "42", "null"],
    ids=["invalid_json", "empty", "whitespace", "list", "string", "number", "null"],
)
def test_unparseable_or_non_dict_stdin_is_allowed(tmp_path: Path, raw_stdin: str) -> None:
    """(12-2) stdin が不正な JSON・空・dict でない JSON → 対象 agent を判定できず exit 0."""
    worktree = _make_worktree(tmp_path)
    result = _run_guard({}, cwd=str(worktree), input_text=raw_stdin)
    _assert_allowed(result, f"(12-2) stdin={raw_stdin!r}")
    _assert_no_traceback(result, f"(12-2) stdin={raw_stdin!r}")


@pytest.mark.parametrize("tool_name", ["Write", "Edit"])
def test_wt_agent_empty_file_path_is_allowed(tmp_path: Path, tool_name: str) -> None:
    """(12-3) 対象 agent で file_path が空文字列 → exit 0."""
    worktree = _make_worktree(tmp_path)
    result = _run_guard(_payload("", tool_name=tool_name), cwd=str(worktree))
    _assert_allowed(result, f"(12-3) empty file_path ({tool_name})")
    _assert_no_traceback(result, f"(12-3) empty file_path ({tool_name})")


@pytest.mark.parametrize(
    "agent_type",
    [["wt_developer"], {"name": "wt_developer"}, 1, None],
    ids=["list", "dict", "number", "null"],
)
def test_non_string_agent_type_is_not_guarded(tmp_path: Path, agent_type: object) -> None:
    """(12-4) agent_type が文字列でない → 対象外として exit 0・Traceback を出さない.

    キーは存在させ（null も明示的に入れる）、値の型だけを変える。
    """
    worktree = _make_worktree(tmp_path)
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(tmp_path / "outside_file.txt")},
        "agent_type": agent_type,
    }
    result = _run_guard(payload, cwd=str(worktree))
    _assert_no_traceback(result, f"(12-4) agent_type={agent_type!r}")
    _assert_allowed(result, f"(12-4) agent_type={agent_type!r}")


@pytest.mark.parametrize(
    "tool_input",
    [None, "C:/outside.txt", ["file_path"]],
    ids=["null", "string", "list"],
)
@pytest.mark.parametrize("tool_name", ["Write", "Edit"])
def test_wt_agent_non_dict_tool_input_is_blocked(
    tmp_path: Path, tool_name: str, tool_input: object
) -> None:
    """(12-5) 対象 agent で tool_input が dict でない → exit 2（fail-closed）・Traceback なし."""
    worktree = _make_worktree(tmp_path)
    payload = {"tool_name": tool_name, "tool_input": tool_input, "agent_type": _WT_AGENT}
    result = _run_guard(payload, cwd=str(worktree))
    _assert_no_traceback(result, f"(12-5) tool_input={tool_input!r}")
    assert result.returncode == 2, (
        f"[(12-5) tool_input={tool_input!r}] block(exit 2) が期待値だが "
        f"exit={result.returncode}\nstderr: {ascii(result.stderr)}"
    )


@pytest.mark.parametrize(
    "file_path",
    [123, ["a.txt"]],
    ids=["number", "list"],
)
@pytest.mark.parametrize("tool_name", ["Write", "Edit"])
def test_wt_agent_non_string_file_path_is_blocked(
    tmp_path: Path, tool_name: str, file_path: object
) -> None:
    """(12-5) 対象 agent で file_path が文字列でない → exit 2（fail-closed）・Traceback なし."""
    worktree = _make_worktree(tmp_path)
    payload = {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
        "agent_type": _WT_AGENT,
    }
    result = _run_guard(payload, cwd=str(worktree))
    _assert_no_traceback(result, f"(12-5) file_path={file_path!r}")
    assert result.returncode == 2, (
        f"[(12-5) file_path={file_path!r}] block(exit 2) が期待値だが "
        f"exit={result.returncode}\nstderr: {ascii(result.stderr)}"
    )


# ===========================================================================
# (12-6) 対象 agent の Write/Edit で、hook 自身の cwd の取得・正規化
# （os.getcwd / os.path.realpath）が OSError / ValueError を出したとき、
# hook は Traceback を出さず exit 2（block・fail-closed）で終わる。
#
# cwd の削除は OS によって再現できない（Windows は使用中ディレクトリを削除できない）
# ため、hook モジュールをプロセス内で読み込み、モジュールが参照する `os` を
# 例外を出す差し替え版（プロキシ）にして main() を直接呼ぶ。グローバルの os を
# 差し替えないのは、pytest 本体（capture・tmp_path 等）を巻き込まないため。
# ===========================================================================


class _RaisingOsPath:
    """`os.path` の代理。`realpath` だけが例外を出し、他は本物へ委譲する。"""

    def __init__(self, exc: BaseException | None) -> None:
        self._exc = exc

    def realpath(self, *args, **kwargs):
        if self._exc is not None:
            raise self._exc
        return os.path.realpath(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(os.path, name)


class _RaisingOs:
    """`os` の代理。`getcwd` / `path.realpath` の片方が例外を出し、他は本物へ委譲する。"""

    def __init__(
        self,
        *,
        cwd: str,
        getcwd_exc: BaseException | None = None,
        realpath_exc: BaseException | None = None,
    ) -> None:
        self._cwd = cwd
        self._getcwd_exc = getcwd_exc
        self.path = _RaisingOsPath(realpath_exc)

    def getcwd(self) -> str:
        if self._getcwd_exc is not None:
            raise self._getcwd_exc
        return self._cwd

    def __getattr__(self, name: str):
        return getattr(os, name)


@pytest.mark.parametrize(
    ("failing", "exc"),
    [
        ("getcwd", FileNotFoundError(2, "cwd has been removed")),
        ("getcwd", OSError(5, "access denied")),
        ("getcwd", ValueError("embedded null character")),
        ("realpath", OSError(5, "access denied")),
        ("realpath", ValueError("embedded null character")),
    ],
    ids=[
        "getcwd-FileNotFoundError",
        "getcwd-OSError",
        "getcwd-ValueError",
        "realpath-OSError",
        "realpath-ValueError",
    ],
)
@pytest.mark.parametrize("tool_name", ["Write", "Edit"])
def test_wt_agent_cwd_resolution_error_is_blocked_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tool_name: str,
    failing: str,
    exc: BaseException,
) -> None:
    """(12-6) 対象 agent で cwd の取得・正規化が例外 → exit 2（fail-closed）・Traceback なし."""
    import io

    module = _load_guard_module()
    worktree = _make_worktree(tmp_path)
    payload = _payload(str(worktree / "inside.txt"), tool_name=tool_name)

    proxy = _RaisingOs(
        cwd=str(worktree),
        getcwd_exc=exc if failing == "getcwd" else None,
        realpath_exc=exc if failing == "realpath" else None,
    )
    context = f"(12-6) {failing} raises {type(exc).__name__} ({tool_name})"

    with monkeypatch.context() as m:
        m.delenv("C3_WORKTREE_GUARD_DISABLE", raising=False)
        m.setattr(module, "os", proxy)
        m.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        code: object = 0
        leaked_repr: str | None = None
        try:
            module.main()
        except SystemExit as stop:
            code = stop.code
        except Exception as leaked:  # noqa: BLE001 - 未捕捉例外の漏れそのものを検出する
            code = None
            leaked_repr = f"{type(leaked).__name__}: {leaked}"

    stderr = capsys.readouterr().err
    assert leaked_repr is None, (
        f"[{context}] main() から未捕捉例外が漏れた（実行時は Traceback・exit 1 になる）: "
        f"{ascii(leaked_repr)}"
    )
    assert "Traceback" not in stderr, f"[{context}] Traceback が出た: {ascii(stderr)}"
    assert code == 2, (
        f"[{context}] block(exit 2) が期待値だが exit={code!r}\nstderr: {ascii(stderr)}"
    )
