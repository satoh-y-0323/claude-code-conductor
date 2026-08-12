"""Tests for .claude/hooks/report_path_guard.py（PreToolUse・配布対象）

レポートファイルの **書き先パス** を守る新規 hook の挙動を固定する。本ファイルの
担当は **hook 単体の挙動のみ**。settings.json への登録有無・静的検査（stdin
reconfigure idiom 等）は別タスク（test-reg）の担当であり、ここでは一切扱わない。

## 仕様（タスク指示の逐語契約。外部文書は参照しない）

- stdin に JSON payload（`tool_name` / `tool_input.file_path`）を受け、PreToolUse
  として exit 0（許可）/ exit 2（block）を返す
- 発火条件: `tool_name` == "Write" かつ、basename を `norm_component` 相当
  （lower・末尾ドット/スペース除去）で正規化した値が対象 9 prefix のいずれかで
  始まる場合のみ。非対象 basename・Write 以外は沈黙（exit 0・stderr 空）
- 許可 root は 2 つ:
  root A = `realpath(os.getcwd())/.claude/reports`（一次）
  root B = `realpath($CLAUDE_PROJECT_DIR)/.claude/reports`（env が非空のときのみ）
  env 未設定・空なら root A のみで判定（縮退・block しない）
- 判定入力の一本化: target = file_path が絶対ならそのまま、相対なら `os.getcwd()`
  と結合したパス。第 1 層・第 2 層とも target を判定する
- 封じ込め（検査 1）: 第 1 層 = target の親が `reports`・祖父が `.claude`
  （norm_component 正規化の名前判定）。第 2 層 = `realpath(target)` の親ディレクトリが
  root A または root B と `os.path.samefile` で一致。両層 AND で許可。不成立・
  両 root 不在・親不在は block（fail-closed）。realpath 後の NUL 混入は明示 block
- 免責: 許可 root ディレクトリ自体（root A/B の `.claude/reports` そのもの）が外部への
  リンクに差し替えられているケースは expected と resolved が同一実体になるため
  block しない（設計上の脅威モデル外・allow が正しい）
- 判定の分割: prefix 一致判定は `norm_component` 正規化後の basename・timestamp
  形式判定は元の（正規化前の）basename に対して行う（report_contract_check.py と
  統一したポリシー）。これにより `.MD` 拡張子や prefix 大文字混じりは timestamp 形式
  として扱われない
- 上書き block（検査 2）: 元の basename が timestamp 形式（prefix + 8 桁-6 桁 +
  `.md` の `re.ASCII` フルマッチ）かつ書き込み先実体が既存 → block。task_id 形式
  （非 timestamp）の既存上書きは許可。timestamp 形式でも未存在なら許可
- strict-4 形式 block（検査 3）: strict-4 prefix（requirements / architecture /
  plan / design-review）に一致する（正規化 basename が prefix で始まる）ファイルの、
  元の basename が timestamp 形式にフルマッチしない場合は新規作成でも block
  （CR / SR / test / debug 系には非適用）
- fail 方針: 壊れた stdin JSON / file_path キー欠落は fail-open（exit 0）。パス判定中の
  例外を誘発する入力は fail-closed（block）
- env `C3_REPORT_GUARD_DISABLE=1` で全検査スキップ（exit 0）
- block 時 stderr は違反種別（封じ込め / 上書き / 形式）と対象 basename を含み、
  `_hook_utils.sanitize_for_terminal` を通す。回復誘導は違反種別で出し分ける:
  上書き・strict-4 形式違反 →「report-timestamp skill で新規採番」/
  封じ込め違反 →「書き先パスを .claude/reports/ 直下へ修正、cwd がプロジェクトルート外
  なら cd してから再実行」。Edit には言及しない

## 3 群分類

- **Red 群**: `_run_hook()` を呼ぶ全クラス（`TestTriggerConditions` /
  `TestContainment` / `TestTraversalAndLinkSpoofing` / `TestRelativePath` /
  `TestOverwriteCheck` / `TestStrict4Format` / `TestFailPolicy` /
  `TestEnvBypass` / `TestStderrContract`）。hook 未実装の現在は全て赤。
- **回帰ガード群**: `TestSharedHelperRegressionGuards`。既存の `_hook_utils` の
  挙動（本 hook の正規化・サニタイズが依存する土台）を凍結する。**現時点で緑であり、
  緑のまま維持するのが目的**（「最初から Pass するテストは修正する」の対象外）。
- **既存緑**: `tests/` 配下の既存スイート。本ファイルの追加で赤化させない。

## Red が「正しい理由」で赤くなること

hook が未実装のとき `python <missing>.py` は **exit 2** を返すため、block 期待の
テストが「偶然の緑」になりうる。これを避けるため `_run_hook()` は起動前に
hook ファイルの実在を検査し、無ければ `pytest.fail()` で明示的に落とす
（＝ Red の失敗理由が常に「hook 不在」になる）。

## スタブ 2 種による検知力実証時の注意（red-red 許可リスト）

do-nothing スタブ（always-exit-0 / always-exit-2）を hook 位置に置いた場合、
**exit code のみを判定するケース群**は許可群と block 群の pass/fail が入れ替わる。
一方 `TestStderrContract` は stderr の文言・サニタイズまで検証するため、
**両スタブとも赤のままが正常**（always-exit-0 は exit 判定で赤、always-exit-2 は
stderr が空で赤）。同クラスが red-red 許可リストである。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "report_path_guard.py"

# 対象 9 prefix（契約の逐語）
PREFIXES = (
    "requirements-report-",
    "architecture-report-",
    "plan-report-",
    "design-review-report-",
    "code-review-report-",
    "security-review-report-",
    "test-report-",
    "debug-analysis-",
    "debug-needed-",
)

# strict-4（検査 3 の適用対象）と非 strict-4（非適用）
STRICT4_PREFIXES = PREFIXES[:4]
NON_STRICT4_PREFIXES = PREFIXES[4:]

# timestamp 形式の実体（YYYYMMDD-HHMMSS）
TS = "20260812-120000"

# 環境から必ず取り除く変数。CLAUDE_PROJECT_DIR は Claude Code 実行時に
# 外側から設定されうるため、明示しないテストでは root B を無効化しておく。
_STRIPPED_ENV = ("C3_REPORT_GUARD_DISABLE", "CLAUDE_PROJECT_DIR")


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------


def _payload(tool_name: str, file_path: str) -> dict:
    return {"tool_name": tool_name, "tool_input": {"file_path": file_path}}


def _run_hook(
    payload: dict | None,
    *,
    cwd: Path | str,
    env: dict | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    """hook を subprocess で起動する。

    `cwd` は擬似リポジトリのルート（root A の導出元）。`input_text` を渡した場合は
    payload を無視してその文字列をそのまま stdin へ流す（壊れた JSON の検証用）。
    """
    if not HOOK_PATH.is_file():
        pytest.fail(
            "hook 未実装のため実行できない（Red フェーズの期待どおりの失敗理由）: "
            f"{ascii(str(HOOK_PATH))} が存在しない"
        )
    run_env = os.environ.copy()
    for key in _STRIPPED_ENV:
        run_env.pop(key, None)
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload) if input_text is None else input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd),
        env=run_env,
    )


def _make_repo(base: Path, name: str = "repoA", *, with_reports: bool = True) -> Path:
    """`<base>/<name>/.claude/reports` を持つ擬似リポジトリを作って realpath を返す。

    Windows の 8.3 短縮名・シンボリックな tmp 配置で `samefile` 判定が揺れないよう、
    返すパスは必ず realpath 化する。
    """
    root = Path(os.path.realpath(base)) / name
    if with_reports:
        (root / ".claude" / "reports").mkdir(parents=True)
    else:
        root.mkdir(parents=True)
    return Path(os.path.realpath(root))


def _reports(root: Path) -> Path:
    return root / ".claude" / "reports"


def _detail(result: subprocess.CompletedProcess, target: str) -> str:
    return (
        f"exit={result.returncode} target={ascii(target)} "
        f"stderr={ascii(result.stderr)}"
    )


@pytest.fixture
def dir_link():
    """ディレクトリリンクを作るファクトリ（NTFS ジャンクション優先・symlink 代替）。

    リンク偽装の検証にはディレクトリリンクが要る。開発機（Windows）では
    `_winapi.CreateJunction` が管理者権限なしで成功することを既存テスト
    （tests/test_archive_reports.py の junction fixture）で実証済みのため
    これを一次手段とし、失敗したら `os.symlink` を試す。

    **ジャンクションと symlink の両方が作成不能な環境に限り skip する**
    （symlink 不能だけでは skip しない）。skip 理由には試行した API と実際の例外を
    必ず載せる（「作成を試みて失敗した」ことが分かる形にする）。

    後始末は `os.rmdir(link)` でリンク自体を先に外す。ジャンクションを張ったまま
    一時ディレクトリを消すとリンク先を巻き込んで削除しうるため。
    """
    created: list[Path] = []

    def _create(link: Path, target: Path) -> str:
        target.mkdir(parents=True, exist_ok=True)
        link.parent.mkdir(parents=True, exist_ok=True)
        reasons: list[str] = []

        try:
            import _winapi
        except ImportError as exc:
            reasons.append(f"_winapi import 不可（非 Windows）: {type(exc).__name__}: {exc}")
        else:
            creator = getattr(_winapi, "CreateJunction", None)
            if creator is None:
                reasons.append("_winapi.CreateJunction が存在しない")
            else:
                try:
                    creator(str(target), str(link))
                except (OSError, NotImplementedError, ValueError) as exc:
                    reasons.append(f"CreateJunction 失敗: {type(exc).__name__}: {exc}")
                else:
                    created.append(link)
                    return "junction"

        try:
            os.symlink(str(target), str(link), target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError) as exc:
            reasons.append(f"os.symlink 失敗: {type(exc).__name__}: {exc}")
        else:
            created.append(link)
            return "symlink"

        pytest.skip(
            "ジャンクション・symlink の双方を試みたが作成できなかったため "
            f"リンク偽装を検証できない（{ascii(str(link))} -> {ascii(str(target))}）: "
            + " / ".join(reasons)
        )

    yield _create

    for link in reversed(created):
        try:
            os.rmdir(str(link))
        except OSError:
            pass


# ===========================================================================
# 性質 1: 発火条件
# ===========================================================================


class TestTriggerConditions:
    """対象 9 prefix で発火し、非対象 basename・Write 以外では沈黙する。"""

    @pytest.mark.parametrize("prefix", PREFIXES)
    def test_all_nine_prefixes_fire(self, tmp_path: Path, prefix: str) -> None:
        """9 prefix いずれも、reports 外（リポジトリ直下）への Write は block される。

        「発火したか」を許可経路ではなく違反経路で観測する（沈黙との差が出る）。
        """
        repo = _make_repo(tmp_path)
        target = str(repo / f"{prefix}{TS}.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 2, (
            f"対象 prefix {prefix!r} が発火していない: {_detail(result, target)}"
        )

    @pytest.mark.parametrize(
        "basename",
        [
            "notes.md",
            "README.md",
            f"report-{TS}.md",
            f"plan-summary-{TS}.md",
            f"testreport-{TS}.md",
            f"my-plan-report-{TS}.md",
            f"xtest-report-{TS}.md",
        ],
    )
    def test_non_target_basename_is_silent(self, tmp_path: Path, basename: str) -> None:
        """対象 prefix で始まらない basename は、reports 外でも沈黙する。"""
        repo = _make_repo(tmp_path)
        target = str(repo / basename)

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 0, (
            f"非対象 basename {basename!r} で発火した: {_detail(result, target)}"
        )
        assert result.stderr == "", (
            f"非対象 basename {basename!r} で stderr が出力された: {ascii(result.stderr)}"
        )

    @pytest.mark.parametrize(
        "tool_name", ["Edit", "Read", "NotebookEdit", "Bash", "Glob", "MultiEdit"]
    )
    def test_non_write_tool_is_silent(self, tmp_path: Path, tool_name: str) -> None:
        """Write 以外のツールは、対象 basename・違反パスでも沈黙する。"""
        repo = _make_repo(tmp_path)
        target = str(repo / f"plan-report-{TS}.md")

        result = _run_hook(_payload(tool_name, target), cwd=repo)

        assert result.returncode == 0, (
            f"tool_name={tool_name!r} で発火した: {_detail(result, target)}"
        )
        assert result.stderr == "", (
            f"tool_name={tool_name!r} で stderr が出力された: {ascii(result.stderr)}"
        )

    def test_uppercase_basename_fires_via_normalization(self, tmp_path: Path) -> None:
        """大文字混じり `Plan-Report-x.md` も正規化により発火する（reports 外 → block）。"""
        repo = _make_repo(tmp_path)
        target = str(repo / "Plan-Report-x.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 2, (
            f"大文字混じり basename が正規化されず素通りした: {_detail(result, target)}"
        )

    def test_uppercase_mixed_basename_in_reports_is_blocked(
        self, tmp_path: Path
    ) -> None:
        """[CR-NW-007 是正] 大文字混じり名は正規位置でも strict-4 形式違反として block。

        prefix 一致判定は正規化 basename（`Plan-Report-` も `plan-report-` として
        一致）で行うが、timestamp 形式判定は元の basename に対して行う
        （report_contract_check.py と統一したポリシー）。`Plan-Report-...MD` は
        元の basename が `plan-report-` リテラル・`.md` リテラルにフルマッチしないため
        timestamp 形式とはみなされず、strict-4 prefix として検査 3 で block される。

        旧実装は prefix 判定・形式判定の両方を正規化 basename に対して行っていたため
        このケースを許可していたが、これは report_contract_check.py（形式判定を元の
        basename に対して行い、同じ basename を warn 対象とする）との判定不一致
        （同一ファイルに矛盾した判定を返す）そのものであり、CR-NW-007 の是正対象。
        """
        repo = _make_repo(tmp_path)
        target = str(_reports(repo) / f"Plan-Report-{TS}.MD")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 2, (
            f"大文字混じり timestamp 類似名が strict-4 形式違反として block されなかった: "
            f"{_detail(result, target)}"
        )


# ===========================================================================
# 性質 2: 封じ込め（root A / root B）
# ===========================================================================


class TestContainment:
    """許可 root 直下のみを許可し、それ以外を fail-closed で block する。"""

    def test_allow_canonical_path_under_root_a(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        target = str(_reports(repo) / "test-report-test-guard.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 0, (
            f"root A 直下の正規パスが block された: {_detail(result, target)}"
        )
        assert result.stderr == ""

    def test_block_repo_root(self, tmp_path: Path) -> None:
        """リポジトリ直下（reports 外）は block。"""
        repo = _make_repo(tmp_path)
        target = str(repo / f"test-report-{TS}.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 2, _detail(result, target)

    def test_block_archive_subdirectory(self, tmp_path: Path) -> None:
        """`.claude/reports/archive/` 配下は block（第 1 層の親名判定で外れる）。

        親ディレクトリは実在させる（「親が無いから block」ではないことを担保する）。
        """
        repo = _make_repo(tmp_path)
        archive = _reports(repo) / "archive"
        archive.mkdir()
        target = str(archive / "test-report-test-guard.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 2, _detail(result, target)

    def test_block_docs_directory(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        docs = repo / "docs"
        docs.mkdir()
        target = str(docs / f"plan-report-{TS}.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 2, _detail(result, target)

    def test_block_other_claude_reports_outside_cwd(self, tmp_path: Path) -> None:
        """cwd 外の別の `.claude/reports` は block。

        第 1 層（親 = reports・祖父 = .claude）は通るため、第 2 層（samefile）だけが
        block の根拠になるケース。
        """
        repo = _make_repo(tmp_path)
        fake = _make_repo(tmp_path, "fake_root")
        target = str(_reports(fake) / "test-report-test-guard.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 2, (
            f"cwd 外の別 .claude/reports が許可された: {_detail(result, target)}"
        )

    def test_allow_root_b_when_env_set(self, tmp_path: Path) -> None:
        """CLAUDE_PROJECT_DIR を明示した別リポジトリの reports 直下は許可（root B）。"""
        repo = _make_repo(tmp_path)
        repo_b = _make_repo(tmp_path, "repoB")
        target = str(_reports(repo_b) / "test-report-test-guard.md")

        result = _run_hook(
            _payload("Write", target),
            cwd=repo,
            env={"CLAUDE_PROJECT_DIR": str(repo_b)},
        )

        assert result.returncode == 0, (
            f"root B 直下が block された: {_detail(result, target)}"
        )
        assert result.stderr == ""

    def test_allow_root_a_when_env_set_to_other_repo(self, tmp_path: Path) -> None:
        """root B を設定しても root A（cwd 側）は一次の許可 root として機能し続ける。"""
        repo = _make_repo(tmp_path)
        repo_b = _make_repo(tmp_path, "repoB")
        target = str(_reports(repo) / "test-report-test-guard.md")

        result = _run_hook(
            _payload("Write", target),
            cwd=repo,
            env={"CLAUDE_PROJECT_DIR": str(repo_b)},
        )

        assert result.returncode == 0, _detail(result, target)

    def test_block_root_b_path_when_env_absent(self, tmp_path: Path) -> None:
        """env 未設定なら root B は成立せず、別リポジトリの reports は block（縮退）。"""
        repo = _make_repo(tmp_path)
        repo_b = _make_repo(tmp_path, "repoB")
        target = str(_reports(repo_b) / "test-report-test-guard.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 2, (
            f"env 未設定なのに root B が成立した: {_detail(result, target)}"
        )

    def test_allow_root_a_when_env_absent(self, tmp_path: Path) -> None:
        """env 未設定でも root A 判定は機能する（縮退しても block しない）。"""
        repo = _make_repo(tmp_path)
        target = str(_reports(repo) / "test-report-test-guard.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 0, _detail(result, target)

    def test_allow_root_a_when_env_empty(self, tmp_path: Path) -> None:
        """env が空文字なら root A のみで判定する（空を root として誤解決しない）。"""
        repo = _make_repo(tmp_path)
        target = str(_reports(repo) / "test-report-test-guard.md")

        result = _run_hook(
            _payload("Write", target), cwd=repo, env={"CLAUDE_PROJECT_DIR": ""}
        )

        assert result.returncode == 0, _detail(result, target)

    def test_block_other_repo_when_env_empty(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        repo_b = _make_repo(tmp_path, "repoB")
        target = str(_reports(repo_b) / "test-report-test-guard.md")

        result = _run_hook(
            _payload("Write", target), cwd=repo, env={"CLAUDE_PROJECT_DIR": ""}
        )

        assert result.returncode == 2, _detail(result, target)

    def test_block_when_reports_root_missing(self, tmp_path: Path) -> None:
        """許可 root も親も不在なら block（fail-closed）。"""
        repo = _make_repo(tmp_path, with_reports=False)
        target = str(_reports(repo) / "test-report-test-guard.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 2, (
            f"root 不在なのに許可された（fail-open になっている）: {_detail(result, target)}"
        )


# ===========================================================================
# 性質 3: トラバーサル・リンク偽装
# ===========================================================================


class TestTraversalAndLinkSpoofing:
    """`..` による脱出とリンク偽装を block し、免責ケースだけを許可する。"""

    @pytest.mark.parametrize(
        "suffix_parts",
        [
            ("..", f"test-report-{TS}.md"),
            ("..", "..", f"test-report-{TS}.md"),
            ("..", "..", "..", f"plan-report-{TS}.md"),
        ],
    )
    def test_block_dotdot_escape(self, tmp_path: Path, suffix_parts: tuple) -> None:
        """`..` を含み実体が許可 root 外へ脱出するパスは block。"""
        repo = _make_repo(tmp_path)
        target = str(_reports(repo).joinpath(*suffix_parts))

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 2, (
            f"`..` による脱出が許可された: {_detail(result, target)}"
        )

    def test_block_link_spoofed_reports_outside_allowed_root(
        self, tmp_path: Path, dir_link
    ) -> None:
        """許可 root 以外の位置に張ったリンク経由の `.claude/reports` は block。

        `cwd/fake/.claude/reports` を外部の `.claude/reports` 構造へ張る。
        第 1 層の名前判定（親 = reports・祖父 = .claude）は通るため、
        realpath 後の samefile 判定だけが block の根拠になる。
        """
        repo = _make_repo(tmp_path)
        outside = _make_repo(tmp_path, "outside")
        link = repo / "fake" / ".claude" / "reports"
        method = dir_link(link, _reports(outside))
        target = str(link / "test-report-test-guard.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 2, (
            f"リンク偽装（{method}）された reports が許可された: {_detail(result, target)}"
        )

    def test_allow_when_allowed_root_itself_is_a_link(
        self, tmp_path: Path, dir_link
    ) -> None:
        """[免責・対の沈黙ケース] 許可 root 自体がリンクに差し替えられた場合は許可。

        root A の `.claude/reports` そのものを外部へ張り替えると、expected（root A の
        realpath）と resolved（target 親の realpath）が同一実体になるため block しない。
        設計上の脅威モデル外であり **allow が正しい**。過剰ブロックへ倒れていないことを
        固定する。
        """
        repo = _make_repo(tmp_path, with_reports=False)
        outside = _make_repo(tmp_path, "outside")
        (repo / ".claude").mkdir(parents=True, exist_ok=True)
        method = dir_link(_reports(repo), _reports(outside))
        target = str(_reports(repo) / "test-report-test-guard.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 0, (
            f"免責ケース（許可 root 自体が {method}）が block された: "
            f"{_detail(result, target)}"
        )


# ===========================================================================
# 性質 4: 相対パス
# ===========================================================================


class TestRelativePath:
    """相対パスは cwd と結合してから判定される。"""

    @pytest.mark.parametrize(
        "rel",
        [
            ".claude/reports/test-report-test-guard.md",
            "./.claude/reports/test-report-test-guard.md",
        ],
    )
    def test_allow_relative_path_under_reports(self, tmp_path: Path, rel: str) -> None:
        repo = _make_repo(tmp_path)

        result = _run_hook(_payload("Write", rel), cwd=repo)

        assert result.returncode == 0, (
            f"相対パスの正規位置が block された: {_detail(result, rel)}"
        )
        assert result.stderr == ""

    def test_block_relative_path_outside_reports(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)

        result = _run_hook(_payload("Write", f"plan-report-{TS}.md"), cwd=repo)

        assert result.returncode == 2, _detail(result, f"plan-report-{TS}.md")


# ===========================================================================
# 性質 5: 上書き検査（検査 2）の形式分岐
# ===========================================================================


class TestOverwriteCheck:
    """timestamp 形式の既存のみ block する。task_id 形式の既存上書きは許可。"""

    @pytest.mark.parametrize("prefix", ["test-report-", "plan-report-"])
    def test_block_existing_timestamp_file(self, tmp_path: Path, prefix: str) -> None:
        repo = _make_repo(tmp_path)
        existing = _reports(repo) / f"{prefix}{TS}.md"
        existing.write_text("既存レポート\n", encoding="utf-8")

        result = _run_hook(_payload("Write", str(existing)), cwd=repo)

        assert result.returncode == 2, (
            f"timestamp 形式の既存ファイル上書きが許可された: {_detail(result, str(existing))}"
        )

    @pytest.mark.parametrize(
        "basename",
        [
            "test-report-test-guard.md",
            "code-review-report-abc.md",
            "debug-analysis-issue42.md",
        ],
    )
    def test_allow_existing_task_id_file(self, tmp_path: Path, basename: str) -> None:
        """task_id 形式（非 timestamp）の既存上書きは許可（再実行での更新経路）。"""
        repo = _make_repo(tmp_path)
        existing = _reports(repo) / basename
        existing.write_text("既存レポート\n", encoding="utf-8")

        result = _run_hook(_payload("Write", str(existing)), cwd=repo)

        assert result.returncode == 0, (
            f"task_id 形式の既存上書きが block された: {_detail(result, str(existing))}"
        )

    @pytest.mark.parametrize("prefix", ["test-report-", "plan-report-"])
    def test_allow_timestamp_file_when_absent(self, tmp_path: Path, prefix: str) -> None:
        repo = _make_repo(tmp_path)
        target = str(_reports(repo) / f"{prefix}{TS}.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 0, (
            f"未存在の timestamp 形式が block された: {_detail(result, target)}"
        )

    def test_allow_timestamp_when_same_name_exists_in_other_directory(
        self, tmp_path: Path
    ) -> None:
        """既存判定は「書き込み先実体」で行う（別ディレクトリの同名は無関係）。"""
        repo = _make_repo(tmp_path)
        other = repo / "elsewhere"
        other.mkdir()
        (other / f"test-report-{TS}.md").write_text("別物\n", encoding="utf-8")
        target = str(_reports(repo) / f"test-report-{TS}.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 0, _detail(result, target)


# ===========================================================================
# 性質 6: strict-4 形式 block（検査 3）
# ===========================================================================


class TestStrict4Format:
    """strict-4 prefix は timestamp 形式にフルマッチしない限り新規でも block。"""

    @pytest.mark.parametrize("prefix", STRICT4_PREFIXES)
    def test_block_non_timestamp_for_strict4(self, tmp_path: Path, prefix: str) -> None:
        repo = _make_repo(tmp_path)
        target = str(_reports(repo) / f"{prefix}APPROVED.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 2, (
            f"strict-4 prefix {prefix!r} の非 timestamp 名が許可された: "
            f"{_detail(result, target)}"
        )

    @pytest.mark.parametrize("prefix", STRICT4_PREFIXES)
    def test_allow_timestamp_for_strict4(self, tmp_path: Path, prefix: str) -> None:
        repo = _make_repo(tmp_path)
        target = str(_reports(repo) / f"{prefix}{TS}.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 0, (
            f"strict-4 prefix {prefix!r} の timestamp 名が block された: "
            f"{_detail(result, target)}"
        )

    @pytest.mark.parametrize("prefix", NON_STRICT4_PREFIXES)
    def test_allow_non_timestamp_for_non_strict4(
        self, tmp_path: Path, prefix: str
    ) -> None:
        """CR / SR / test / debug 系には検査 3 を適用しない（task_id 命名が正規）。"""
        repo = _make_repo(tmp_path)
        target = str(_reports(repo) / f"{prefix}abc.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 0, (
            f"非 strict-4 prefix {prefix!r} に検査 3 が適用された: "
            f"{_detail(result, target)}"
        )

    @pytest.mark.parametrize(
        "basename",
        [
            f"plan-report-2026081-120000.md",  # 日付 7 桁
            f"plan-report-202608120-120000.md",  # 日付 9 桁
            f"plan-report-20260812-12000.md",  # 時刻 5 桁
            f"plan-report-20260812-1200000.md",  # 時刻 7 桁
            f"plan-report-20260812_120000.md",  # 区切りがアンダースコア
            f"plan-report-20260812-120000-v2.md",  # 余剰サフィックス
            f"plan-report-20260812-120000.txt",  # 拡張子違い
            f"plan-report-{TS}.md.bak",  # 二重拡張子
            "plan-report-２０２６０８１２-"
            "１２００００.md",  # 全角数字（re.ASCII で不一致）
        ],
    )
    def test_block_timestamp_lookalikes_for_strict4(
        self, tmp_path: Path, basename: str
    ) -> None:
        """timestamp 形式に「似ているが一致しない」名は strict-4 では block。

        全角数字ケースは `re.ASCII` 指定の有無を分ける（`\\d` が Unicode 数字を
        拾うと素通りする）。
        """
        repo = _make_repo(tmp_path)
        target = str(_reports(repo) / basename)

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 2, (
            f"timestamp 類似名 {ascii(basename)} が strict-4 で許可された: "
            f"{_detail(result, target)}"
        )

    def test_allow_timestamp_lookalike_for_non_strict4(self, tmp_path: Path) -> None:
        """同じ類似名でも非 strict-4 なら許可（検査 3 の適用範囲の裏取り）。"""
        repo = _make_repo(tmp_path)
        target = str(_reports(repo) / "test-report-20260812_120000.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 0, _detail(result, target)

    def test_block_strict4_non_timestamp_even_outside_reports(
        self, tmp_path: Path
    ) -> None:
        """封じ込め違反と形式違反が重なっても block（どちらかで落ちる）。"""
        repo = _make_repo(tmp_path)
        target = str(repo / "plan-report-APPROVED.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 2, _detail(result, target)

    def test_block_uppercase_extension_for_strict4(self, tmp_path: Path) -> None:
        """[CR-NW-007 回帰] `.MD`（大文字拡張子）は strict-4 で timestamp 扱いされず block。

        report_contract_check.py の形式判定（元の basename に対するフルマッチ）と
        統一したポリシーの固定。正規化 basename で timestamp 扱いすると
        report_contract_check.py（同じファイルを warn 対象と判定）と矛盾する。
        """
        repo = _make_repo(tmp_path)
        target = str(_reports(repo) / f"requirements-report-{TS}.MD")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 2, (
            f"大文字拡張子 .MD が strict-4 で timestamp 扱い（許可）された: "
            f"{_detail(result, target)}"
        )

    def test_block_uppercase_prefix_letters_for_strict4(self, tmp_path: Path) -> None:
        """[CR-NW-007 回帰] prefix 大文字混じりは strict-4 で timestamp 扱いされず block。

        prefix 一致は正規化 basename（`Plan-Report-` も `plan-report-` として一致）で
        判定するが、timestamp 形式判定は元の basename に対して行うため、大文字混じり
        prefix は timestamp 形式とはみなされず検査 3 で block される。
        """
        repo = _make_repo(tmp_path)
        target = str(_reports(repo) / "Plan-Report-20260101-123456.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 2, (
            f"prefix 大文字混じり timestamp 類似名が strict-4 で許可された: "
            f"{_detail(result, target)}"
        )


# ===========================================================================
# 性質 7: fail-open / fail-closed
# ===========================================================================


class TestFailPolicy:
    """壊れた入力は fail-open、パス判定を壊す入力は fail-closed。"""

    @pytest.mark.parametrize(
        "raw",
        [
            "this is not json",
            "",
            "{",
            '{"tool_name": "Write", "tool_input": {',
        ],
    )
    def test_fail_open_on_broken_json(self, tmp_path: Path, raw: str) -> None:
        repo = _make_repo(tmp_path)

        result = _run_hook(None, cwd=repo, input_text=raw)

        assert result.returncode == 0, (
            f"壊れた stdin JSON で fail-open しなかった: exit={result.returncode} "
            f"stderr={ascii(result.stderr)}"
        )

    @pytest.mark.parametrize(
        "payload",
        [
            {"tool_name": "Write", "tool_input": {}},
            {"tool_name": "Write"},
            {"tool_input": {"file_path": "plan-report-APPROVED.md"}},
            {},
        ],
    )
    def test_fail_open_on_missing_keys(self, tmp_path: Path, payload: dict) -> None:
        repo = _make_repo(tmp_path)

        result = _run_hook(payload, cwd=repo)

        assert result.returncode == 0, (
            f"キー欠落で fail-open しなかった: exit={result.returncode} "
            f"stderr={ascii(result.stderr)}"
        )

    def test_fail_open_on_non_object_payload(self, tmp_path: Path) -> None:
        """JSON としては妥当だがオブジェクトでない payload も fail-open。"""
        repo = _make_repo(tmp_path)

        result = _run_hook(None, cwd=repo, input_text="[1, 2, 3]")

        assert result.returncode == 0, (
            f"非オブジェクト payload でクラッシュ／block した: exit={result.returncode} "
            f"stderr={ascii(result.stderr)}"
        )

    def test_fail_open_on_empty_file_path(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)

        result = _run_hook(_payload("Write", ""), cwd=repo)

        assert result.returncode == 0, (
            f"空 file_path で fail-open しなかった: exit={result.returncode}"
        )

    @pytest.mark.parametrize(
        "target_tmpl",
        [
            "{reports}/test-report-x\x00.md",
            "{reports}/\x00/test-report-x.md",
            "{repo}/\x00.claude/reports/test-report-x.md",
        ],
    )
    def test_fail_closed_on_nul_in_path(self, tmp_path: Path, target_tmpl: str) -> None:
        """NUL 混入パスは fail-closed で block（例外を握って素通りしない）。"""
        repo = _make_repo(tmp_path)
        target = target_tmpl.format(reports=str(_reports(repo)), repo=str(repo))

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 2, (
            f"NUL 混入パスが fail-open した: exit={result.returncode} "
            f"target={ascii(target)} stderr={ascii(result.stderr)}"
        )

    def test_non_strict4_with_control_chars_silently_allowed(self, tmp_path: Path) -> None:
        """[CR-T-001] 非 strict-4 prefix・C0 制御文字入り basename は exit 0・stderr 空で許可。

        非 strict-4 prefix（loose-3 / debug 系）は prefix・suffix 以外のケースを
        発火対象外とするため、C0 制御文字が混入しても hook 自体は対象外として
        沈黙する（形式検査は PostToolUse contract-check に委譲）。
        実ファイル作成は避けるため、文字列のみで検証。
        """
        repo = _make_repo(tmp_path)
        # loose-3 prefix + C0 制御文字（BEL: \x07）+ 未存在
        target = str(_reports(repo) / "test-report-bad\x07name.md")

        result = _run_hook(_payload("Write", target), cwd=repo)

        assert result.returncode == 0, (
            f"非 strict-4 で exit 0 でなかった: exit={result.returncode} "
            f"target={ascii(target)} stderr={ascii(result.stderr)}"
        )
        assert result.stderr == "", (
            f"非 strict-4 でも stderr が出た: {ascii(result.stderr)}"
        )


# ===========================================================================
# 性質 8: env バイパス
# ===========================================================================


class TestEnvBypass:
    """C3_REPORT_GUARD_DISABLE=1 で全検査をスキップする。"""

    def test_bypass_containment_violation(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        target = str(repo / f"plan-report-{TS}.md")

        result = _run_hook(
            _payload("Write", target), cwd=repo, env={"C3_REPORT_GUARD_DISABLE": "1"}
        )

        assert result.returncode == 0, _detail(result, target)
        assert result.stderr == ""

    def test_bypass_overwrite_violation(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        existing = _reports(repo) / f"test-report-{TS}.md"
        existing.write_text("既存\n", encoding="utf-8")

        result = _run_hook(
            _payload("Write", str(existing)),
            cwd=repo,
            env={"C3_REPORT_GUARD_DISABLE": "1"},
        )

        assert result.returncode == 0, _detail(result, str(existing))

    def test_bypass_strict4_format_violation(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        target = str(_reports(repo) / "plan-report-APPROVED.md")

        result = _run_hook(
            _payload("Write", target), cwd=repo, env={"C3_REPORT_GUARD_DISABLE": "1"}
        )

        assert result.returncode == 0, _detail(result, target)

    @pytest.mark.parametrize("value", ["0", "true", "yes", "", "11"])
    def test_other_env_values_do_not_bypass(self, tmp_path: Path, value: str) -> None:
        """`1` 以外の値では無効化されず block する（誤設定の安全側挙動）。"""
        repo = _make_repo(tmp_path)
        target = str(repo / f"plan-report-{TS}.md")

        result = _run_hook(
            _payload("Write", target),
            cwd=repo,
            env={"C3_REPORT_GUARD_DISABLE": value},
        )

        assert result.returncode == 2, (
            f"C3_REPORT_GUARD_DISABLE={value!r} でバイパスされた: {_detail(result, target)}"
        )


# ===========================================================================
# 性質 9: block 時 stderr の内容（違反種別・回復誘導・サニタイズ）
#
# **red-red 許可リスト**: 本クラスは exit code に加えて stderr の文言まで検証するため、
# do-nothing スタブ 2 種（always-exit-0 / always-exit-2）ではいずれも赤のままが正常。
# ===========================================================================


class TestStderrContract:
    """block 時の stderr が違反種別・basename・回復誘導を含み、サニタイズされる。"""

    def test_containment_violation_message(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        basename = f"plan-report-{TS}.md"
        target = str(repo / basename)

        result = _run_hook(_payload("Write", target), cwd=repo)
        stderr = result.stderr

        assert result.returncode == 2, _detail(result, target)
        assert "封じ込め" in stderr, f"違反種別（封じ込め）が示されていない: {ascii(stderr)}"
        assert basename in stderr, f"対象 basename が示されていない: {ascii(stderr)}"
        assert ".claude/reports" in stderr, (
            f"封じ込め違反の回復誘導（書き先の修正先）が無い: {ascii(stderr)}"
        )
        assert "cd" in stderr, (
            f"封じ込め違反の回復誘導（cwd がプロジェクトルート外なら cd）が無い: "
            f"{ascii(stderr)}"
        )
        assert "Edit" not in stderr, f"Edit に言及している: {ascii(stderr)}"

    def test_overwrite_violation_message(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        basename = f"test-report-{TS}.md"
        existing = _reports(repo) / basename
        existing.write_text("既存\n", encoding="utf-8")

        result = _run_hook(_payload("Write", str(existing)), cwd=repo)
        stderr = result.stderr

        assert result.returncode == 2, _detail(result, str(existing))
        assert "上書き" in stderr, f"違反種別（上書き）が示されていない: {ascii(stderr)}"
        assert basename in stderr, f"対象 basename が示されていない: {ascii(stderr)}"
        assert "report-timestamp" in stderr, (
            f"上書き違反の回復誘導（report-timestamp skill で新規採番）が無い: "
            f"{ascii(stderr)}"
        )
        assert "Edit" not in stderr, f"Edit に言及している: {ascii(stderr)}"

    def test_format_violation_message(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        basename = "plan-report-APPROVED.md"
        target = str(_reports(repo) / basename)

        result = _run_hook(_payload("Write", target), cwd=repo)
        stderr = result.stderr

        assert result.returncode == 2, _detail(result, target)
        assert "形式" in stderr, f"違反種別（形式）が示されていない: {ascii(stderr)}"
        assert basename in stderr, f"対象 basename が示されていない: {ascii(stderr)}"
        assert "report-timestamp" in stderr, (
            f"形式違反の回復誘導（report-timestamp skill で新規採番）が無い: "
            f"{ascii(stderr)}"
        )
        assert "Edit" not in stderr, f"Edit に言及している: {ascii(stderr)}"

    def test_stderr_is_sanitized(self, tmp_path: Path) -> None:
        """C0 / C1 / 双方向制御 / ゼロ幅を含む basename が stderr へ素通りしない。

        除去集合は `_hook_utils.sanitize_for_terminal` に従う。NUL は fail-closed 経路
        （性質 7）で扱うため、ここでは ESC / CSI / 改行 / RLO / ZWSP を使う。
        """
        repo = _make_repo(tmp_path)
        hostile = (
            "plan-report-\x1b[31mRED\x9b0m\n[FAKE WARNING]"
            "‮evil​-" + TS + ".md"
        )
        target = str(repo / hostile)

        result = _run_hook(_payload("Write", target), cwd=repo)
        stderr = result.stderr

        assert result.returncode == 2, _detail(result, target)
        for bad, label in (
            ("\x1b", "ESC"),
            ("\x9b", "CSI"),
            ("\n[FAKE WARNING]", "改行による偽警告行"),
            ("‮", "RLO"),
            ("​", "ZWSP"),
        ):
            assert bad not in stderr, (
                f"{label} が stderr に残っている（サニタイズ未適用）: {ascii(stderr)}"
            )
        assert "封じ込め" in stderr, f"違反種別が示されていない: {ascii(stderr)}"
        assert "RED" in stderr, (
            f"サニタイズ後も残るべき可視部分まで欠落している: {ascii(stderr)}"
        )


# ===========================================================================
# 回帰ガード群（現時点で緑・緑のまま維持する）
#
# 本 hook の正規化（norm_component 相当）とサニタイズ（sanitize_for_terminal）は
# 既存の共有ヘルパーの挙動に乗る。ここが黙って狭まると hook の保証も黙って弱まるため、
# 依存している性質を固定する。**意図した緑であり「最初から Pass するテストは修正する」
# の対象外**。
# ===========================================================================


class TestSharedHelperRegressionGuards:
    """`_hook_utils` の正規化・サニタイズ挙動を凍結する。"""

    def test_hook_utils_exports_required_helpers(self) -> None:
        from _hook_utils import norm_component, sanitize_for_terminal

        assert callable(norm_component)
        assert callable(sanitize_for_terminal)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Plan-Report-X.MD", "plan-report-x.md"),
            ("TEST-REPORT-abc.md ", "test-report-abc.md"),
            ("test-report-abc.md.", "test-report-abc.md"),
            ("test-report-abc.md. .", "test-report-abc.md"),
            ("reports", "reports"),
            (".Claude", ".claude"),
        ],
    )
    def test_norm_component_lowercases_and_strips(
        self, raw: str, expected: str
    ) -> None:
        """basename / ディレクトリ名の正規化が lower + 末尾ドット/スペース除去であること。"""
        from _hook_utils import norm_component

        assert norm_component(raw) == expected

    @pytest.mark.parametrize(
        "hostile",
        ["\x00", "\x1b", "\x9b", "\n", "\r", "\x7f", "‮", "​", "﻿"],
    )
    def test_sanitize_removes_terminal_hostile_characters(self, hostile: str) -> None:
        """stderr へ載せる前に除去されるべき文字集合を凍結する。"""
        from _hook_utils import sanitize_for_terminal

        assert hostile not in sanitize_for_terminal(f"plan-report{hostile}-{TS}.md")

    def test_sanitize_keeps_visible_text(self) -> None:
        """可視文字（ASCII・日本語）は落とさない（過剰除去の回帰ガード）。"""
        from _hook_utils import sanitize_for_terminal

        text = f"封じ込め違反: plan-report-{TS}.md"
        assert sanitize_for_terminal(text) == text
