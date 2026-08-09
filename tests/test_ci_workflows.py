"""
tests/test_ci_workflows.py

.github/workflows/test.yml が `scripts/check_deletions.py --check` を実行する CI job を
持ち続けること、かつその job の checkout が `fetch-depth: 0` を持ち続けることを守るテスト。

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
