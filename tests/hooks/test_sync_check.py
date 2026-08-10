"""Tests for .dev/hooks/_sync_check.py

PostToolUse hook（配布元専用）の挙動を検証する。

テストケース:
 警告動作:
  1. .gitignore を Write → stderr に _excludes.py と hatch_build.py の同期警告
  2. src/c3/_excludes.py を Edit → stderr に .gitignore と hatch_build.py の同期警告
  3. hatch_build.py を Write → stderr に .gitignore と _excludes.py の同期警告

 通過動作（警告なし）:
  4. 関係ないファイル → stderr 空
  5. tool_name が Read など → stderr 空
  6. file_path が空 / payload に無い → stderr 空
  7. 不正な JSON → crash しない

 ブロックしない:
  8. いかなる入力でも exit 0

 9. S3 M1 Unicode 正規化 / stdin UTF-8 受信 — 下記参照

## 9. S3 M1（Red / 回帰ガード）

`rel in SYNC_GROUP` の比較対象は relpath 後の ASCII 定数集合なので、挙動を
変えうるのは **relpath 前段の入力（cwd と abs_target）の表現差** だけである。
そこで擬似リポジトリのルートディレクトリ名に NFC/NFD の表現差を載せ、
NFC 形のみを実在させて NFD 形の絶対パスを payload で渡す。

観測される現行の欠陥は 2 つあり、いずれも「無警告（fail-open）」として現れる:

    欠陥 1: stdin reconfigure 欠落 — payload を生の UTF-8 で送ると cp932 既定で
            復号され file_path が化ける（UnicodeDecodeError は ValueError の
            サブクラスなので既存 except に飲まれ、やはり沈黙する）
    欠陥 2: NFC 正規化欠落 — relpath 前に正規化しないため表現差で rel が
            `../repo-<NFD>/.gitignore` のような別パスになる

Red 群は両者を分離して固定する（R1 = 両方 / R2 = 欠陥 2 のみ / R3 = 欠陥 1 のみ）。
テスト側で PYTHONIOENCODING 等を使って復号問題を回避することはしない
（production 側の欠陥を隠すため）。

回帰ガード群は「表現一致ケース」「非 ASCII ルートでの相対パス」「NFD ルート配下
の無関係ファイル（過剰警告の検出）」を是正前後とも緑で固定する。

ソースに NFD リテラルを直書きせず `unicodedata.normalize()` で実行時に生成し、
アサーションメッセージ中のパスは `ascii()` で退避する（cp932 環境でのエンコード
不能を避けるため）。

`.dev/` は gitignore 対象だが、テストファイル自体は配布される。利用者環境に
`.dev/hooks/_sync_check.py` が無い場合は skip する。
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
HOOK_PATH = WORKTREE_ROOT / ".dev" / "hooks" / "_sync_check.py"

pytestmark = pytest.mark.skipif(
    not HOOK_PATH.is_file(),
    reason=".dev/hooks/_sync_check.py is distributor-only (gitignored)",
)


def _run_hook(
    payload: dict,
    *,
    cwd: str | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    """hook を subprocess で起動する。

    `cwd` 省略時はリポジトリルート、`input_text` 省略時は既定の
    `json.dumps(payload)`（非 ASCII は `\\uXXXX` エスケープ）を stdin へ流す。
    どちらも既定値のままなら従来の呼び出しと完全に同じ挙動になる。
    """
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload) if input_text is None else input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(WORKTREE_ROOT) if cwd is None else cwd,
    )


def _payload(tool_name: str, file_path: str) -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
    }


class TestWarn:
    """SYNC_GROUP の 3 ファイル変更時に他 2 ファイルの同期警告が出る。"""

    def test_warn_on_gitignore_change(self) -> None:
        result = _run_hook(_payload("Write", ".gitignore"))
        assert result.returncode == 0
        assert "[SyncCheck WARN]" in result.stderr
        assert "src/c3/_excludes.py" in result.stderr
        assert "hatch_build.py" in result.stderr
        # 自分自身は出力されないこと
        assert ".gitignore を変更しました" in result.stderr

    def test_warn_on_excludes_change(self) -> None:
        result = _run_hook(_payload("Edit", "src/c3/_excludes.py"))
        assert result.returncode == 0
        assert "[SyncCheck WARN]" in result.stderr
        assert ".gitignore" in result.stderr
        assert "hatch_build.py" in result.stderr

    def test_warn_on_hatch_build_change(self) -> None:
        result = _run_hook(_payload("Write", "hatch_build.py"))
        assert result.returncode == 0
        assert "[SyncCheck WARN]" in result.stderr
        assert ".gitignore" in result.stderr
        assert "src/c3/_excludes.py" in result.stderr

    def test_warn_with_absolute_path(self) -> None:
        abs_target = str(WORKTREE_ROOT / "hatch_build.py")
        result = _run_hook(_payload("Edit", abs_target))
        assert result.returncode == 0
        assert "[SyncCheck WARN]" in result.stderr


class TestNoWarn:
    """対象外なら警告なし。"""

    def test_no_warn_on_unrelated_file(self) -> None:
        result = _run_hook(_payload("Write", "src/c3/cli.py"))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_no_warn_on_read_tool(self) -> None:
        result = _run_hook(_payload("Read", ".gitignore"))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_no_warn_on_empty_file_path(self) -> None:
        result = _run_hook(_payload("Write", ""))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_no_warn_on_payload_without_file_path(self) -> None:
        result = _run_hook({"tool_name": "Write", "tool_input": {}})
        assert result.returncode == 0
        assert result.stderr == ""

    def test_invalid_json_does_not_crash(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input="not json",
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0


class TestNeverBlocks:
    """いかなる入力でも exit 0（ブロックしない）。"""

    @pytest.mark.parametrize(
        "payload",
        [
            {"tool_name": "Write", "tool_input": {"file_path": ".gitignore"}},
            {"tool_name": "Edit", "tool_input": {"file_path": "src/c3/_excludes.py"}},
            {"tool_name": "Read", "tool_input": {"file_path": ".gitignore"}},
            {"tool_name": "Write", "tool_input": {}},
            {},
        ],
    )
    def test_exit_zero(self, payload: dict) -> None:
        result = _run_hook(payload)
        assert result.returncode == 0


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

    NFD 形のルートは**作成しない**（表現差の検証のため実在してはならない）。
    ファイル名を正規化して保存する FS（macOS HFS+ 等）では表現差が消えて
    前提が崩れるため、その場合は skip する。
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
    (nfc_root / ".gitignore").write_text("", encoding="utf-8")
    return nfc_root, nfd_root


# ---------------------------------------------------------------------------
# 9-1. Red 群（是正前は無警告＝fail-open で赤くなる）
# ---------------------------------------------------------------------------


def test_nfd_absolute_path_warns_with_utf8_payload(tmp_path: Path) -> None:
    """[S3M1・Red R1] NFD 形の絶対パスを生 UTF-8 で送っても同期警告が出る。

    是正前は欠陥 1（stdin cp932 復号）と欠陥 2（NFC 正規化欠落）の両方により
    無警告。構文エラー等ではなく機能未実装による失敗。
    """
    nfc_root, nfd_root = _make_fake_repo(tmp_path)
    target = str(nfd_root / ".gitignore")
    payload = _payload("Write", target)

    result = _run_hook(payload, cwd=str(nfc_root), input_text=_utf8_stdin(payload))

    assert result.returncode == 0
    assert "[SyncCheck WARN]" in result.stderr, (
        "NFD 形の絶対パスでも NFC 正規化後は .gitignore と一致し警告されるべきだが "
        f"stderr={ascii(result.stderr)} (target={ascii(target)}, cwd={ascii(str(nfc_root))})"
    )
    assert "src/c3/_excludes.py" in result.stderr
    assert "hatch_build.py" in result.stderr


def test_nfd_absolute_path_warns_with_ascii_escaped_payload(tmp_path: Path) -> None:
    """[S3M1・Red R2] 欠陥 2（NFC 正規化欠落）のみを分離した Red。

    payload を ASCII エスケープで送るため stdin の復号は現行でも成功する。
    それでも表現差だけで無警告になることを固定する。
    """
    nfc_root, nfd_root = _make_fake_repo(tmp_path)
    target = str(nfd_root / ".gitignore")

    result = _run_hook(_payload("Write", target), cwd=str(nfc_root))

    assert result.returncode == 0
    assert "[SyncCheck WARN]" in result.stderr, (
        "stdin の復号が成功していても NFC 正規化が無いと表現差で警告が消える "
        f"stderr={ascii(result.stderr)} (target={ascii(target)})"
    )


def test_nfc_absolute_path_warns_with_utf8_payload(tmp_path: Path) -> None:
    """[S3M1・Red R3] 欠陥 1（stdin reconfigure 欠落）のみを分離した Red。

    表現は cwd と一致（NFC 同士）だが payload を生 UTF-8 で送るため、
    現行は cp932 復号で file_path が化けて（または ValueError で沈黙して）無警告。
    """
    nfc_root, _ = _make_fake_repo(tmp_path)
    target = str(nfc_root / ".gitignore")
    payload = _payload("Write", target)

    result = _run_hook(payload, cwd=str(nfc_root), input_text=_utf8_stdin(payload))

    assert result.returncode == 0
    assert "[SyncCheck WARN]" in result.stderr, (
        "非 ASCII を含む payload を UTF-8 で受け取れていない（stdin reconfigure 欠落）"
        f" stderr={ascii(result.stderr)} (target={ascii(target)})"
    )


# ---------------------------------------------------------------------------
# 9-2. 回帰ガード群（是正前後とも緑を維持する）
# ---------------------------------------------------------------------------


def test_nfc_absolute_path_with_escaped_payload_still_warns(tmp_path: Path) -> None:
    """[S3M1・回帰] 表現一致（NFC×NFC）・ASCII エスケープ送信は現行でも警告する。"""
    nfc_root, _ = _make_fake_repo(tmp_path)

    result = _run_hook(_payload("Write", str(nfc_root / ".gitignore")), cwd=str(nfc_root))

    assert result.returncode == 0
    assert "[SyncCheck WARN]" in result.stderr, (
        f"表現一致ケースの警告が失われた: stderr={ascii(result.stderr)}"
    )


def test_relative_path_in_non_ascii_repo_still_warns(tmp_path: Path) -> None:
    """[S3M1・回帰] 非 ASCII 名のルートでも相対パス payload は警告する。

    相対パスは hook 側で cwd と結合されるため、表現差が生じない経路。
    """
    nfc_root, _ = _make_fake_repo(tmp_path)

    result = _run_hook(_payload("Write", ".gitignore"), cwd=str(nfc_root))

    assert result.returncode == 0
    assert "[SyncCheck WARN]" in result.stderr, (
        f"相対パス経路の警告が失われた: stderr={ascii(result.stderr)}"
    )


def test_unrelated_file_under_nfd_root_stays_silent(tmp_path: Path) -> None:
    """[S3M1・回帰] NFD ルート配下でも SYNC_GROUP 外なら無警告のまま。

    是正後に「NFC 正規化したら何でも警告する」過剰警告になっていないことを固定する
    （是正前は全て無警告なので、この緑が意味を持つのは是正後）。
    """
    nfc_root, nfd_root = _make_fake_repo(tmp_path)
    target = str(nfd_root / "src" / "c3" / "cli.py")
    payload = _payload("Write", target)

    result = _run_hook(payload, cwd=str(nfc_root), input_text=_utf8_stdin(payload))

    assert result.returncode == 0
    assert result.stderr == "", (
        f"SYNC_GROUP 外のファイルで警告が出た: {ascii(result.stderr)}"
    )
