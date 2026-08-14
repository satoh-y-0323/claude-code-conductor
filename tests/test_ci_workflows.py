"""
tests/test_ci_workflows.py

.github/workflows/test.yml が dev script を実行する CI job を持ち続けることを守るテスト。

対象は 2 系統:

1. `scripts/check_deletions.py --check` を実行する job（＋ checkout の `fetch-depth: 0`）
2. `scripts/verify_wheel.py` を実行する job（`wheel-check`。P6・**Red フェーズでは不在**）

2 の凍結項目は 4 つ（architecture 改訂 3 ADR-5 改訂 2）:
(1) job の存在 (2) run に `--wheel` を含まない（公開等価＝実ビルド経路の凍結）
(3) `build`（PyPA build）のインストールステップ (4) `permissions == {contents: read}`。
「空の緑」対策（注入対照の実働実証）は script 内蔵のため YAML では測らない
（tests/test_verify_wheel.py の P8 が凍結する）。

## なぜ fetch-depth: 0 が本質か

`scripts/check_deletions.py` は内部で以下の git コマンドに依存する
(scripts/check_deletions.py:92-98, 121-127):
  - `git describe --tags --match v* --abbrev=0`  （直近 v タグの取得）
  - `git diff --name-status <tag>..HEAD -- .claude/`  （タグ以降の削除検出）

GitHub Actions の `actions/checkout@v5` は既定で `fetch-depth: 1`（shallow clone、
タグ・履歴を含まない単一コミットのみ）を使う。この既定のままだと:
  - `git describe --tags` はタグを見つけられず失敗する
  - check_deletions.py の `_get_latest_vtag()` は None を返し、
    `main()` は「チェック対象のタグが見つかりませんでした」で **exit 0** に早期 return する
    (scripts/check_deletions.py:203-206)

つまり fetch-depth: 0 を欠いた CI 上の check_deletions.py --check は、
未記載の削除が実際にあっても常に exit 0 = 緑になる「空の緑」。
CI が緑のままなので人間には見えない退行になる。

## PyYAML の `on:` キー沼（本テストとは別件・将来の検査者向けメモ）

GitHub Actions の `on:` セクションを検査するテストを将来書く場合の罠:
YAML 1.1 のブール解決により、PyYAML の `yaml.safe_load()` は `on:` キーを
文字列 `"on"` ではなく **ブール値 `True`** としてパースする。
2026-08-09 に本ファイル作成時、`.github/workflows/test.yml` に対して実際に
`yaml.safe_load()` を実行し確認済み（実測、要約からの転記ではない）:

    >>> data = yaml.safe_load(open(".github/workflows/test.yml", encoding="utf-8"))
    >>> [type(k) for k in data.keys()]
    [<class 'str'>, <class 'bool'>, <class 'str'>]   # 'name', True, 'jobs'
    >>> "on" in data
    False
    >>> True in data
    True

`data.get("on", {})` と書くと常に `{}` が返り、`on:` の中身を何も見ずに
恒真判定（空の緑）になる。`on:` を検査するテストを書く場合は `data.get(True)` /
`data[True]` を使うこと（本ファイルでは `on:` を検査しないため対象外だが、
対象ファイルは test.yml であり、キー構成の異なる docs.yml と混同しないよう注意）。

## C3_CI_WORKFLOW_DIR

ワークフロー YAML の読み込み先ディレクトリは環境変数 `C3_CI_WORKFLOW_DIR` で
差し替え可能にしてある。後段の「本テストが本当に検知力を持つか」を確かめる
スタブ検査（壊れた test.yml を用意して本テストが red になることを確認する）が、
本番の `.github/workflows/test.yml` を書き換えずに済むようにするための仕組み。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

_CHECK_DELETIONS_MARKER = "check_deletions.py"
_CHECK_FLAG_MARKER = "--check"


def _workflow_dir() -> Path:
    """ワークフロー YAML を読むディレクトリを返す。

    環境変数 C3_CI_WORKFLOW_DIR が設定されていればそれを優先する
    （スタブ検査が本番 .github/workflows/ を汚さずに検知力を測るための差し替え口）。
    """
    override = os.environ.get("C3_CI_WORKFLOW_DIR")
    if override:
        return Path(override)
    return _DEFAULT_WORKFLOW_DIR


def _load_workflow(filename: str) -> dict:
    path = _workflow_dir() / filename
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _find_job_running_check_deletions(workflow: dict) -> tuple[str | None, dict | None]:
    """workflow 内で check_deletions.py --check を実行するステップを持つ job を探す。

    job 名をハードコードせず、ステップの `run:` 文字列を走査して特定する
    （job 名が変わっても壊れないようにするため）。

    Returns:
        (job_name, job_def) のタプル。見つからなければ (None, None)。
    """
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return None, None

    for job_name, job_def in jobs.items():
        if not isinstance(job_def, dict):
            continue
        steps = job_def.get("steps", [])
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            run = step.get("run", "")
            if not isinstance(run, str):
                continue
            if _CHECK_DELETIONS_MARKER in run and _CHECK_FLAG_MARKER in run:
                return job_name, job_def

    return None, None


def test_test_yml_has_check_deletions_job():
    """test.yml に scripts/check_deletions.py --check を実行する job が存在すること。

    Red フェーズでは存在しない想定（現行 test.yml は未対応）。
    """
    workflow = _load_workflow("test.yml")
    job_name, _job_def = _find_job_running_check_deletions(workflow)

    assert job_name is not None, (
        "test.yml に 'python scripts/check_deletions.py --check' を実行する job が"
        " 見つからない。CLAUDE.md §7（リリース前 削除追記漏れチェック）を CI で"
        " 機械強制するには、専用 job のステップに check_deletions.py --check の"
        " 実行を追加する必要がある。"
    )


def test_test_yml_check_deletions_job_checkout_has_fetch_depth_zero():
    """check_deletions.py --check を実行する job の checkout が fetch-depth: 0 を持つこと。

    fetch-depth: 0 が無いと、既定の shallow clone (fetch-depth: 1) では
    git describe --tags がタグを見つけられず、check_deletions.py が
    「タグが見つからない」で常に exit 0 に早期 return する（空の緑）。
    詳細はモジュール docstring 「なぜ fetch-depth: 0 が本質か」を参照。

    Red フェーズでは job 自体が存在しないため、まずその事実で失敗する
    （fetch-depth の検査に進めない＝上のテストと合わせて検知力があることを示す）。
    """
    workflow = _load_workflow("test.yml")
    job_name, job_def = _find_job_running_check_deletions(workflow)

    assert job_name is not None, (
        "check_deletions.py --check を実行する job が test.yml に無いため、"
        " fetch-depth の検査に進めない（test_test_yml_has_check_deletions_job を先に解消すること）。"
    )

    steps = job_def.get("steps", [])
    checkout_steps = [
        step
        for step in steps
        if isinstance(step, dict) and str(step.get("uses", "")).startswith("actions/checkout")
    ]
    assert checkout_steps, (
        f"job '{job_name}' に actions/checkout ステップが見つからない。"
        " git describe / git diff を実行する job には checkout が必須。"
    )

    checkout_step = checkout_steps[0]
    with_block = checkout_step.get("with", {}) or {}
    fetch_depth = with_block.get("fetch-depth")

    assert fetch_depth == 0, (
        f"job '{job_name}' の actions/checkout ステップに fetch-depth: 0 が無い"
        f"（実際の値: {fetch_depth!r}）。既定の fetch-depth: 1 (shallow clone) では"
        " git describe --tags がタグ履歴を取得できず、check_deletions.py --check が"
        " 常に「タグが見つからない」で exit 0 に早期 return する（空の緑）。"
    )


def test_test_yml_check_deletions_job_declares_permissions():
    """check_deletions.py --check を実行する job が permissions を明示的に宣言すること。

    job レベルで permissions を明示しない場合、GITHUB_TOKEN はリポジトリ設定に
    依存した既定権限（多くの場合 read/write 全パーミッション）で動作する。
    本 job は checkout して読み取り専用の削除検出を行うだけなので、
    最小権限原則に従い `contents: read` に絞る必要がある。

    Red フェーズでは permissions キー自体が test.yml に無いため失敗する。
    """
    workflow = _load_workflow("test.yml")
    job_name, job_def = _find_job_running_check_deletions(workflow)

    assert job_name is not None, (
        "check_deletions.py --check を実行する job が test.yml に無いため、"
        " permissions の検査に進めない（test_test_yml_has_check_deletions_job を先に解消すること）。"
    )

    permissions = job_def.get("permissions")
    assert permissions is not None, (
        f"job '{job_name}' に permissions が宣言されていない。"
        " 既定の GITHUB_TOKEN 権限（リポジトリ設定依存）で動作してしまうため、"
        " 最小権限原則に従い job レベルで permissions を明示する必要がある"
        "（例: permissions: {contents: read}）。"
    )

    assert permissions == {"contents": "read"}, (
        f"job '{job_name}' の permissions が想定値と一致しない（実際の値: {permissions!r}）。"
        " 本 job は checkout して読み取り専用の削除検出を行うだけなので、"
        " permissions: {contents: read} を想定している。"
    )


# ---------------------------------------------------------------------------
# P6: wheel-check job（scripts/verify_wheel.py）の静的検査
#
# Red フェーズでは job 自体が test.yml に無いため、4 本とも「job が無い」で失敗する。
# ---------------------------------------------------------------------------

_VERIFY_WHEEL_MARKER = "verify_wheel.py"
_WHEEL_FLAG_MARKER = "--wheel"
# PyPA build の導入ステップ（`python -m pip install build` 等）。
# `pip install -e .` と同一 job 内の別ステップでも良いよう、run 文字列単位で探す。
_BUILD_INSTALL_RE = re.compile(r"pip\s+install\b[^\n]*\bbuild\b")


def _iter_step_runs(job_def: dict) -> list[str]:
    """job の各ステップの `run:` 文字列を列挙する。"""
    runs: list[str] = []
    steps = job_def.get("steps", [])
    if not isinstance(steps, list):
        return runs
    for step in steps:
        if not isinstance(step, dict):
            continue
        run = step.get("run", "")
        if isinstance(run, str) and run:
            runs.append(run)
    return runs


def _find_job_running_marker(workflow: dict, marker: str) -> tuple[str | None, dict | None]:
    """`run:` 文字列に `marker` を含むステップを持つ job を探す（job 名に依存しない）。"""
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return None, None

    for job_name, job_def in jobs.items():
        if not isinstance(job_def, dict):
            continue
        if any(marker in run for run in _iter_step_runs(job_def)):
            return job_name, job_def

    return None, None


def _require_wheel_check_job() -> tuple[str, dict]:
    """wheel-check job を取得する。無ければ失敗させる（4 本共通の前提）。"""
    workflow = _load_workflow("test.yml")
    job_name, job_def = _find_job_running_marker(workflow, _VERIFY_WHEEL_MARKER)
    assert job_name is not None and job_def is not None, (
        "test.yml に 'python scripts/verify_wheel.py' を実行する job が見つからない。"
        " リリース前の wheel 実体検証（/CLAUDE.md §3・§6 手順 5）を CI で機械強制するには、"
        " 専用 job（wheel-check）のステップに verify_wheel.py の実行を追加する必要がある。"
    )
    return job_name, job_def


def test_test_yml_has_wheel_check_job():
    """P6-(1) test.yml に scripts/verify_wheel.py を実行する job が存在すること。"""
    _require_wheel_check_job()


def test_wheel_check_job_runs_the_build_route_not_an_existing_wheel():
    """P6-(2) verify_wheel.py の run に `--wheel` を含まないこと。

    `--wheel <path>` は「手元にある既存 wheel を検証する」モードであり、公開経路と
    同一のビルド（sdist 経由）を回さない。CI がこちらに倒れると、検証対象が
    「公開される wheel」ではなくなり、実ビルド経路の退行を検出できなくなる。
    """
    job_name, job_def = _require_wheel_check_job()

    offending = [
        run
        for run in _iter_step_runs(job_def)
        if _VERIFY_WHEEL_MARKER in run and _WHEEL_FLAG_MARKER in run
    ]
    assert not offending, (
        f"job '{job_name}' の verify_wheel.py 実行に --wheel が含まれている: {offending!r}。"
        " CI は既定（フラグなし＝ sdist 経由の実ビルド）で実行すること。"
    )


def test_wheel_check_job_installs_pypa_build():
    """P6-(3) PyPA `build` を導入するステップがあること。

    既定モードは `python -m build` を呼ぶため、build が未導入だと job は
    BUILD_TOOL_MISSING（exit 3）で常に赤くなる。
    """
    job_name, job_def = _require_wheel_check_job()

    runs = _iter_step_runs(job_def)
    assert any(_BUILD_INSTALL_RE.search(run) for run in runs), (
        f"job '{job_name}' に PyPA build を導入するステップ（例: "
        f"`python -m pip install build`）が見つからない: {runs!r}"
    )


def test_wheel_check_job_declares_read_only_permissions():
    """P6-(4) permissions が `{contents: read}` に完全一致すること。

    job レベルで permissions を明示しないと、GITHUB_TOKEN はリポジトリ設定依存の
    既定権限で動作する。本 job は checkout してビルドと読み取り検証を行うだけなので
    最小権限に絞る（deletions-check job と同じ方針）。
    """
    job_name, job_def = _require_wheel_check_job()

    permissions = job_def.get("permissions")
    assert permissions == {"contents": "read"}, (
        f"job '{job_name}' の permissions が {{contents: read}} と一致しない"
        f"（実際の値: {permissions!r}）。"
    )
