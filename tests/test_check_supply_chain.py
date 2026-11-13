"""tests/test_check_supply_chain.py

`scripts/check_supply_chain.py`（依存の既知脆弱性・Python のサポート終了の検査。配布元専用
dev ツール）の判定を、ネットワークにも pip-audit の起動にも触れずに測る。

設計の出どころ: `.claude/reports/architecture-report-20261114-023438.md`（revision 5）の
§3-2（検査スクリプト）と §4（テスト）。plan-report-20261114-023833.md のタスク `test-scm`。

## スクリプトの読み込み

`importlib.util.spec_from_file_location` でスクリプトを読み、`sys.modules` に登録してから
exec する。読み込みは各テストの中（`_load()`）で行う。スクリプトが無い（Red）時点でも
collection error にならず、各テストが個別に失敗する。

## テストが前提にする関数（名前と引数は本テストが決める。実装はこれに合わせる）

| 関数 | 引数 | 戻り値 |
|---|---|---|
| `load_allowlist(path, today)` | `path: pathlib.Path`（許容リストの TOML）・`today: datetime.date` | `(active_ids: set[str], violations: list[str])`。`active_ids` は期限内（`expires >= today`）の登録の `id` の集合。`violations` は許容リストそのものの違反（必須キーの欠け・空・日付の形式不正・期限切れ・ファイルが無い・TOML の破損）1 件ごとの文言 |
| `build_pip_audit_argv(requirements_path, output_path, osv_url=None)` | `requirements_path: str`・`output_path: str`・`osv_url: str \\| None` | `list[str]`。`[sys.executable, "-m", "pip_audit", ...]`。`-r <requirements_path>`・`--strict`・`-s osv`・`-f json`・`-o <output_path>`・`--progress-spinner off` を含み、`osv_url` が与えられたときだけ `--osv-url <osv_url>` を含む |
| `evaluate_audit_json(text, declared, allowed_ids)` | `text: str \\| None`（pip-audit の JSON 出力。無ければ `None`）・`declared: list[str]`（`[project].dependencies` の要件文字列そのまま。例 `"PyYAML>=6.0"`）・`allowed_ids: set[str]` | `(violations: list[str], unverifiable: list[str])` |
| `lower_python_bound(spec)` | `spec: str`（`requires-python` の値） | `str \\| None`。下限の major.minor（例 `"3.11"`）。下限が無い・読めないときは `None` |
| `collect_workflow_python_versions(workflows_dir)` | `workflows_dir: pathlib.Path` | `(versions: list[str], unverifiable: list[str])`。`versions` は major.minor の文字列 |
| `check_eol(requires_python, workflow_versions, eol_text, today, allowed_ids)` | `requires_python: str \\| None`・`workflow_versions: list[str]`・`eol_text: str`（endoflife.date の応答本文）・`today: datetime.date`・`allowed_ids: set[str]` | `(violations: list[str], unverifiable: list[str])` |
| `main(argv, *, audit_runner, eol_fetcher, today)` | `argv: list[str]`・`audit_runner: Callable[[list[str]], object]`（`build_pip_audit_argv` が組んだ引数列を受け取り pip-audit を実行する。`-o` の後のパスへ JSON を書く。起動できない・タイムアウトは例外で表す）・`eol_fetcher: Callable[[str], str]`（URL を受け取り応答本文を返す。失敗は例外で表す）・`today: datetime.date` | `int`（終了コード 0 / 1 / 3）。check 名の引数エラーは `SystemExit`（0 以外） |

`main` のオプションは設計 §3-2 の CLI のとおり `--pyproject`・`--workflows-dir`・`--allowlist`・
`--osv-url`・`--eol-url`。テストは常に明示のパスを渡す（既定のパスは測らない）。

文言（違反・検証不能の 1 件ごとの文字列）の中身は測らない。件数（空か否か）と終了コードと、
外部由来の文字列が `ascii()` の形で出ることだけを測る。

## 測る性質（§3-2・§4）

| 性質 | 内容 | テストクラス |
|---|---|---|
| S1 | 許容リストの解釈（必須キー・空・日付形式・期限切れ・不在・TOML 破損は違反／期限内だけ適用／期限当日は期限内） | `TestLoadAllowlist` |
| S2 | pip-audit の引数列（`--strict`・`-s osv`・`-f json` 等を必ず含む・`--osv-url` はそのまま渡す） | `TestBuildPipAuditArgv` |
| S3 | pip-audit の JSON の判定（脆弱性＝違反／許容は ID か aliases で一致／JSON 無し・破損・`skip_reason`・宣言した依存の欠落＝検証不能／名前は PEP 503 で正規化して照合） | `TestEvaluateAuditJson` |
| S4 | `requires-python` の下限（`>=`・`~=`・`>`・複数なら最大・下限演算子が無ければ None・major.minor にそろえる） | `TestLowerPythonBound` |
| S5 | workflow の版の収集（step の `with`・matrix の値・配列は要素ごと・`include` は数え `exclude` は数えない・`${{ }}` は読み飛ばし・文字列でない値は検証不能・major.minor にそろえる） | `TestCollectWorkflowPythonVersions` |
| S6 | 期限の判定（`eolFrom <= today` は違反・許容リストの `python-X.Y` で通る・応答の形の違い・応答に無い版・(a) の下限無し・(b) が 0 件は検証不能／(b) が 1 件以上なら (b) を理由に検証不能にならない） | `TestCheckEol` |
| S7 | `main` の配線（終了コード 0/1/3・引数エラー・pip-audit に渡す引数列・`--osv-url`/`--eol-url` の受け渡し・取得失敗は 3・外部由来の文字列は `ascii()` の形） | `TestMain` |

## Red の失敗理由

`scripts/check_supply_chain.py` がまだ無いため、`_load()` が `FileNotFoundError` を送出して
全テストが失敗する（構文エラー・import 漏れではない）。
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_supply_chain.py"
MODULE_NAME = "check_supply_chain"

TODAY = datetime.date(2026, 11, 14)

EOL_TEXT = json.dumps(
    {
        "result": {
            "releases": [
                {"name": "3.12", "eolFrom": "2028-10-31"},
                {"name": "3.11", "eolFrom": "2027-10-31"},
                {"name": "3.10", "eolFrom": "2026-10-01"},
            ]
        }
    }
)


def _load():
    """スクリプトを importlib で読み、`sys.modules` に登録してから exec する。"""
    if not SCRIPT_PATH.exists():
        raise FileNotFoundError(f"未実装: {SCRIPT_PATH}")
    spec = importlib.util.spec_from_file_location(MODULE_NAME, SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(MODULE_NAME, None)
        raise
    return module


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _allow_entry(id_: str, reason: str = "修正版が無い", expires: str = "2026-12-31") -> str:
    return f'[[allow]]\nid = "{id_}"\nreason = "{reason}"\nexpires = "{expires}"\n'


def _audit_json(dependencies: list[dict]) -> str:
    return json.dumps({"dependencies": dependencies, "fixes": []})


def _dep(name: str, version: str = "1.0.0", vulns: list[dict] | None = None) -> dict:
    return {"name": name, "version": version, "vulns": vulns or []}


def _vuln(id_: str, aliases: list[str] | None = None) -> dict:
    return {"id": id_, "fix_versions": [], "aliases": aliases or [], "description": "x"}


# ---------------------------------------------------------------------------
# S1: 許容リスト
# ---------------------------------------------------------------------------


class TestLoadAllowlist:
    def test_empty_file_has_no_entries_and_no_violations(self, tmp_path):
        mod = _load()
        path = _write(tmp_path / "allow.toml", "# 登録 0 件\n")
        active, violations = mod.load_allowlist(path, TODAY)
        assert active == set()
        assert violations == []

    def test_entry_within_term_is_active(self, tmp_path):
        mod = _load()
        path = _write(tmp_path / "allow.toml", _allow_entry("GHSA-aaaa", expires="2026-12-31"))
        active, violations = mod.load_allowlist(path, TODAY)
        assert active == {"GHSA-aaaa"}
        assert violations == []

    def test_entry_expiring_today_is_still_active(self, tmp_path):
        mod = _load()
        path = _write(tmp_path / "allow.toml", _allow_entry("GHSA-aaaa", expires="2026-11-14"))
        active, violations = mod.load_allowlist(path, TODAY)
        assert active == {"GHSA-aaaa"}
        assert violations == []

    def test_expired_entry_is_violation_and_not_applied(self, tmp_path):
        mod = _load()
        path = _write(tmp_path / "allow.toml", _allow_entry("GHSA-aaaa", expires="2026-11-13"))
        active, violations = mod.load_allowlist(path, TODAY)
        assert "GHSA-aaaa" not in active
        assert len(violations) >= 1

    @pytest.mark.parametrize("missing", ["id", "reason", "expires"])
    def test_missing_required_key_is_violation(self, tmp_path, missing):
        mod = _load()
        fields = {"id": '"GHSA-aaaa"', "reason": '"理由"', "expires": '"2026-12-31"'}
        body = "[[allow]]\n"
        for key, value in fields.items():
            if key != missing:
                body += f"{key} = {value}\n"
        path = _write(tmp_path / "allow.toml", body)
        _active, violations = mod.load_allowlist(path, TODAY)
        assert len(violations) >= 1

    @pytest.mark.parametrize("empty", ["id", "reason", "expires"])
    def test_empty_required_value_is_violation(self, tmp_path, empty):
        mod = _load()
        fields = {"id": "GHSA-aaaa", "reason": "理由", "expires": "2026-12-31"}
        fields[empty] = ""
        path = _write(
            tmp_path / "allow.toml",
            _allow_entry(fields["id"], reason=fields["reason"], expires=fields["expires"]),
        )
        _active, violations = mod.load_allowlist(path, TODAY)
        assert len(violations) >= 1

    def test_malformed_date_is_violation(self, tmp_path):
        mod = _load()
        path = _write(tmp_path / "allow.toml", _allow_entry("GHSA-aaaa", expires="2026/12/31"))
        active, violations = mod.load_allowlist(path, TODAY)
        assert "GHSA-aaaa" not in active
        assert len(violations) >= 1

    def test_missing_file_is_violation(self, tmp_path):
        mod = _load()
        _active, violations = mod.load_allowlist(tmp_path / "absent.toml", TODAY)
        assert len(violations) >= 1

    def test_broken_toml_is_violation(self, tmp_path):
        mod = _load()
        path = _write(tmp_path / "allow.toml", "[[allow]\nid = \n")
        _active, violations = mod.load_allowlist(path, TODAY)
        assert len(violations) >= 1


# ---------------------------------------------------------------------------
# S2: pip-audit の引数列
# ---------------------------------------------------------------------------


def _has_pair(argv: list[str], flag: str, value: str) -> bool:
    return any(argv[i] == flag and argv[i + 1] == value for i in range(len(argv) - 1))


class TestBuildPipAuditArgv:
    def test_required_arguments_are_present(self):
        mod = _load()
        argv = mod.build_pip_audit_argv("req.txt", "out.json")
        assert argv[:3] == [sys.executable, "-m", "pip_audit"]
        assert "--strict" in argv
        assert _has_pair(argv, "-s", "osv")
        assert _has_pair(argv, "-f", "json")
        assert _has_pair(argv, "-r", "req.txt")
        assert _has_pair(argv, "-o", "out.json")
        assert _has_pair(argv, "--progress-spinner", "off")

    def test_osv_url_is_absent_by_default(self):
        mod = _load()
        argv = mod.build_pip_audit_argv("req.txt", "out.json")
        assert "--osv-url" not in argv

    def test_osv_url_is_passed_as_is(self):
        mod = _load()
        argv = mod.build_pip_audit_argv("req.txt", "out.json", osv_url="http://127.0.0.1:9/v1")
        assert _has_pair(argv, "--osv-url", "http://127.0.0.1:9/v1")


# ---------------------------------------------------------------------------
# S3: pip-audit の JSON の判定
# ---------------------------------------------------------------------------

DECLARED = ["PyYAML>=6.0", "duckdb>=0.10"]


class TestEvaluateAuditJson:
    def test_clean_result_has_no_violation_and_no_unverifiable(self):
        mod = _load()
        text = _audit_json([_dep("pyyaml"), _dep("duckdb"), _dep("numpy")])
        violations, unverifiable = mod.evaluate_audit_json(text, DECLARED, set())
        assert violations == []
        assert unverifiable == []

    def test_vulnerability_is_violation(self):
        mod = _load()
        text = _audit_json([_dep("pyyaml"), _dep("duckdb", vulns=[_vuln("PYSEC-2024-1")])])
        violations, unverifiable = mod.evaluate_audit_json(text, DECLARED, set())
        assert len(violations) >= 1
        assert unverifiable == []

    def test_vulnerability_in_transitive_dependency_is_violation(self):
        mod = _load()
        text = _audit_json(
            [_dep("pyyaml"), _dep("duckdb"), _dep("numpy", vulns=[_vuln("PYSEC-2024-2")])]
        )
        violations, _unverifiable = mod.evaluate_audit_json(text, DECLARED, set())
        assert len(violations) >= 1

    def test_allowed_by_id(self):
        mod = _load()
        text = _audit_json([_dep("pyyaml"), _dep("duckdb", vulns=[_vuln("PYSEC-2024-1")])])
        violations, unverifiable = mod.evaluate_audit_json(text, DECLARED, {"PYSEC-2024-1"})
        assert violations == []
        assert unverifiable == []

    def test_allowed_by_alias(self):
        mod = _load()
        vuln = _vuln("PYSEC-2024-1", aliases=["CVE-2024-0001", "GHSA-xxxx-yyyy-zzzz"])
        text = _audit_json([_dep("pyyaml"), _dep("duckdb", vulns=[vuln])])
        violations, _unverifiable = mod.evaluate_audit_json(
            text, DECLARED, {"GHSA-xxxx-yyyy-zzzz"}
        )
        assert violations == []

    def test_unrelated_allow_entry_does_not_suppress(self):
        mod = _load()
        text = _audit_json([_dep("pyyaml"), _dep("duckdb", vulns=[_vuln("PYSEC-2024-1")])])
        violations, _unverifiable = mod.evaluate_audit_json(text, DECLARED, {"PYSEC-2099-9"})
        assert len(violations) >= 1

    def test_missing_json_is_unverifiable(self):
        mod = _load()
        violations, unverifiable = mod.evaluate_audit_json(None, DECLARED, set())
        assert violations == []
        assert len(unverifiable) >= 1

    def test_broken_json_is_unverifiable(self):
        mod = _load()
        violations, unverifiable = mod.evaluate_audit_json('{"dependencies": [', DECLARED, set())
        assert violations == []
        assert len(unverifiable) >= 1

    def test_skip_reason_is_unverifiable(self):
        mod = _load()
        text = _audit_json(
            [_dep("pyyaml"), {"name": "duckdb", "skip_reason": "Dependency not found on PyPI"}]
        )
        _violations, unverifiable = mod.evaluate_audit_json(text, DECLARED, set())
        assert len(unverifiable) >= 1

    def test_declared_dependency_absent_from_result_is_unverifiable(self):
        mod = _load()
        text = _audit_json([_dep("pyyaml")])
        _violations, unverifiable = mod.evaluate_audit_json(text, DECLARED, set())
        assert len(unverifiable) >= 1

    def test_names_are_matched_after_pep503_normalization(self):
        mod = _load()
        declared = ["Foo_Bar.Baz>=1", "PyYAML>=6.0"]
        text = _audit_json([_dep("foo-bar-baz"), _dep("pyyaml")])
        violations, unverifiable = mod.evaluate_audit_json(text, declared, set())
        assert violations == []
        assert unverifiable == []

    # --- aliases の型（code-review-report-review-code [CR-Q-001] High） ---
    # 「配列でない」= キーがあって値が配列以外のものすべて（偽になる "" と 0 も含む）。
    # キーが無い・値が null は別名が空の記録として従来どおり読む。

    @pytest.mark.parametrize(
        "aliases",
        [5, "abc", "", 0],
        ids=["int", "str", "empty-str", "zero"],
    )
    def test_non_array_aliases_is_unverifiable_not_violation(self, aliases):
        mod = _load()
        vuln = {"id": "PYSEC-2024-1", "fix_versions": [], "aliases": aliases}
        text = _audit_json([_dep("pyyaml"), _dep("duckdb", vulns=[vuln])])
        violations, unverifiable = mod.evaluate_audit_json(text, DECLARED, set())
        assert violations == []
        assert len(unverifiable) >= 1

    def test_array_aliases_is_read_as_before(self):
        mod = _load()
        vuln = _vuln("PYSEC-2024-1", aliases=["CVE-2024-0001"])
        text = _audit_json([_dep("pyyaml"), _dep("duckdb", vulns=[vuln])])
        violations, unverifiable = mod.evaluate_audit_json(text, DECLARED, set())
        assert len(violations) >= 1
        assert unverifiable == []
        allowed_v, allowed_u = mod.evaluate_audit_json(text, DECLARED, {"CVE-2024-0001"})
        assert allowed_v == []
        assert allowed_u == []

    @pytest.mark.parametrize("present", [False, True], ids=["absent", "null"])
    def test_absent_or_null_aliases_is_read_as_empty(self, present):
        mod = _load()
        vuln = {"id": "PYSEC-2024-1", "fix_versions": []}
        if present:
            vuln["aliases"] = None
        text = _audit_json([_dep("pyyaml"), _dep("duckdb", vulns=[vuln])])
        violations, unverifiable = mod.evaluate_audit_json(text, DECLARED, set())
        assert len(violations) >= 1
        assert unverifiable == []
        allowed_v, allowed_u = mod.evaluate_audit_json(text, DECLARED, {"PYSEC-2024-1"})
        assert allowed_v == []
        assert allowed_u == []


# ---------------------------------------------------------------------------
# S4: requires-python の下限
# ---------------------------------------------------------------------------


class TestLowerPythonBound:
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            (">=3.11", "3.11"),
            (">=3.11.2", "3.11"),
            ("~=3.11", "3.11"),
            (">3.10", "3.10"),
            (">=3.10,>=3.11", "3.11"),
            (">=3.11,<4", "3.11"),
            (">=3.10", "3.10"),
        ],
    )
    def test_lower_bound(self, spec, expected):
        mod = _load()
        assert mod.lower_python_bound(spec) == expected

    @pytest.mark.parametrize("spec", ["==3.11", "!=3.10", "<4", "<=3.12", "", "abc"])
    def test_no_lower_bound_is_none(self, spec):
        mod = _load()
        assert mod.lower_python_bound(spec) is None


# ---------------------------------------------------------------------------
# S5: workflow の版の収集
# ---------------------------------------------------------------------------

STEP_WORKFLOW = """\
name: a
on: push
jobs:
  j:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@0000000000000000000000000000000000000000 # v5.0.0
        with:
          python-version: "3.12"
"""

MATRIX_WORKFLOW = """\
name: b
on: push
jobs:
  j:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12.4"]
        include:
          - os: windows-latest
            python-version: "3.13"
        exclude:
          - python-version: "3.9"
    steps:
      - uses: actions/setup-python@0000000000000000000000000000000000000000 # v5.0.0
        with:
          python-version: ${{ matrix.python-version }}
"""

NUMERIC_WORKFLOW = """\
name: c
on: push
jobs:
  j:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@0000000000000000000000000000000000000000 # v5.0.0
        with:
          python-version: 3.10
"""


class TestCollectWorkflowPythonVersions:
    def test_step_with_value(self, tmp_path):
        mod = _load()
        _write(tmp_path / "wf" / "a.yml", STEP_WORKFLOW)
        versions, unverifiable = mod.collect_workflow_python_versions(tmp_path / "wf")
        assert set(versions) == {"3.12"}
        assert unverifiable == []

    def test_matrix_array_include_counted_exclude_not_expression_skipped(self, tmp_path):
        mod = _load()
        _write(tmp_path / "wf" / "b.yml", MATRIX_WORKFLOW)
        versions, unverifiable = mod.collect_workflow_python_versions(tmp_path / "wf")
        assert set(versions) == {"3.11", "3.12", "3.13"}
        assert "3.9" not in versions
        assert unverifiable == []

    def test_non_string_value_is_unverifiable(self, tmp_path):
        mod = _load()
        _write(tmp_path / "wf" / "c.yml", NUMERIC_WORKFLOW)
        _versions, unverifiable = mod.collect_workflow_python_versions(tmp_path / "wf")
        assert len(unverifiable) >= 1

    def test_versions_from_multiple_files(self, tmp_path):
        mod = _load()
        _write(tmp_path / "wf" / "a.yml", STEP_WORKFLOW)
        _write(tmp_path / "wf" / "b.yml", MATRIX_WORKFLOW)
        versions, _unverifiable = mod.collect_workflow_python_versions(tmp_path / "wf")
        assert set(versions) == {"3.11", "3.12", "3.13"}

    def test_missing_directory_yields_no_versions(self, tmp_path):
        mod = _load()
        versions, _unverifiable = mod.collect_workflow_python_versions(tmp_path / "absent")
        assert list(versions) == []


# ---------------------------------------------------------------------------
# S6: 期限の判定
# ---------------------------------------------------------------------------


class TestCheckEol:
    def test_supported_versions_pass(self):
        mod = _load()
        violations, unverifiable = mod.check_eol(">=3.11", ["3.11", "3.12"], EOL_TEXT, TODAY, set())
        assert violations == []
        assert unverifiable == []

    def test_eol_lower_bound_is_violation(self):
        mod = _load()
        violations, _unverifiable = mod.check_eol(">=3.10", ["3.11"], EOL_TEXT, TODAY, set())
        assert len(violations) >= 1

    def test_eol_workflow_version_is_violation(self):
        mod = _load()
        violations, _unverifiable = mod.check_eol(
            ">=3.11", ["3.10", "3.11"], EOL_TEXT, TODAY, set()
        )
        assert len(violations) >= 1

    def test_eol_on_today_is_violation_and_tomorrow_is_not(self):
        mod = _load()
        text = json.dumps(
            {
                "result": {
                    "releases": [
                        {"name": "3.11", "eolFrom": "2026-11-14"},
                        {"name": "3.12", "eolFrom": "2026-11-15"},
                    ]
                }
            }
        )
        on_today, _ = mod.check_eol(">=3.11", ["3.11"], text, TODAY, set())
        tomorrow, unverifiable = mod.check_eol(">=3.12", ["3.12"], text, TODAY, set())
        assert len(on_today) >= 1
        assert tomorrow == []
        assert unverifiable == []

    def test_allowed_python_version_passes(self):
        mod = _load()
        violations, _unverifiable = mod.check_eol(
            ">=3.10", ["3.10"], EOL_TEXT, TODAY, {"python-3.10"}
        )
        assert violations == []

    def test_allow_for_other_version_does_not_suppress(self):
        mod = _load()
        violations, _unverifiable = mod.check_eol(
            ">=3.10", ["3.10"], EOL_TEXT, TODAY, {"python-3.9"}
        )
        assert len(violations) >= 1

    @pytest.mark.parametrize(
        "text",
        [
            "not json",
            json.dumps({"releases": []}),
            json.dumps({"result": {"releases": [{"name": "3.11"}]}}),
            json.dumps([1, 2, 3]),
        ],
    )
    def test_malformed_response_is_unverifiable(self, text):
        mod = _load()
        _violations, unverifiable = mod.check_eol(">=3.11", ["3.11"], text, TODAY, set())
        assert len(unverifiable) >= 1

    def test_version_absent_from_response_is_unverifiable(self):
        mod = _load()
        _violations, unverifiable = mod.check_eol(
            ">=3.11", ["3.11", "3.14"], EOL_TEXT, TODAY, set()
        )
        assert len(unverifiable) >= 1

    @pytest.mark.parametrize("requires_python", [None, "<4", "abc"])
    def test_no_lower_bound_is_unverifiable(self, requires_python):
        mod = _load()
        _violations, unverifiable = mod.check_eol(
            requires_python, ["3.11"], EOL_TEXT, TODAY, set()
        )
        assert len(unverifiable) >= 1

    def test_zero_workflow_versions_is_unverifiable_even_if_bound_is_fine(self):
        mod = _load()
        violations, unverifiable = mod.check_eol(">=3.11", [], EOL_TEXT, TODAY, set())
        assert violations == []
        assert len(unverifiable) >= 1

    def test_one_workflow_version_is_not_unverifiable_for_that_reason(self):
        mod = _load()
        _violations, unverifiable = mod.check_eol(">=3.11", ["3.12"], EOL_TEXT, TODAY, set())
        assert unverifiable == []


# ---------------------------------------------------------------------------
# S7: main の配線
# ---------------------------------------------------------------------------

PYPROJECT = """\
[project]
name = "x"
requires-python = "{requires}"
dependencies = [
    "PyYAML>=6.0",
    "duckdb>=0.10",
]
"""


class _FakeAudit:
    """pip-audit の代わり。受け取った引数列を記録し、`-o` の後のパスへ JSON を書く。"""

    def __init__(self, text: str | None = None, exc: BaseException | None = None):
        self.text = text
        self.exc = exc
        self.calls: list[list[str]] = []
        self.requirements: list[str] = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if "-r" in argv:
            req = Path(argv[argv.index("-r") + 1])
            if req.exists():
                self.requirements.append(req.read_text(encoding="utf-8"))
        if self.exc is not None:
            raise self.exc
        if self.text is not None:
            Path(argv[argv.index("-o") + 1]).write_text(self.text, encoding="utf-8")
        return 0


class _FakeEol:
    def __init__(self, text: str = EOL_TEXT, exc: BaseException | None = None):
        self.text = text
        self.exc = exc
        self.urls: list[str] = []

    def __call__(self, url):
        self.urls.append(url)
        if self.exc is not None:
            raise self.exc
        return self.text


CLEAN_AUDIT = _audit_json([_dep("pyyaml"), _dep("duckdb")])


def _setup(tmp_path, requires=">=3.11", workflow=STEP_WORKFLOW, allowlist="# 0 件\n"):
    pyproject = _write(tmp_path / "pyproject.toml", PYPROJECT.format(requires=requires))
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    if workflow is not None:
        _write(wf_dir / "test.yml", workflow)
    allow = tmp_path / "allow.toml"
    if allowlist is not None:
        _write(allow, allowlist)
    return [
        "--pyproject",
        str(pyproject),
        "--workflows-dir",
        str(wf_dir),
        "--allowlist",
        str(allow),
    ]


def _run(mod, argv, audit=None, eol=None):
    return mod.main(
        argv,
        audit_runner=audit if audit is not None else _FakeAudit(CLEAN_AUDIT),
        eol_fetcher=eol if eol is not None else _FakeEol(),
        today=TODAY,
    )


class TestMain:
    def test_clean_vuln_and_eol_exit_0(self, tmp_path):
        mod = _load()
        assert _run(mod, ["vuln", "eol", *_setup(tmp_path)]) == 0

    def test_vulnerability_exits_1(self, tmp_path):
        mod = _load()
        audit = _FakeAudit(_audit_json([_dep("pyyaml"), _dep("duckdb", vulns=[_vuln("PYSEC-1")])]))
        assert _run(mod, ["vuln", *_setup(tmp_path)], audit=audit) == 1

    def test_vulnerability_allowed_within_term_exits_0(self, tmp_path):
        mod = _load()
        audit = _FakeAudit(_audit_json([_dep("pyyaml"), _dep("duckdb", vulns=[_vuln("PYSEC-1")])]))
        argv = _setup(tmp_path, allowlist=_allow_entry("PYSEC-1", expires="2026-12-31"))
        assert _run(mod, ["vuln", *argv], audit=audit) == 0

    def test_vulnerability_allowed_but_expired_exits_1(self, tmp_path):
        mod = _load()
        audit = _FakeAudit(_audit_json([_dep("pyyaml"), _dep("duckdb", vulns=[_vuln("PYSEC-1")])]))
        argv = _setup(tmp_path, allowlist=_allow_entry("PYSEC-1", expires="2026-11-13"))
        assert _run(mod, ["vuln", *argv], audit=audit) == 1

    def test_invalid_allowlist_exits_1(self, tmp_path):
        mod = _load()
        argv = _setup(tmp_path, allowlist='[[allow]]\nid = "PYSEC-1"\nexpires = "2026-12-31"\n')
        assert _run(mod, ["vuln", *argv]) == 1

    def test_missing_allowlist_exits_1(self, tmp_path):
        mod = _load()
        argv = _setup(tmp_path, allowlist=None)
        assert _run(mod, ["vuln", *argv]) == 1

    def test_audit_without_json_exits_3(self, tmp_path):
        mod = _load()
        assert _run(mod, ["vuln", *_setup(tmp_path)], audit=_FakeAudit(None)) == 3

    def test_audit_launch_failure_exits_3(self, tmp_path):
        mod = _load()
        audit = _FakeAudit(exc=FileNotFoundError("pip_audit"))
        assert _run(mod, ["vuln", *_setup(tmp_path)], audit=audit) == 3

    def test_violation_takes_precedence_over_unverifiable(self, tmp_path):
        mod = _load()
        audit = _FakeAudit(_audit_json([_dep("pyyaml"), _dep("duckdb", vulns=[_vuln("PYSEC-1")])]))
        eol = _FakeEol(exc=OSError("unreachable"))
        assert _run(mod, ["vuln", "eol", *_setup(tmp_path)], audit=audit, eol=eol) == 1

    def test_audit_receives_built_argv_and_declared_requirements(self, tmp_path):
        mod = _load()
        audit = _FakeAudit(CLEAN_AUDIT)
        _run(mod, ["vuln", *_setup(tmp_path)], audit=audit)
        assert len(audit.calls) == 1
        argv = audit.calls[0]
        assert "--strict" in argv
        assert _has_pair(argv, "-s", "osv")
        assert _has_pair(argv, "-f", "json")
        assert "--osv-url" not in argv
        assert len(audit.requirements) == 1
        assert "PyYAML>=6.0" in audit.requirements[0]
        assert "duckdb>=0.10" in audit.requirements[0]

    def test_osv_url_option_is_passed_to_pip_audit(self, tmp_path):
        mod = _load()
        audit = _FakeAudit(CLEAN_AUDIT)
        argv = ["vuln", *_setup(tmp_path), "--osv-url", "http://127.0.0.1:9/osv"]
        _run(mod, argv, audit=audit)
        assert _has_pair(audit.calls[0], "--osv-url", "http://127.0.0.1:9/osv")

    def test_eol_only_does_not_run_audit(self, tmp_path):
        mod = _load()
        audit = _FakeAudit(CLEAN_AUDIT)
        assert _run(mod, ["eol", *_setup(tmp_path)], audit=audit) == 0
        assert audit.calls == []

    def test_eol_url_option_is_passed_to_fetcher(self, tmp_path):
        mod = _load()
        eol = _FakeEol()
        _run(mod, ["eol", *_setup(tmp_path), "--eol-url", "http://127.0.0.1:9/eol"], eol=eol)
        assert eol.urls == ["http://127.0.0.1:9/eol"]

    def test_eol_fetch_failure_exits_3(self, tmp_path):
        mod = _load()
        eol = _FakeEol(exc=OSError("unreachable"))
        assert _run(mod, ["eol", *_setup(tmp_path)], eol=eol) == 3

    def test_eol_requires_python_310_exits_1(self, tmp_path):
        mod = _load()
        assert _run(mod, ["eol", *_setup(tmp_path, requires=">=3.10")]) == 1

    def test_eol_workflow_310_exits_1(self, tmp_path):
        mod = _load()
        workflow = STEP_WORKFLOW.replace('"3.12"', '"3.10"')
        assert _run(mod, ["eol", *_setup(tmp_path, workflow=workflow)]) == 1

    def test_eol_310_allowed_exits_0(self, tmp_path):
        mod = _load()
        argv = _setup(
            tmp_path, requires=">=3.10", allowlist=_allow_entry("python-3.10", expires="2026-12-31")
        )
        assert _run(mod, ["eol", *argv]) == 0

    def test_eol_with_no_workflow_versions_exits_3(self, tmp_path):
        mod = _load()
        assert _run(mod, ["eol", *_setup(tmp_path, workflow=None)]) == 3

    def test_no_check_is_argument_error(self, tmp_path):
        mod = _load()
        audit = _FakeAudit(CLEAN_AUDIT)
        with pytest.raises(SystemExit) as excinfo:
            _run(mod, [*_setup(tmp_path)], audit=audit)
        assert excinfo.value.code not in (0, None)
        assert audit.calls == []

    def test_unknown_check_is_argument_error(self, tmp_path):
        mod = _load()
        audit = _FakeAudit(CLEAN_AUDIT)
        with pytest.raises(SystemExit) as excinfo:
            _run(mod, ["vuln", "nope", *_setup(tmp_path)], audit=audit)
        assert excinfo.value.code not in (0, None)
        assert audit.calls == []

    def test_external_strings_are_printed_in_ascii_form(self, tmp_path, capsys):
        mod = _load()
        evil_id = "PYSEC-\x1b[31mX\u202e"
        audit = _FakeAudit(
            _audit_json([_dep("pyyaml"), _dep("duckdb", vulns=[_vuln(evil_id)])])
        )
        assert _run(mod, ["vuln", *_setup(tmp_path)], audit=audit) == 1
        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert "\x1b" not in out
        assert "\u202e" not in out
        assert ascii(evil_id) in out
