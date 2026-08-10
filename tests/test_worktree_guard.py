"""Tests for .claude/hooks/worktree_guard.py

Tests are organised around the CWD-based activation scheme:
1. CWD outside .claude/worktrees/ → exits 0 (guard disabled, silent)
2. CWD inside .claude/worktrees/ + Write inside worktree → exits 0
3. CWD inside .claude/worktrees/ + Write outside worktree → exits 2 + stderr
4. Block message sanitizes ANSI escapes in file_path (sec-Low)
5. env gate: PO_WORKTREE_GUARD 未設定 → no-op (CWD が worktree 内でも exit 0)
6. S3 ① NFC 正規化 / 二層 AND 判定（decide_containment）— 下記参照

## 6. S3 ① 二層 AND 設計（Red / 回帰ガード）

許可には次の両方の合格が必要（どちらか一方でも不合格なら block）:

    第 1 層: 正規化文字列（realpath 後に NFC 正規化）の prefix 判定
    第 2 層: ファイルシステム実体による cwd 配下確認
             （対象の実在する最深祖先から根方向へ祖先鎖を辿り、いずれかの祖先
               ——最深祖先自身を含む——が cwd と samefile なら配下。対象自身が
               cwd と同一の場合も許可）

判定は純関数 `worktree_guard.decide_containment()` に集約し、実体同一性判定
（および実在判定）を引数注入することで単体テスト可能にする。戻り値は
**ブロック判定**（True = block / False = allow）。

    Red 群（関数レベル）: 是正前は `decide_containment` が存在しないため、
        当該テスト関数のみ「関数不在」で赤くなる（意図した赤）。
    Red 群（プロセスレベル）: file_path に NUL を含む payload。是正前後で
        観測値が変わる真の Red。
    Red 群（darwin 限定）: 正規化非感受 FS 上の同一実体を NFD/NFC 表現差で参照。
    回帰ガード群: 実在サブディレクトリ配下への書き込み許可（過剰ブロック検出）・
        NFD 名の実在する worktree 外ディレクトリへの block・既存 ASCII ケース。

hook モジュールの読み込みは**テスト関数内**で行う（モジュールレベル import に
すると、上記 1〜5 の subprocess 方式 8 件まで collection error に巻き込まれる）。

ソースに NFD リテラルを直書きせず `unicodedata.normalize()` で実行時に生成し、
アサーションメッセージ中のパスは `ascii()` で退避する（cp932 環境でのエンコード
不能を避けるため）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

# Absolute path to the hook under test
_HOOK = Path(__file__).parent.parent / ".claude" / "hooks" / "worktree_guard.py"

# Absolute path to the template copy of the hook
_TEMPLATE_HOOK = (
    Path(__file__).parent.parent
    / "src"
    / "c3"
    / "_template"
    / ".claude"
    / "hooks"
    / "worktree_guard.py"
)


def _make_worktree_cwd(base: Path) -> Path:
    """`base/.claude/worktrees/agent-test/` を作って返す（worktree-shaped CWD）."""
    worktree = base / ".claude" / "worktrees" / "agent-test"
    worktree.mkdir(parents=True, exist_ok=True)
    return worktree


def _run_guard(
    payload: dict,
    *,
    cwd: str | None = None,
    hook: Path = _HOOK,
    enable_guard: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    """Run worktree_guard.py as a subprocess, feeding *payload* via stdin.

    worktree_guard.py は `PO_WORKTREE_GUARD=1` が設定されている場合のみ動作する。
    デフォルトでガード有効化（`enable_guard=True`）でテストする。

    `input_text` を渡すと *payload* の代わりにその文字列をそのまま stdin へ送る
    （非 ASCII を `\\uXXXX` エスケープせず UTF-8 のまま送りたいケース用）。
    """
    env: dict[str, str] = {}
    if enable_guard:
        env["PO_WORKTREE_GUARD"] = "1"
    # Windows では subprocess に SYSTEMROOT を継承させないと sys.executable 起動が失敗する
    for key in ("SYSTEMROOT", "PATH"):
        if key in os.environ:
            env[key] = os.environ[key]
    return subprocess.run(
        [sys.executable, str(hook)],
        input=input_text if input_text is not None else json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=cwd,
    )


# ---------------------------------------------------------------------------
# 1. CWD outside .claude/worktrees/ → exits 0 silently
# ---------------------------------------------------------------------------


def test_guard_disabled_when_cwd_outside_worktrees(tmp_path: Path):
    """CWD が .claude/worktrees/ 外なら exit 0 で何もしない（main セッション扱い）."""
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "/some/outside/path/file.txt"},
    }
    result = _run_guard(payload, cwd=str(tmp_path))
    assert result.returncode == 0, (
        f"Expected exit 0 when CWD is outside .claude/worktrees/, "
        f"got {result.returncode}"
    )
    assert not result.stderr.strip(), (
        f"Expected NO stderr output when guard is inactive, but got: {result.stderr!r}"
    )


def test_template_guard_disabled_when_cwd_outside_worktrees(tmp_path: Path):
    """Template copy も同じ挙動: CWD が外なら静かに exit 0."""
    import pytest

    if not _TEMPLATE_HOOK.exists():
        pytest.skip(f"Template hook not found at {_TEMPLATE_HOOK}; skipping.")

    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "/some/outside/path/file.txt"},
    }
    result = _run_guard(payload, cwd=str(tmp_path), hook=_TEMPLATE_HOOK)
    assert result.returncode == 0
    assert not result.stderr.strip(), (
        f"Template hook should produce NO stderr when CWD outside worktrees, "
        f"got: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# 2. CWD inside .claude/worktrees/ + Write inside worktree → exits 0
# ---------------------------------------------------------------------------


def test_write_inside_worktree_is_allowed(tmp_path: Path):
    """CWD が worktree 配下で、書き込み先も worktree 内部 → exit 0."""
    worktree = _make_worktree_cwd(tmp_path)
    target = worktree / "subdir" / "file.txt"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target)},
    }
    result = _run_guard(payload, cwd=str(worktree))
    assert result.returncode == 0, (
        f"Write inside worktree should be allowed (exit 0), got {result.returncode}.\n"
        f"stderr: {result.stderr}"
    )


def test_write_relative_path_inside_worktree_is_allowed(tmp_path: Path):
    """相対パスでの書き込みも worktree 内部に解決されれば exit 0."""
    worktree = _make_worktree_cwd(tmp_path)
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "subdir/file.txt"},
    }
    result = _run_guard(payload, cwd=str(worktree))
    assert result.returncode == 0, (
        f"Relative path inside worktree should be allowed (exit 0), got {result.returncode}"
    )


# ---------------------------------------------------------------------------
# 3. CWD inside .claude/worktrees/ + Write outside worktree → exits 2
# ---------------------------------------------------------------------------


def test_write_outside_worktree_is_blocked(tmp_path: Path):
    """CWD が worktree 配下で、書き込み先が worktree 外 → exit 2 + stderr."""
    worktree = _make_worktree_cwd(tmp_path)
    # tmp_path は worktree の親より上 → worktree 外
    outside = tmp_path / "outside_file.txt"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(outside)},
    }
    result = _run_guard(payload, cwd=str(worktree))
    assert result.returncode == 2, (
        f"Write outside worktree should be blocked (exit 2), got {result.returncode}.\n"
        f"stderr: {result.stderr}"
    )
    assert result.stderr.strip(), (
        "Blocked operation must also emit a message to stderr."
    )
    assert "WorktreeGuard BLOCK" in result.stderr, (
        f"stderr should contain '[WorktreeGuard BLOCK]', got: {result.stderr!r}"
    )


def test_write_absolute_path_to_main_repo_is_blocked(tmp_path: Path):
    """worktree CWD から絶対パスで main repo に書こうとしても exit 2 でブロック."""
    worktree = _make_worktree_cwd(tmp_path)
    # 別のディレクトリへの絶対パス（worktree の親階層など）
    outside_abs = tmp_path / "main_repo_path" / "src" / "file.py"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(outside_abs)},
    }
    result = _run_guard(payload, cwd=str(worktree))
    assert result.returncode == 2, (
        f"Absolute path to outside should be blocked, got {result.returncode}"
    )


# ---------------------------------------------------------------------------
# 4. Block message sanitizes ANSI escapes in file_path (sec-Low)
# ---------------------------------------------------------------------------


def test_block_message_sanitizes_ansi_escapes(tmp_path: Path):
    """file_path に ANSI escape が含まれていても stderr にそのまま出力されない."""
    worktree = _make_worktree_cwd(tmp_path)
    ansi_injected_path = str(tmp_path / f"outside\x1b[31mINJECTED\x1b[0m.txt")
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": ansi_injected_path},
    }
    result = _run_guard(payload, cwd=str(worktree))

    assert result.returncode == 2, (
        f"Command with ANSI-injected path must still be blocked (exit 2), "
        f"got exit={result.returncode}.\nstderr: {result.stderr!r}"
    )

    assert "\x1b" not in result.stderr, (
        "[sec-Low] Block message must not contain raw ANSI escape sequences. "
        f"stderr preview: {result.stderr[:300]!r}"
    )


# U+202E (RIGHT-TO-LEFT OVERRIDE)。ソースへ生の制御文字・エスケープ表記を書かず
# chr() で生成する（表示上は不可視のため、リテラル直書きは差分レビューで追えない）。
_RLO = chr(0x202E)


def test_block_message_sanitizes_bidi_override(tmp_path: Path):
    """[CR-M-001/SR-NEW-1] file_path の U+202E が BLOCK メッセージに生のまま出ない.

    共有ヘルパー `_hook_utils.sanitize_for_terminal` へ寄せる前の除去集合は
    C0 + DEL のみで、双方向制御文字（RLO）はそのまま stderr に出ていた。
    RLO はターミナル上で以降の文字列を右から左へ描画させ、実際の書き込み先を
    偽装できるため除去対象に含める。
    """
    worktree = _make_worktree_cwd(tmp_path)
    injected = str(tmp_path / f"outside{_RLO}INJECTED.txt")
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": injected},
    }
    result = _run_guard(payload, cwd=str(worktree))

    assert result.returncode == 2, (
        f"worktree 外への書き込みは block(exit 2) されるべきだが "
        f"exit={result.returncode}\nstderr: {ascii(result.stderr)}"
    )
    assert "WorktreeGuard BLOCK" in result.stderr, (
        f"BLOCK メッセージが出ていない: {ascii(result.stderr)}"
    )
    assert _RLO not in result.stderr, (
        "BLOCK メッセージに生の U+202E (RIGHT-TO-LEFT OVERRIDE) が残っている: "
        f"{ascii(result.stderr)}"
    )
    # 可読部分は残り、どのパスが拒否されたか分かること（過剰除去の検出）
    assert "outside" in result.stderr and "INJECTED" in result.stderr, (
        f"対象パスの可読部分が消えている: {ascii(result.stderr)}"
    )


# ---------------------------------------------------------------------------
# 5. env gate: PO_WORKTREE_GUARD 未設定なら CWD が worktree 内でも no-op
# ---------------------------------------------------------------------------


def test_guard_disabled_when_env_not_set(tmp_path: Path):
    """env 未設定なら CWD が worktree 配下でも no-op (exit 0)。

    worktree_guard.py L40 の env gate 契約を固定する回帰テスト。
    将来 env gate を外す変更（auto activation のみ）に切り替える場合は
    このテストが落ちることで設計変更を検出する。

    Note (env gate 廃止移行時):
        env gate を外して CWD ベース自動有効化に切り替える場合は、
        先に本テスト自体を更新（または削除）してから worktree_guard.py 側を変更すること。
        順序を逆にすると本テストが先に落ちて hook の正常な改修と区別できなくなる。
    """
    worktree = _make_worktree_cwd(tmp_path)
    outside = tmp_path / "outside_file.txt"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(outside)},
    }
    result = _run_guard(payload, cwd=str(worktree), enable_guard=False)

    assert result.returncode == 0, (
        f"Guard must be inactive without PO_WORKTREE_GUARD=1 even with CWD "
        f"inside worktree, got exit={result.returncode}\nstderr: {result.stderr}"
    )
    assert not result.stderr.strip(), (
        f"Inactive guard must produce NO stderr, got: {result.stderr!r}"
    )


# ===========================================================================
# 6. S3 ① NFC 正規化 / 二層 AND 判定
# ===========================================================================

# NFC と NFD で表現が異なる文字（ダ = U+30C0 / U+30BF + U+3099）。
# NFD リテラルをソースへ直書きせず実行時に生成する（モジュール docstring 参照）。
_NFC_MARK = unicodedata.normalize("NFC", "ダ")
_NFD_MARK = unicodedata.normalize("NFD", _NFC_MARK)


def _load_guard_module():
    """worktree_guard.py をテスト関数内でモジュールとして読み込む。

    モジュールレベル import にしないこと（上記 1〜5 の subprocess 方式テストを
    collection error に巻き込まないため）。`_hook_utils` を import する実装に
    なっても解決できるよう hooks/ を一時的に sys.path へ載せる。
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
    """`decide_containment` を取り出す。未実装なら意図した Red として失敗させる。"""
    module = _load_guard_module()
    decide = getattr(module, "decide_containment", None)
    assert decide is not None, (
        "[Red] worktree_guard.decide_containment が未実装（S3 ① 二層 AND 判定の"
        "純関数がモジュールレベルに公開されていない）。"
        "構文エラー・タイポではなく機能未実装による失敗。"
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
# 6-1. Red 群（関数レベル・是正前は decide_containment 不在で赤）
# ---------------------------------------------------------------------------


def test_decide_containment_allows_representation_difference_of_same_entity(
    tmp_path: Path,
) -> None:
    """[S3①・Red] 表現差のみ・実体同一なら許可される（block=False）。

    cwd は NFC 形、対象は NFD 形の同一ディレクトリ配下。第 1 層（NFC 正規化後の
    prefix 判定）と第 2 層（実体同一性）の両方が合格するため許可。
    """
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
    """[S3①・Red] 実体同一性判定が OSError を送出したら block（fail-closed）。

    例外の捕捉は decide_containment の内側で行い、呼び出し側へ伝播させない。
    （是正前は上記 6-1 の 1 件目と同じく decide_containment 不在による赤。）
    """
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
    """[S3①・Red] 対象自身が cwd と同一なら許可される（配下判定規則の明示条項）。"""
    decide = _require_decide_containment()
    cwd = _worktree_paths(tmp_path, "test")
    fs = _NormalizationInsensitiveFs(_ancestors_of(cwd))

    blocked = decide(cwd, cwd, same_entity=fs.same_entity, path_exists=fs.exists)
    assert blocked is False, (
        f"対象が cwd 自身なら許可されるべきだが {blocked!r} が返った"
    )


def test_decide_containment_blocks_when_first_layer_fails(tmp_path: Path) -> None:
    """[S3①・Red] 第 1 層（正規化文字列 prefix）不合格なら第 2 層合格でも block。

    二層 AND の片側だけで許可が出ないことを固定する。
    """
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
    """[S3①・Red] 第 2 層（FS 実体による配下確認）不合格なら block。

    文字列上は cwd 配下に見えても、実体として cwd 配下でなければ許可しない。
    """
    decide = _require_decide_containment()
    cwd = _worktree_paths(tmp_path, "test")
    resolved = str(Path(cwd) / "sub" / "file.txt")

    blocked = decide(
        resolved, cwd, same_entity=lambda _a, _b: False, path_exists=lambda _p: True
    )
    assert blocked is True, (
        "第 2 層不合格（実体が cwd 配下でない）なら block されるべきだが "
        f"{blocked!r} が返った"
    )


# ---------------------------------------------------------------------------
# 6-2. Red 群（プロセスレベル・是正前後で観測値が変わる）
# ---------------------------------------------------------------------------


def test_nul_byte_in_file_path_is_blocked(tmp_path: Path) -> None:
    """[S3①・Red] file_path に NUL を含む payload は block(exit 2) される。

    是正前の実測（Windows / Python 3.11）は exit 0 = 素通り（`os.path.realpath` が
    NUL を保持したまま返り、prefix 判定を通過する）。POSIX では `realpath` が
    `ValueError` を送出して exit 1 になる。いずれも exit 2 ではないため真の Red。
    解決不能なパスは fail-closed で block する。
    """
    worktree = _make_worktree_cwd(tmp_path)
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(worktree / "pa\x00th.txt")},
    }
    result = _run_guard(payload, cwd=str(worktree))
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
        "block が期待値（同趣旨の回帰ガードを "
        "test_nfd_form_path_to_nfc_worktree_stays_blocked_off_darwin で固定）。"
    ),
)
def test_nfd_form_path_to_nfc_worktree_is_allowed_on_darwin(tmp_path: Path) -> None:
    """[S3①・Red / darwin 限定] 同一実体を NFD 表現で参照した書き込みは許可される。

    worktree ディレクトリ名を NFC 形で実在させ、payload では NFD 形で参照する。
    是正前は第 1 層に相当する生文字列 prefix 判定が表現差で不一致になり exit 2
    （過剰ブロック）。是正後は第 1 層（NFC 正規化）・第 2 層（samefile）とも合格し
    exit 0。
    """
    worktree = tmp_path / ".claude" / "worktrees" / f"agent-{_NFC_MARK}"
    worktree.mkdir(parents=True)
    nfd_target = str(
        tmp_path / ".claude" / "worktrees" / f"agent-{_NFD_MARK}" / "file.txt"
    )
    payload = {"tool_name": "Write", "tool_input": {"file_path": nfd_target}}
    result = _run_guard(
        payload,
        cwd=str(worktree),
        input_text=json.dumps(payload, ensure_ascii=False),
    )
    assert result.returncode == 0, (
        f"同一実体への NFD 表現参照は許可されるべきだが exit={result.returncode}\n"
        f"stderr: {ascii(result.stderr)}"
    )


# ---------------------------------------------------------------------------
# 6-3. 回帰ガード群（実ファイル・是正前後で緑を維持）
# ---------------------------------------------------------------------------


def test_write_into_existing_subdirectory_is_allowed(tmp_path: Path) -> None:
    """[S3①・回帰] 実在するサブディレクトリ配下への書き込みは許可される。

    第 2 層導入による過剰ブロックを検出する必須ケース。`sub/` を実際に mkdir した
    うえで `sub/file.txt` を書く（既存の
    test_write_inside_worktree_is_allowed は実在しない subdir を使うため、
    「最深祖先＝worktree 自身」の経路しか通らない）。
    """
    worktree = _make_worktree_cwd(tmp_path)
    sub = worktree / "sub"
    sub.mkdir()
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(sub / "file.txt")},
    }
    result = _run_guard(payload, cwd=str(worktree))
    assert result.returncode == 0, (
        f"実在サブディレクトリ配下への書き込みが block された: "
        f"exit={result.returncode}\nstderr: {ascii(result.stderr)}"
    )


def test_write_to_existing_nfd_named_outside_directory_is_blocked(
    tmp_path: Path,
) -> None:
    """[S3①・回帰] NFD 名で実在する worktree 外ディレクトリへの書き込みは block。

    正規化を導入しても、worktree 外は表現に関わらず block のまま。
    """
    worktree = _make_worktree_cwd(tmp_path)
    outside_dir = tmp_path / f"outside-{_NFD_MARK}"
    outside_dir.mkdir()
    target = str(outside_dir / "file.txt")
    payload = {"tool_name": "Write", "tool_input": {"file_path": target}}
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
    """[S3①・回帰] Windows / Linux では NFD 表現の参照は是正前後とも block。

    第 1 層（NFC 正規化 prefix）は合格するが、NFD 名のディレクトリは実在しないため
    第 2 層（最深祖先の samefile 判定）が不合格になり block。射程の期待値
    「Windows / Linux では合成挙動が現行と同一」を固定する。
    """
    worktree = tmp_path / ".claude" / "worktrees" / f"agent-{_NFC_MARK}"
    worktree.mkdir(parents=True)
    nfd_target = str(
        tmp_path / ".claude" / "worktrees" / f"agent-{_NFD_MARK}" / "file.txt"
    )
    payload = {"tool_name": "Write", "tool_input": {"file_path": nfd_target}}
    result = _run_guard(
        payload,
        cwd=str(worktree),
        input_text=json.dumps(payload, ensure_ascii=False),
    )
    assert result.returncode == 2, (
        f"NFD 名は実在しない（別実体）ため block が期待値だが "
        f"exit={result.returncode}\nstderr: {ascii(result.stderr)}"
    )
