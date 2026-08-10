"""Tests for .dev/hooks/_template_guard.py

PreToolUse hook（配布元専用）の挙動を検証する。

テストケース:
 ブロック動作:
  1. src/c3/_template/ への Write → exit 2 + stderr 警告
  2. src/c3/_template/ への Edit (絶対パス) → exit 2
  3. ディレクトリトラバーサル経由でも resolve 後 block

 通過動作:
  4. src/c3/cli.py など _template/ 外 → exit 0
  5. tool_name が Read など Write/Edit 以外 → exit 0
  6. file_path が空 / payload に無い → exit 0
  7. 不正な JSON → exit 0 (crash しない)

 bypass:
  8. C3_TEMPLATE_GUARD_DISABLE=1 設定下では _template/ 配下でも exit 0

 9. S3 M1 Unicode 正規化 / stdin UTF-8 受信 — 下記参照

## 9. S3 M1（Red / 回帰ガード）

block 判定は `resolved == template_root or resolved.startswith(template_root + os.sep)`
という生文字列比較なので、`src/c3/_template/` を指す実体であっても Unicode
正規化形が違えば一致しない。擬似リポジトリのルートディレクトリ名に NFC/NFD の
表現差を載せ、NFC 形のみを実在させて NFD 形の絶対パスを payload で渡すと、
現行は **block されず素通り（fail-open）** する。

観測される現行の欠陥は 2 つあり、いずれも「素通り（exit 0）」として現れる:

    欠陥 1: stdin reconfigure 欠落 — payload を生の UTF-8 で送ると cp932 既定で
            復号され file_path が化ける（UnicodeDecodeError は ValueError の
            サブクラスなので既存 except に飲まれ、やはり exit 0 で沈黙する）
    欠陥 2: NFC 正規化欠落 — realpath 後の比較両辺を正規化しないため表現差で
            prefix 判定が外れる

Red 群は両者を分離して固定する（R1 = 両方 / R2 = 欠陥 2 のみ / R3 = 欠陥 1 のみ）。
テスト側で PYTHONIOENCODING 等を使って復号問題を回避することはしない
（production 側の欠陥を隠すため）。

回帰ガード群は「表現一致ケース」「非 ASCII ルートでの相対パス」「NFD ルート配下
の `_template/` 外ファイル（過剰ブロックの検出）」を是正前後とも緑で固定する。

ソースに NFD リテラルを直書きせず `unicodedata.normalize()` で実行時に生成し、
アサーションメッセージ中のパスは `ascii()` で退避する（cp932 環境でのエンコード
不能を避けるため）。

`.dev/` は gitignore 対象だが、テストファイル自体は配布される。利用者環境で
このテストが落ちないよう、利用者環境に `.dev/hooks/_template_guard.py` が
無い場合は skip する。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".dev" / "hooks" / "_template_guard.py"

pytestmark = pytest.mark.skipif(
    not HOOK_PATH.is_file(),
    reason=".dev/hooks/_template_guard.py is distributor-only (gitignored)",
)


def _run_hook(
    payload: dict,
    *,
    env: dict | None = None,
    cwd: str | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    """hook を subprocess で起動する。

    `cwd` 省略時はリポジトリルート、`input_text` 省略時は既定の
    `json.dumps(payload)`（非 ASCII は `\\uXXXX` エスケープ）を stdin へ流す。
    どちらも既定値のままなら従来の呼び出しと完全に同じ挙動になる。
    """
    run_env = os.environ.copy()
    # bypass 環境変数のテスト時のみ override したいので、デフォルトでは消す
    run_env.pop("C3_TEMPLATE_GUARD_DISABLE", None)
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload) if input_text is None else input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(WORKTREE_ROOT) if cwd is None else cwd,
        env=run_env,
    )


def _payload(tool_name: str, file_path: str) -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
    }


class TestBlock:
    """src/c3/_template/ 配下の Write / Edit を block する。"""

    def test_block_write_to_template_relative(self) -> None:
        result = _run_hook(_payload("Write", "src/c3/_template/foo.md"))
        assert result.returncode == 2
        assert "[TemplateGuard BLOCK]" in result.stderr

    def test_block_edit_with_absolute_template_path(self) -> None:
        abs_target = str(WORKTREE_ROOT / "src" / "c3" / "_template" / "subdir" / "x.py")
        result = _run_hook(_payload("Edit", abs_target))
        assert result.returncode == 2
        assert "[TemplateGuard BLOCK]" in result.stderr

    def test_block_realpath_traversal(self) -> None:
        """src/c3/../c3/_template/x も解決後に block される。"""
        result = _run_hook(_payload("Write", "src/c3/../c3/_template/x.md"))
        assert result.returncode == 2

    def test_block_template_root_itself(self) -> None:
        """_template/ ルート自身を file として書こうとしても block。"""
        result = _run_hook(_payload("Write", "src/c3/_template"))
        assert result.returncode == 2


class TestPass:
    """対象外パスは exit 0 で通過する。"""

    def test_allow_write_outside_template(self) -> None:
        result = _run_hook(_payload("Write", "src/c3/cli.py"))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_allow_edit_in_claude_dir(self) -> None:
        result = _run_hook(_payload("Edit", ".claude/hooks/post_tool.py"))
        assert result.returncode == 0

    def test_non_write_edit_tool_passes(self) -> None:
        result = _run_hook(_payload("Read", "src/c3/_template/foo.md"))
        assert result.returncode == 0

    def test_empty_file_path_passes(self) -> None:
        result = _run_hook(_payload("Write", ""))
        assert result.returncode == 0

    def test_payload_without_file_path(self) -> None:
        result = _run_hook({"tool_name": "Write", "tool_input": {}})
        assert result.returncode == 0

    def test_invalid_json_does_not_crash(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input="this is not json",
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0


class TestBypass:
    """C3_TEMPLATE_GUARD_DISABLE=1 で全て exit 0 になる。"""

    def test_bypass_via_env_var(self) -> None:
        result = _run_hook(
            _payload("Write", "src/c3/_template/foo.md"),
            env={"C3_TEMPLATE_GUARD_DISABLE": "1"},
        )
        assert result.returncode == 0
        assert "[TemplateGuard BLOCK]" not in result.stderr

    def test_bypass_disabled_with_other_value_still_blocks(self) -> None:
        """値が '1' 以外なら無効化されず block する（誤設定の安全側挙動）。"""
        result = _run_hook(
            _payload("Write", "src/c3/_template/foo.md"),
            env={"C3_TEMPLATE_GUARD_DISABLE": "true"},
        )
        assert result.returncode == 2


# ===========================================================================
# 9. S3 M1 Unicode 正規化 / stdin UTF-8 受信
# ===========================================================================

# NFC と NFD で表現が異なる文字（ダ = U+30C0 / U+30BF + U+3099）。
# NFD リテラルをソースへ直書きせず実行時に生成する（モジュール docstring 参照）。
_NFC_MARK = unicodedata.normalize("NFC", "ダ")
_NFD_MARK = unicodedata.normalize("NFD", _NFC_MARK)


def _utf8_stdin(payload: dict) -> str:
    """payload を生の UTF-8 で送るための JSON 文字列（`\\uXXXX` 退避をしない）。

    `ensure_ascii=True`（既定）だと非 ASCII が ASCII エスケープされ、hook 側の
    stdin が cp932 でも復号できてしまう＝欠陥 1 を隠す。
    """
    return json.dumps(payload, ensure_ascii=False)


def _make_fake_repo(tmp_path: Path) -> tuple[Path, Path]:
    """NFC 形の名前を持つ擬似リポジトリを実在させ `(NFC ルート, NFD ルート)` を返す。

    `<root>/src/c3/_template/` を NFC 形でのみ実在させる。NFD 形のルートは
    **作成しない**（表現差の検証のため実在してはならない）。ファイル名を正規化して
    保存する FS（macOS HFS+ 等）では表現差が消えて前提が崩れるため skip する。
    """
    assert _NFC_MARK != _NFD_MARK, (
        "テスト前提が崩れている: NFC と NFD で表現が異なる文字を使うこと "
        f"(NFC={ascii(_NFC_MARK)}, NFD={ascii(_NFD_MARK)})"
    )
    base = Path(os.path.realpath(tmp_path))
    nfc_root = base / f"repo-{_NFC_MARK}"
    nfc_root.mkdir()
    nfc_root = Path(os.path.realpath(nfc_root))
    nfd_root = nfc_root.parent / f"repo-{_NFD_MARK}"
    if unicodedata.normalize("NFC", str(nfc_root)) != str(nfc_root) or nfd_root.exists():
        pytest.skip(
            "ファイル名を正規化する FS のため NFC/NFD の表現差が観測できない "
            f"(created={ascii(str(nfc_root))})"
        )
    (nfc_root / "src" / "c3" / "_template").mkdir(parents=True)
    return nfc_root, nfd_root


def _template_file(root: Path) -> str:
    return str(root / "src" / "c3" / "_template" / "foo.md")


# ---------------------------------------------------------------------------
# 9-1. Red 群（是正前は素通り＝fail-open で赤くなる）
# ---------------------------------------------------------------------------


def test_nfd_absolute_path_into_template_is_blocked_with_utf8_payload(
    tmp_path: Path,
) -> None:
    """[S3M1・Red R1] NFD 形の絶対パスを生 UTF-8 で送っても _template/ 配下は block。

    是正前は欠陥 1（stdin cp932 復号）と欠陥 2（NFC 正規化欠落）の両方により
    exit 0 で素通りする。構文エラー等ではなく機能未実装による失敗。
    """
    nfc_root, nfd_root = _make_fake_repo(tmp_path)
    target = _template_file(nfd_root)
    payload = _payload("Write", target)

    result = _run_hook(payload, cwd=str(nfc_root), input_text=_utf8_stdin(payload))

    assert result.returncode == 2, (
        "NFD 形の絶対パスでも NFC 正規化後は _template/ 配下と判定され block されるべきだが "
        f"exit={result.returncode} (target={ascii(target)}, cwd={ascii(str(nfc_root))})"
    )
    assert "[TemplateGuard BLOCK]" in result.stderr


def test_nfd_absolute_path_into_template_is_blocked_with_ascii_escaped_payload(
    tmp_path: Path,
) -> None:
    """[S3M1・Red R2] 欠陥 2（NFC 正規化欠落）のみを分離した Red。

    payload を ASCII エスケープで送るため stdin の復号は現行でも成功する。
    それでも表現差だけで block が外れることを固定する。
    """
    nfc_root, nfd_root = _make_fake_repo(tmp_path)
    target = _template_file(nfd_root)

    result = _run_hook(_payload("Edit", target), cwd=str(nfc_root))

    assert result.returncode == 2, (
        "stdin の復号が成功していても NFC 正規化が無いと表現差で block が外れる "
        f"exit={result.returncode} (target={ascii(target)})"
    )


def test_nfc_absolute_path_into_template_is_blocked_with_utf8_payload(
    tmp_path: Path,
) -> None:
    """[S3M1・Red R3] 欠陥 1（stdin reconfigure 欠落）のみを分離した Red。

    表現は cwd と一致（NFC 同士）だが payload を生 UTF-8 で送るため、
    現行は cp932 復号で file_path が化けて（または ValueError で沈黙して）素通りする。
    """
    nfc_root, _ = _make_fake_repo(tmp_path)
    target = _template_file(nfc_root)
    payload = _payload("Write", target)

    result = _run_hook(payload, cwd=str(nfc_root), input_text=_utf8_stdin(payload))

    assert result.returncode == 2, (
        "非 ASCII を含む payload を UTF-8 で受け取れていない（stdin reconfigure 欠落）"
        f" exit={result.returncode} (target={ascii(target)})"
    )


# ---------------------------------------------------------------------------
# 9-2. 回帰ガード群（是正前後とも緑を維持する）
# ---------------------------------------------------------------------------


def test_nfc_absolute_path_with_escaped_payload_still_blocks(tmp_path: Path) -> None:
    """[S3M1・回帰] 表現一致（NFC×NFC）・ASCII エスケープ送信は現行でも block する。"""
    nfc_root, _ = _make_fake_repo(tmp_path)

    result = _run_hook(_payload("Write", _template_file(nfc_root)), cwd=str(nfc_root))

    assert result.returncode == 2, (
        f"表現一致ケースの block が失われた: exit={result.returncode}"
    )
    assert "[TemplateGuard BLOCK]" in result.stderr


def test_relative_path_in_non_ascii_repo_still_blocks(tmp_path: Path) -> None:
    """[S3M1・回帰] 非 ASCII 名のルートでも相対パス payload は block する。

    相対パスは hook 側で cwd と結合されるため、表現差が生じない経路。
    """
    nfc_root, _ = _make_fake_repo(tmp_path)

    result = _run_hook(_payload("Write", "src/c3/_template/foo.md"), cwd=str(nfc_root))

    assert result.returncode == 2, (
        f"相対パス経路の block が失われた: exit={result.returncode}"
    )


def test_unrelated_file_under_nfd_root_stays_allowed(tmp_path: Path) -> None:
    """[S3M1・回帰] NFD ルート配下でも `_template/` 外なら素通りのまま。

    是正後に「NFC 正規化したら何でも block する」過剰ブロックになっていないことを
    固定する（是正前は全て素通りなので、この緑が意味を持つのは是正後）。
    """
    nfc_root, nfd_root = _make_fake_repo(tmp_path)
    target = str(nfd_root / "src" / "c3" / "cli.py")
    payload = _payload("Write", target)

    result = _run_hook(payload, cwd=str(nfc_root), input_text=_utf8_stdin(payload))

    assert result.returncode == 0, (
        f"_template/ 外のファイルが block された: exit={result.returncode} "
        f"stderr={ascii(result.stderr)}"
    )
    assert result.stderr == ""


def test_bypass_env_var_still_wins_over_nfd_path(tmp_path: Path) -> None:
    """[S3M1・回帰] bypass 環境変数は NFD 形の _template/ 配下パスでも優先される。

    正規化の導入後も、緊急 bypass の関所（最初の環境変数判定）が
    パス判定より前段にあることを固定する。
    """
    nfc_root, nfd_root = _make_fake_repo(tmp_path)
    target = _template_file(nfd_root)
    payload = _payload("Write", target)

    result = _run_hook(
        payload,
        env={"C3_TEMPLATE_GUARD_DISABLE": "1"},
        cwd=str(nfc_root),
        input_text=_utf8_stdin(payload),
    )

    assert result.returncode == 0
    assert "[TemplateGuard BLOCK]" not in result.stderr
