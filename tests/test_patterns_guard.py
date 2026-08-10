"""Tests for .claude/hooks/patterns_guard.py (P1 hook, 未実装 — Red フェーズ)

plan-report-20260725-180252.md T1(1) / architecture-report-20260725-175915.md §2 D-1 の
仕様に基づく。

## Red の理由

`.claude/hooks/patterns_guard.py` はまだ存在しない（T2 developer 実装前）。本ファイルの
`_install_fake_repo()` ヘルパーは対象ファイルが無ければ `FileNotFoundError` を送出する
（`pytestmark = pytest.mark.skipif(...)` は使わない。SKIP では Red の証跡が残らないため。
fixture 経由のテストは ERROR、非 fixture のテストは FAILED として失敗が可視化される）。
構文エラー・タイポによる失敗ではなく、hook 不在という「機能未実装」による失敗である。

唯一の例外は `TestSavePatternsSourceRegression`（下記参照）: これはリポジトリ実体の
`stop.py` を直接検査する回帰固定ケースで、patterns_guard.py の実装状況に**依存しない**。
stop.py は既に `tempfile.mkstemp` / `os.fdopen` / `os.replace` の書き込み経路を実装済み
なので、このケース単体は Red 時点でも成功しうる（偶発 Pass ではなく意図した回帰ガード）。

## tmp_path 擬似リポジトリ方式（K-2 対応）

実装対象 hook は保護対象パス・フラグパスを `Path(__file__)` から導出する設計
（architecture D-1(1)(3)）。そのため hook スクリプト自体を `tmp_path` 配下の
`.claude/hooks/patterns_guard.py` にコピーして起動し、保護対象・フラグの解決先を
tmp_path 配下に閉じ込める。実リポジトリの `.claude/memory/patterns.json` /
`.claude/state/` には一切触れない。

設計判断メモ（tester による具体化）:
    - 保護対象ファイルの実パスは architecture 本文では「patterns.json」としか
      書かれていないが、D-1 の前提事実節が参照する stop.py の実装
      (`PATTERNS_FILE = os.path.join(_CLAUDE_DIR, 'memory', 'patterns.json')`)
      から `.claude/memory/patterns.json` に確定させた。
    - フラグパスは architecture 本文に明記された
      `.claude/state/patterns_guard_allow.flag` をそのまま用いる。
    - 両パスとも hook ファイルの 2 階層上（`.claude/hooks/` → `.claude` → リポジトリ
      ルート相当）を起点に解決される想定。既存 hook（`planner_check.py`
      `PROJECT_ROOT = Path(__file__).resolve().parents[2]`）と同じ慣例。
    - TTL 境界テストは `time.time()` を注入できない subprocess 方式のため、厳密な
      600.000 秒ちょうどではなく、サブプロセス起動レイテンシを吸収する数秒のバッファを
      設けた「境界近傍」の値（597 秒 / 603 秒）で allow/block 両側を検証する。

## ケース仕様（plan T1(1) 準拠）

    1. tool_name が Write/Edit 以外 → exit 0（対象外）
    2. 対象パスが patterns.json 以外 → exit 0（対象外）
    3. patterns.json への Write/Edit（realpath 一致）で許可フラグが無ければ exit 2 block。
       stderr に正規経路（session.tmp 経由・promote-pattern）・フラグ作成 2 例
       （touch / New-Item）・`C3_PATTERNS_GUARD_DISABLE=1` の案内を含む
    4. 相対パス・`..` トラバーサル経由でも realpath 一致すれば block
    5. フラグ TTL 型: `time.time() - os.path.getmtime(flag) <= 600` の境界判定。
       TTL 内は複数回許可されフラグは削除されない。未来 mtime（負値）も許可
    6. TTL 超過（600 秒超）はフラグを削除したうえで block
    7. `C3_PATTERNS_GUARD_DISABLE=1` → 常に exit 0（フラグの有無に関わらず）
    8. 壊れた JSON・キー欠落 → exit 0（fail-open）
    9. リポジトリ実体 stop.py の `save_patterns` ソース検査（回帰固定・DC-GP-007）

## E 周回 1 修正サイクル追加分（T5・Red — plan-report §2-B）

以下は F3 / F4 / F5 の未実装による Red。すべて独立した module-level の test 関数として
追加する（rework2 DC-AM-001）。

    (c) F3: `file_path` の `.resolve()` を `except (OSError, ValueError): sys.exit(0)`
        で包む。現行実装は NUL byte 混入パスで `ValueError: embedded null character`
        を送出し exit 1 + トレースバックになる → Red。
    (d) F4: `TTL_SECONDS = 600` をモジュール定数化する。現行実装は判定式が
        `if age <= 600:` のリテラル直書きで定数が存在しない → Red。
        TTL は subprocess 経由で観測できない（600 秒待てない）ため、AST による
        ソース検査で「定数の定義」と「判定式からの参照」を固定する（rework DC-AM-002a）。
    (g) F5: `_install_fake_repo()` が `_hook_utils.py` を tmp hooks へ同梱コピーする。
        F5 で patterns_guard.py が `from _hook_utils import sanitize_for_terminal`
        するようになるため、単体コピー方式の擬似リポジトリでも import を成立させる
        （前例 tests/hooks/test_restore_session.py:79-93 の session_utils.py 同梱）。
        この fixture 変更自体は既存テストの挙動を変えない（コピーが 1 ファイル増えるのみ）。

## S3 ① NFC 正規化 追加分（Red）

閉じる穴: `resolve()` 後の等値比較が Unicode 正規化形の違い（NFC / NFD）で不一致になり、
同一ファイルを指す別表現の `file_path` がガードを素通りする fail-open。

    - Red 群: NFD 表現の絶対パス × NFC 表現で実在する擬似リポジトリ → block(exit 2) を期待。
      現行実装は文字列としての表現差で `resolved != protected_path` となり exit 0（素通り）。
    - 回帰ガード群: NFC × NFC の block 維持・無関係パス（NFD 名）の素通り維持・
      正規化後も許可フラグ経路が効くこと。既存 ASCII ケースは本ファイル上記 28 件が担保する。

ソースに NFD リテラルを直接埋め込まず `unicodedata.normalize()` で実行時に生成する
（cp932 環境で NFD の結合文字がテストソース表示・stdout でエンコード不能になるのを避ける）。
アサーションメッセージ中のパスは `ascii()` で退避する（同じ理由）。
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

WORKTREE_ROOT = Path(__file__).resolve().parent.parent
SOURCE_HOOK = WORKTREE_ROOT / ".claude" / "hooks" / "patterns_guard.py"
STOP_PY = WORKTREE_ROOT / ".claude" / "hooks" / "stop.py"
HOOK_UTILS = WORKTREE_ROOT / ".claude" / "hooks" / "_hook_utils.py"

TTL_SECONDS = 600


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------


def _install_fake_repo(tmp_path: Path) -> Path:
    """tmp_path 配下に `.claude/hooks/patterns_guard.py` をコピーした擬似リポジトリを作る。

    SOURCE_HOOK が存在しない場合（Red フェーズ）は FileNotFoundError を送出する。
    """
    if not SOURCE_HOOK.is_file():
        raise FileNotFoundError(
            f"{SOURCE_HOOK} が存在しません（P1 hook 未実装のため Red）"
        )
    hooks_dir = tmp_path / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    dest = hooks_dir / "patterns_guard.py"
    shutil.copy(SOURCE_HOOK, dest)
    # F5: 共有 `_hook_utils.sanitize_for_terminal()` を import できるよう同梱する
    # （前例 tests/hooks/test_restore_session.py:79-93 の session_utils.py 同梱）
    if HOOK_UTILS.is_file():
        shutil.copy(HOOK_UTILS, hooks_dir / "_hook_utils.py")
    return dest


def _patterns_json_path(tmp_path: Path, *, create: bool = True) -> Path:
    p = tmp_path / ".claude" / "memory" / "patterns.json"
    if create:
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text('{"patterns": []}', encoding="utf-8")
    return p


def _flag_path(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "state" / "patterns_guard_allow.flag"


def _write_flag(tmp_path: Path, age_seconds: float) -> Path:
    """`age_seconds` 秒前の mtime を持つフラグファイルを作る（負値で未来 mtime）。"""
    flag = _flag_path(tmp_path)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("", encoding="utf-8")
    mtime = time.time() - age_seconds
    os.utime(flag, (mtime, mtime))
    return flag


def _run_hook(
    hook_path: Path,
    payload: dict | None,
    *,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
    raw_stdin: str | None = None,
) -> subprocess.CompletedProcess:
    env: dict[str, str] = {"PYTHONIOENCODING": "utf-8"}
    for key in ("SYSTEMROOT", "PATH"):
        if key in os.environ:
            env[key] = os.environ[key]
    if extra_env:
        env.update(extra_env)
    stdin_data = raw_stdin if raw_stdin is not None else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(hook_path)],
        input=stdin_data,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(cwd) if cwd is not None else None,
    )


def _write_payload(tool_name: str, file_path: str) -> dict:
    return {"tool_name": tool_name, "tool_input": {"file_path": file_path}}


# ---------------------------------------------------------------------------
# 1. 対象外の tool_name / パス → exit 0
# ---------------------------------------------------------------------------


class TestNonTargetPassthrough:
    def test_bash_tool_name_is_ignored(self, tmp_path: Path) -> None:
        """tool_name が Bash（Write/Edit 以外）なら patterns.json 相手でも exit 0."""
        hook = _install_fake_repo(tmp_path)
        patterns = _patterns_json_path(tmp_path)
        result = _run_hook(hook, _write_payload("Bash", str(patterns)))
        assert result.returncode == 0, (
            f"tool_name=Bash は対象外のはずが exit {result.returncode}\n{result.stderr}"
        )

    def test_notebook_edit_tool_name_is_ignored(self, tmp_path: Path) -> None:
        """NotebookEdit も P1 の対象外（Write/Edit のみが対象・射程の明記）。"""
        hook = _install_fake_repo(tmp_path)
        patterns = _patterns_json_path(tmp_path)
        result = _run_hook(hook, _write_payload("NotebookEdit", str(patterns)))
        assert result.returncode == 0

    def test_unrelated_file_path_is_ignored(self, tmp_path: Path) -> None:
        """patterns.json 以外への Write は exit 0."""
        hook = _install_fake_repo(tmp_path)
        _patterns_json_path(tmp_path)  # 保護対象は用意しておくが対象にはしない
        other = tmp_path / ".claude" / "memory" / "other.json"
        other.parent.mkdir(parents=True, exist_ok=True)
        result = _run_hook(hook, _write_payload("Write", str(other)))
        assert result.returncode == 0, (
            f"無関係ファイルへの Write が block された: exit {result.returncode}\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# 2. フラグ無しでの block（直接パス・相対/.. パス）
# ---------------------------------------------------------------------------


class TestBlockWithoutFlag:
    def test_direct_write_to_patterns_json_is_blocked(self, tmp_path: Path) -> None:
        hook = _install_fake_repo(tmp_path)
        patterns = _patterns_json_path(tmp_path)
        result = _run_hook(hook, _write_payload("Write", str(patterns)))
        assert result.returncode == 2, (
            f"フラグなしの直接 Write は block(exit 2) されるはずが exit {result.returncode}\n"
            f"{result.stderr}"
        )
        assert result.stderr.strip(), "block 時は stderr にメッセージが必要"

    def test_direct_edit_to_patterns_json_is_blocked(self, tmp_path: Path) -> None:
        hook = _install_fake_repo(tmp_path)
        patterns = _patterns_json_path(tmp_path)
        result = _run_hook(hook, _write_payload("Edit", str(patterns)))
        assert result.returncode == 2, (
            f"フラグなしの直接 Edit は block(exit 2) されるはずが exit {result.returncode}"
        )

    def test_relative_dotdot_path_resolves_and_is_blocked(self, tmp_path: Path) -> None:
        """相対パス + `..` トラバーサル経由でも realpath 一致すれば block."""
        hook = _install_fake_repo(tmp_path)
        _patterns_json_path(tmp_path)
        state_dir = tmp_path / ".claude" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        payload = _write_payload("Write", "state/../memory/patterns.json")
        result = _run_hook(hook, payload, cwd=tmp_path / ".claude")
        assert result.returncode == 2, (
            f"相対 '..' 経由の patterns.json 指定が block されなかった: exit "
            f"{result.returncode}\n{result.stderr}"
        )

    def test_absolute_dotdot_path_resolves_and_is_blocked(self, tmp_path: Path) -> None:
        """絶対パスに `..` セグメントを含む場合も realpath 正規化後に一致すれば block."""
        hook = _install_fake_repo(tmp_path)
        _patterns_json_path(tmp_path)
        state_dir = tmp_path / ".claude" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        traversal_path = tmp_path / ".claude" / "state" / ".." / "memory" / "patterns.json"
        result = _run_hook(hook, _write_payload("Write", str(traversal_path)))
        assert result.returncode == 2, (
            f"絶対 '..' 経由の patterns.json 指定が block されなかった: exit "
            f"{result.returncode}\n{result.stderr}"
        )

    def test_block_message_mentions_regular_channels_and_flag_instructions(
        self, tmp_path: Path
    ) -> None:
        """block 時の stderr に正規経路・フラグ作成 2 例・disable env の案内が含まれる。"""
        hook = _install_fake_repo(tmp_path)
        patterns = _patterns_json_path(tmp_path)
        result = _run_hook(hook, _write_payload("Write", str(patterns)))
        assert result.returncode == 2
        stderr = result.stderr
        assert "session.tmp" in stderr, f"正規経路(session.tmp)の案内が無い: {stderr!r}"
        assert "promote-pattern" in stderr, f"正規経路(promote-pattern)の案内が無い: {stderr!r}"
        assert "touch" in stderr, f"フラグ作成例(touch)の案内が無い: {stderr!r}"
        assert "New-Item" in stderr, f"フラグ作成例(New-Item)の案内が無い: {stderr!r}"
        assert "C3_PATTERNS_GUARD_DISABLE=1" in stderr, (
            f"恒久 bypass 環境変数の案内が無い: {stderr!r}"
        )


# ---------------------------------------------------------------------------
# 3. フラグ TTL 型
# ---------------------------------------------------------------------------


class TestFlagTtl:
    def test_flag_within_ttl_allows_without_deleting(self, tmp_path: Path) -> None:
        hook = _install_fake_repo(tmp_path)
        patterns = _patterns_json_path(tmp_path)
        flag = _write_flag(tmp_path, age_seconds=300)  # 300 秒前 = TTL 内
        result = _run_hook(hook, _write_payload("Write", str(patterns)))
        assert result.returncode == 0, (
            f"TTL 内のフラグで allow されるはずが exit {result.returncode}\n{result.stderr}"
        )
        assert flag.exists(), "TTL 内の許可はフラグを削除しない仕様のはず"

    def test_flag_allows_multiple_operations_within_ttl(self, tmp_path: Path) -> None:
        """TTL 内は 1 フラグで複数回の操作が許可される（consume 型ではない）。"""
        hook = _install_fake_repo(tmp_path)
        patterns = _patterns_json_path(tmp_path)
        flag = _write_flag(tmp_path, age_seconds=100)
        result1 = _run_hook(hook, _write_payload("Write", str(patterns)))
        result2 = _run_hook(hook, _write_payload("Edit", str(patterns)))
        assert result1.returncode == 0
        assert result2.returncode == 0, (
            f"2 回目の操作が block された（consume 型になっている可能性）: "
            f"exit {result2.returncode}\n{result2.stderr}"
        )
        assert flag.exists(), "複数回許可後もフラグは残っているはず"

    def test_flag_near_ttl_boundary_allows(self, tmp_path: Path) -> None:
        """境界近傍（600 秒よりやや手前）は許可される。

        サブプロセス起動レイテンシを吸収するため 597 秒（600 秒より 3 秒手前）を使う。
        """
        hook = _install_fake_repo(tmp_path)
        patterns = _patterns_json_path(tmp_path)
        flag = _write_flag(tmp_path, age_seconds=TTL_SECONDS - 3)
        result = _run_hook(hook, _write_payload("Write", str(patterns)))
        assert result.returncode == 0, (
            f"境界近傍(TTL手前)は allow されるはずが exit {result.returncode}\n{result.stderr}"
        )
        assert flag.exists()

    def test_flag_near_ttl_boundary_blocks_and_deletes(self, tmp_path: Path) -> None:
        """境界近傍（600 秒よりやや超過）は block されフラグが削除される。"""
        hook = _install_fake_repo(tmp_path)
        patterns = _patterns_json_path(tmp_path)
        flag = _write_flag(tmp_path, age_seconds=TTL_SECONDS + 3)
        result = _run_hook(hook, _write_payload("Write", str(patterns)))
        assert result.returncode == 2, (
            f"境界近傍(TTL超過)は block されるはずが exit {result.returncode}\n{result.stderr}"
        )
        assert not flag.exists(), "TTL 超過フラグは削除される仕様のはず"

    def test_flag_future_mtime_allows(self, tmp_path: Path) -> None:
        """mtime が未来（差が負値）は「作りたて」とみなして許可する。"""
        hook = _install_fake_repo(tmp_path)
        patterns = _patterns_json_path(tmp_path)
        flag = _write_flag(tmp_path, age_seconds=-3600)  # 1時間後の未来 mtime
        result = _run_hook(hook, _write_payload("Write", str(patterns)))
        assert result.returncode == 0, (
            f"未来 mtime のフラグは allow されるはずが exit {result.returncode}\n{result.stderr}"
        )
        assert flag.exists()

    def test_flag_expired_over_ttl_deletes_and_blocks(self, tmp_path: Path) -> None:
        hook = _install_fake_repo(tmp_path)
        patterns = _patterns_json_path(tmp_path)
        flag = _write_flag(tmp_path, age_seconds=3600)  # 1 時間前 = 大幅超過
        result = _run_hook(hook, _write_payload("Write", str(patterns)))
        assert result.returncode == 2, (
            f"TTL 大幅超過は block されるはずが exit {result.returncode}\n{result.stderr}"
        )
        assert not flag.exists(), "TTL 超過フラグは削除される仕様のはず"


# ---------------------------------------------------------------------------
# 4. 恒久 disable env
# ---------------------------------------------------------------------------


class TestDisableEnvVar:
    def test_disable_env_always_allows_even_without_flag(self, tmp_path: Path) -> None:
        hook = _install_fake_repo(tmp_path)
        patterns = _patterns_json_path(tmp_path)
        result = _run_hook(
            hook,
            _write_payload("Write", str(patterns)),
            extra_env={"C3_PATTERNS_GUARD_DISABLE": "1"},
        )
        assert result.returncode == 0, (
            f"C3_PATTERNS_GUARD_DISABLE=1 は常に allow のはずが exit {result.returncode}\n"
            f"{result.stderr}"
        )

    def test_disable_env_allows_even_with_expired_flag(self, tmp_path: Path) -> None:
        hook = _install_fake_repo(tmp_path)
        patterns = _patterns_json_path(tmp_path)
        _write_flag(tmp_path, age_seconds=3600)  # 超過フラグがあっても env が優先
        result = _run_hook(
            hook,
            _write_payload("Write", str(patterns)),
            extra_env={"C3_PATTERNS_GUARD_DISABLE": "1"},
        )
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# 5. fail-open
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_malformed_json_stdin_exits_zero(self, tmp_path: Path) -> None:
        hook = _install_fake_repo(tmp_path)
        _patterns_json_path(tmp_path)
        result = _run_hook(hook, None, raw_stdin="{not valid json")
        assert result.returncode == 0, (
            f"壊れた JSON は fail-open(exit 0) のはずが exit {result.returncode}\n{result.stderr}"
        )

    def test_missing_tool_name_key_exits_zero(self, tmp_path: Path) -> None:
        hook = _install_fake_repo(tmp_path)
        patterns = _patterns_json_path(tmp_path)
        payload = {"tool_input": {"file_path": str(patterns)}}
        result = _run_hook(hook, payload)
        assert result.returncode == 0

    def test_missing_file_path_key_exits_zero(self, tmp_path: Path) -> None:
        hook = _install_fake_repo(tmp_path)
        _patterns_json_path(tmp_path)
        payload = {"tool_name": "Write", "tool_input": {}}
        result = _run_hook(hook, payload)
        assert result.returncode == 0

    def test_empty_stdin_exits_zero(self, tmp_path: Path) -> None:
        hook = _install_fake_repo(tmp_path)
        _patterns_json_path(tmp_path)
        result = _run_hook(hook, None, raw_stdin="")
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# 6. ソース検査（回帰固定・DC-GP-007）
# ---------------------------------------------------------------------------


class TestSavePatternsSourceRegression:
    """stop.py の save_patterns がアトミック書き込み経路を維持していることの回帰固定。

    Note: このテストは patterns_guard.py の実装状況に依存しない。stop.py は既に
    アトミック書き込みを実装済みのため、Red フェーズ時点でも本ケース単体は成功しうる
    （偶発 Pass ではなく、将来 save_patterns がツール経由化された場合に赤くなる回帰ガード
    として意図的に配置している）。
    """

    def test_stop_py_exists(self) -> None:
        assert STOP_PY.is_file(), f"{STOP_PY} が見つからない"

    def test_save_patterns_uses_atomic_tempfile_write(self) -> None:
        sys.path.insert(0, str(STOP_PY.parent))
        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location("stop_module_under_test", STOP_PY)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            source = inspect.getsource(module.save_patterns)
        finally:
            if str(STOP_PY.parent) in sys.path:
                sys.path.remove(str(STOP_PY.parent))

        assert "tempfile.mkstemp" in source, (
            "save_patterns が tempfile.mkstemp を使っていない（アトミック書き込み経路の回帰）"
        )
        assert "os.fdopen" in source, (
            "save_patterns が os.fdopen を使っていない（アトミック書き込み経路の回帰）"
        )
        assert "os.replace" in source, (
            "save_patterns が os.replace を使っていない（アトミック書き込み経路の回帰）"
        )


# ===========================================================================
# T5 追加分（E 周回 1 修正サイクル・Red）— すべて独立した module-level test 関数
# ===========================================================================


# ---------------------------------------------------------------------------
# (c) F3: NUL byte 混入 file_path で fail-open
# ---------------------------------------------------------------------------


def test_nul_byte_in_file_path_exits_zero(tmp_path: Path) -> None:
    """[F3・Red] NUL byte 混入 file_path でも fail-open（exit 0）する。

    現行実装は `Path(file_path).resolve()` が `ValueError: embedded null character`
    を送出し、未捕捉のまま exit 1 でクラッシュする。`.resolve()` を
    `except (OSError, ValueError): sys.exit(0)` で包むと exit 0 になる。
    """
    hook = _install_fake_repo(tmp_path)
    _patterns_json_path(tmp_path)
    nul_path = str(tmp_path / ".claude" / "memory" / "pat\x00terns.json")
    result = _run_hook(hook, _write_payload("Write", nul_path))
    assert result.returncode == 0, (
        f"NUL byte 混入パスは fail-open(exit 0) のはずが exit {result.returncode}\n"
        f"{result.stderr}"
    )


def test_nul_byte_in_file_path_emits_no_traceback(tmp_path: Path) -> None:
    """[F3・Red] NUL byte 混入 file_path で stderr にトレースバックが出ない。"""
    hook = _install_fake_repo(tmp_path)
    _patterns_json_path(tmp_path)
    nul_path = str(tmp_path / ".claude" / "memory" / "pat\x00terns.json")
    result = _run_hook(hook, _write_payload("Write", nul_path))
    assert "Traceback" not in result.stderr, (
        f"NUL byte 混入パスでトレースバックが出力された: {result.stderr!r}"
    )
    assert "ValueError" not in result.stderr, (
        f"NUL byte 混入パスで未捕捉の ValueError が漏れた: {result.stderr!r}"
    )


def test_nul_byte_in_relative_file_path_exits_zero(tmp_path: Path) -> None:
    """[F3・Red] 相対パス経路（cwd 合成後の resolve）でも NUL byte で落ちない。"""
    hook = _install_fake_repo(tmp_path)
    _patterns_json_path(tmp_path)
    result = _run_hook(
        hook,
        _write_payload("Write", "memory/pat\x00terns.json"),
        cwd=tmp_path / ".claude",
    )
    assert result.returncode == 0, (
        f"相対 NUL byte パスは fail-open(exit 0) のはずが exit {result.returncode}\n"
        f"{result.stderr}"
    )


# ---------------------------------------------------------------------------
# (d) F4: TTL_SECONDS のモジュール定数化（AST ソース検査）
# ---------------------------------------------------------------------------


def _parse_source_hook() -> ast.Module:
    """リポジトリ実体の patterns_guard.py を AST として読む（未実装なら Red）。"""
    if not SOURCE_HOOK.is_file():
        raise FileNotFoundError(
            f"{SOURCE_HOOK} が存在しません（P1 hook 未実装のため Red）"
        )
    return ast.parse(SOURCE_HOOK.read_text(encoding="utf-8"))


def test_ttl_seconds_is_module_level_constant() -> None:
    """[F4・Red] `TTL_SECONDS = 600` がモジュール直下の定数として定義されている。

    subprocess 方式では 600 秒の実時間を観測できないため、定数化はソース検査で固定する
    （rework DC-AM-002a）。
    """
    tree = _parse_source_hook()
    found: list[object] = []
    for node in tree.body:  # モジュール直下のみ（関数内ローカルは対象外）
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "TTL_SECONDS":
                    found.append(ast.literal_eval(node.value))
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id == "TTL_SECONDS"
                and node.value is not None
            ):
                found.append(ast.literal_eval(node.value))
    assert found, (
        "patterns_guard.py のモジュール直下に TTL_SECONDS 定数の定義が無い"
        "（判定式に 600 をリテラル直書きしている）"
    )
    assert found[0] == TTL_SECONDS, (
        f"TTL_SECONDS の値が {TTL_SECONDS} でない: {found[0]!r}"
    )


def test_ttl_comparison_references_the_constant() -> None:
    """[F4・Red] TTL 判定式が `TTL_SECONDS` を参照する（リテラル 600 の直書きでない）。

    `time.time() - os.path.getmtime(flag) <= TTL_SECONDS` の右辺が定数参照であること。
    """
    tree = _parse_source_hook()
    compares_with_constant = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any(
            isinstance(sub, ast.Name) and sub.id == "TTL_SECONDS"
            for operand in [node.left, *node.comparators]
            for sub in ast.walk(operand)
        )
    ]
    assert compares_with_constant, (
        "TTL 判定の比較式が TTL_SECONDS を参照していない"
        "（定数を定義しても判定式がリテラルのままでは意味がない）"
    )
    assert any(
        any(isinstance(op, ast.LtE) for op in node.ops)
        for node in compares_with_constant
    ), "TTL 判定の比較演算子が `<=`（境界を含む）になっていない"


def test_ttl_literal_is_not_hardcoded_in_comparison() -> None:
    """[F4・Red] TTL 判定式に 600 のリテラル直書きが残っていない。"""
    tree = _parse_source_hook()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for operand in [node.left, *node.comparators]:
            for sub in ast.walk(operand):
                assert not (
                    isinstance(sub, ast.Constant) and sub.value == TTL_SECONDS
                ), (
                    "TTL 判定の比較式に 600 がリテラルで直書きされている"
                    "（TTL_SECONDS 定数を参照すること）"
                )


# ===========================================================================
# S3 ① NFC 正規化（Red / 回帰ガード）
# ===========================================================================

# NFC と NFD で表現が異なる文字（ダ = U+30C0 / U+30BF + U+3099）。
# NFD リテラルをソースへ直書きせず実行時に生成する（モジュール docstring 参照）。
_NFC_MARK = unicodedata.normalize("NFC", "ダ")
_NFD_MARK = unicodedata.normalize("NFD", _NFC_MARK)

_NFC_REPO_DIRNAME = f"repo-{_NFC_MARK}"
_NFD_REPO_DIRNAME = f"repo-{_NFD_MARK}"


def _install_unicode_fake_repo(tmp_path: Path) -> tuple[Path, Path]:
    """NFC 正規形のディレクトリ名を持つ擬似リポジトリを作り ``(repo_root, hook)`` を返す。

    `.claude/memory/patterns.json` は ASCII なので、NFC / NFD の表現差は
    リポジトリルートのディレクトリ名（実在するのは NFC 形のみ）に載せる。
    """
    assert _NFC_MARK != _NFD_MARK, (
        "テスト前提が崩れている: NFC と NFD で表現が異なる文字を使うこと "
        f"(NFC={ascii(_NFC_MARK)}, NFD={ascii(_NFD_MARK)})"
    )
    repo_root = tmp_path / _NFC_REPO_DIRNAME
    repo_root.mkdir(parents=True, exist_ok=True)
    hook = _install_fake_repo(repo_root)
    _patterns_json_path(repo_root)
    return repo_root, hook


def _nfd_path_in_repo(tmp_path: Path, *parts: str) -> str:
    """NFD 表現のリポジトリルート経由の絶対パス文字列を組み立てる（実在しない表現）。"""
    return str(tmp_path.joinpath(_NFD_REPO_DIRNAME, *parts))


def _run_hook_utf8(hook: Path, payload: dict) -> subprocess.CompletedProcess:
    """payload を（\\uXXXX エスケープせず）UTF-8 のまま stdin へ送る。"""
    return _run_hook(hook, None, raw_stdin=json.dumps(payload, ensure_ascii=False))


# --- Red 群: NFD 入力 × NFC 実在 -------------------------------------------


def test_nfd_form_path_to_patterns_json_is_blocked(tmp_path: Path) -> None:
    """[S3①・Red] NFD 表現の絶対パスでも patterns.json への Write は block される。

    現行実装は `resolved != protected_path` の等値比較が Unicode 正規化形の違いで
    不一致になり exit 0（素通り＝fail-open）。比較の両辺に NFC 正規化を適用すると
    exit 2 になる。
    """
    _repo_root, hook = _install_unicode_fake_repo(tmp_path)
    nfd_target = _nfd_path_in_repo(tmp_path, ".claude", "memory", "patterns.json")
    result = _run_hook_utf8(hook, _write_payload("Write", nfd_target))
    assert result.returncode == 2, (
        f"NFD 表現の patterns.json 指定が block されなかった: exit {result.returncode} "
        f"(path={ascii(nfd_target)})\n{ascii(result.stderr)}"
    )


def test_nfd_form_path_emits_block_message(tmp_path: Path) -> None:
    """[S3①・Red] NFD 表現で block されたとき通常と同じ案内が stderr に出る。"""
    _repo_root, hook = _install_unicode_fake_repo(tmp_path)
    nfd_target = _nfd_path_in_repo(tmp_path, ".claude", "memory", "patterns.json")
    result = _run_hook_utf8(hook, _write_payload("Edit", nfd_target))
    assert "PatternGuard BLOCK" in result.stderr, (
        f"block メッセージが出ていない: {ascii(result.stderr)}"
    )
    assert "Traceback" not in result.stderr, (
        f"正規化処理で未捕捉例外が漏れた: {ascii(result.stderr)}"
    )


# --- 回帰ガード群 -----------------------------------------------------------


def test_nfc_form_path_in_unicode_repo_stays_blocked(tmp_path: Path) -> None:
    """[S3①・回帰] NFC 入力 × NFC 実在は現行どおり block(exit 2) のまま。"""
    repo_root, hook = _install_unicode_fake_repo(tmp_path)
    nfc_target = str(repo_root / ".claude" / "memory" / "patterns.json")
    result = _run_hook_utf8(hook, _write_payload("Write", nfc_target))
    assert result.returncode == 2, (
        f"NFC 入力での block が壊れた: exit {result.returncode}\n{ascii(result.stderr)}"
    )


def test_unrelated_nfd_form_path_stays_ignored(tmp_path: Path) -> None:
    """[S3①・回帰] 無関係ファイルは NFD 表現でも素通り（正規化で過剰ブロックしない）。"""
    _repo_root, hook = _install_unicode_fake_repo(tmp_path)
    nfd_other = _nfd_path_in_repo(tmp_path, ".claude", "memory", "other.json")
    result = _run_hook_utf8(hook, _write_payload("Write", nfd_other))
    assert result.returncode == 0, (
        f"無関係ファイル(NFD 表現)が block された: exit {result.returncode}\n"
        f"{ascii(result.stderr)}"
    )


def test_nfd_form_path_with_valid_flag_is_allowed(tmp_path: Path) -> None:
    """[S3①・回帰] 正規化を効かせても TTL 内の許可フラグ経路は allow のまま。

    是正前は「表現差で素通り」という別の理由で exit 0 になるため、本ケースは是正の
    前後どちらでも緑。フラグ経路が正規化導入で潰れないことを固定する回帰ガード。
    """
    repo_root, hook = _install_unicode_fake_repo(tmp_path)
    flag = _write_flag(repo_root, age_seconds=100)
    nfd_target = _nfd_path_in_repo(tmp_path, ".claude", "memory", "patterns.json")
    result = _run_hook_utf8(hook, _write_payload("Write", nfd_target))
    assert result.returncode == 0, (
        f"TTL 内フラグがあるのに block された: exit {result.returncode}\n"
        f"{ascii(result.stderr)}"
    )
    assert flag.exists(), "TTL 内の許可はフラグを削除しない仕様のはず"
