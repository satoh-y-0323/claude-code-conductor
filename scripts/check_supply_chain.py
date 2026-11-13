#!/usr/bin/env python3
"""依存の既知脆弱性と Python のサポート終了を検査する（配布元専用 dev ツール）。

使い方:
    python scripts/check_supply_chain.py <check> [<check> ...] [options]

check:
    vuln  pyproject.toml の [project].dependencies を pip-audit（OSV）で検査する
    eol   requires-python の下限と workflow の python-version を endoflife.date と照合する

オプション:
    --pyproject PATH      既定: リポジトリの pyproject.toml
    --workflows-dir PATH  既定: リポジトリの .github/workflows
    --allowlist PATH      既定: リポジトリの .github/supply-chain-allowlist.toml
    --osv-url URL         pip-audit の --osv-url にそのまま渡す
    --eol-url URL         既定: endoflife.date API v1 の python

終了コード:
    0  違反も検証不能も無い
    1  違反がある（脆弱性・サポート終了の版・許容リストの不備）
    3  違反は無いが検証不能がある（情報源に届かない・結果を読めない等）

設計: architecture-report-20261114-023438.md §3-2。`c3` は import しない（CI の検査 job に
`c3` を入れないため）。PyYAML は eol の workflow 読み取りでだけ使う（無ければ検証不能）。
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from pathlib import Path
from typing import Callable

# stdout/stderr reconfigure for Windows CI compatibility (cp1252 environment)
# stdin は未使用なため reconfigure 不要（本スクリプトは stdin を読まない）
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError, OSError):
    pass

try:
    sys.stderr.reconfigure(encoding='utf-8')
except (AttributeError, ValueError, OSError):
    pass


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PYPROJECT = _REPO_ROOT / "pyproject.toml"
DEFAULT_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"
DEFAULT_ALLOWLIST = _REPO_ROOT / ".github" / "supply-chain-allowlist.toml"
DEFAULT_EOL_URL = "https://endoflife.date/api/v1/products/python/"

AUDIT_TIMEOUT_SECONDS = 600
EOL_TIMEOUT_SECONDS = 30

CHECKS = ("vuln", "eol")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LOWER_SPEC_RE = re.compile(r"^(>=|~=|>)\s*(\d+)\.(\d+)")
_MAJOR_MINOR_RE = re.compile(r"^(\d+)\.(\d+)")
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
_PEP503_RE = re.compile(r"[-_.]+")


# ---------------------------------------------------------------------------
# 許容リスト
# ---------------------------------------------------------------------------


def _parse_date(value: object) -> datetime.date | None:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def load_allowlist(path: Path, today: datetime.date) -> tuple[set[str], list[str]]:
    """許容リストを読み、期限内の登録の id 集合と、許容リスト自体の違反を返す。

    期限内 = `expires >= today`（期限当日は期限内）。期限切れ・必須キーの欠け/空・
    日付の形式不正・ファイルが無い・TOML の破損は違反。違反の登録は適用しない。
    """
    active: set[str] = set()
    violations: list[str] = []
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return active, [f"許容リストが無い: {ascii(str(path))}"]
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return active, [f"許容リストを読めない: {ascii(str(path))}: {ascii(str(exc))}"]

    entries = data.get("allow", [])
    if not isinstance(entries, list):
        return active, ["許容リストの allow が [[allow]] の配列ではない"]

    for index, entry in enumerate(entries):
        label = f"allow[{index}]"
        if not isinstance(entry, dict):
            violations.append(f"{label}: 表ではない")
            continue
        broken = False
        for key in ("id", "reason", "expires"):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                violations.append(f"{label}: 必須キー {key} が無いか空")
                broken = True
        if broken:
            continue
        entry_id = entry["id"]
        expires = _parse_date(entry["expires"])
        if expires is None:
            violations.append(
                f"{label} {ascii(entry_id)}: expires が YYYY-MM-DD ではない: {ascii(entry['expires'])}"
            )
            continue
        if expires < today:
            violations.append(f"{label} {ascii(entry_id)}: 期限切れ（expires={expires.isoformat()}）")
            continue
        active.add(entry_id)
    return active, violations


# ---------------------------------------------------------------------------
# vuln
# ---------------------------------------------------------------------------


def build_pip_audit_argv(
    requirements_path: str, output_path: str, osv_url: str | None = None
) -> list[str]:
    """pip-audit の引数列を組む。判定は終了コードでなく JSON で行う（ADR-2）。"""
    argv = [
        sys.executable,
        "-m",
        "pip_audit",
        "-r",
        requirements_path,
        "--strict",
        "-s",
        "osv",
        "-f",
        "json",
        "-o",
        output_path,
        "--progress-spinner",
        "off",
    ]
    if osv_url is not None:
        argv += ["--osv-url", osv_url]
    return argv


def _normalize_name(name: str) -> str:
    """PEP 503 の正規化（小文字化し `-` `_` `.` の連続を `-` に）。"""
    return _PEP503_RE.sub("-", name).lower()


def _requirement_name(requirement: str) -> str | None:
    match = _REQ_NAME_RE.match(requirement)
    return match.group(1) if match else None


def evaluate_audit_json(
    text: str | None, declared: list[str], allowed_ids: set[str]
) -> tuple[list[str], list[str]]:
    """pip-audit の JSON 出力を判定し (violations, unverifiable) を返す。"""
    violations: list[str] = []
    unverifiable: list[str] = []
    if text is None:
        return violations, ["pip-audit の JSON 出力が無い"]
    try:
        data = json.loads(text)
    except ValueError as exc:
        return violations, [f"pip-audit の JSON を読めない: {ascii(str(exc))}"]
    dependencies = data.get("dependencies") if isinstance(data, dict) else None
    if not isinstance(dependencies, list):
        return violations, ["pip-audit の JSON に dependencies の配列が無い"]

    seen: set[str] = set()
    for dep in dependencies:
        if not isinstance(dep, dict) or not isinstance(dep.get("name"), str):
            unverifiable.append(f"pip-audit の結果に読めない依存がある: {ascii(dep)}")
            continue
        name = dep["name"]
        seen.add(_normalize_name(name))
        if dep.get("skip_reason"):
            unverifiable.append(
                f"{ascii(name)} は検査されていない: {ascii(str(dep['skip_reason']))}"
            )
            continue
        vulns = dep.get("vulns", [])
        if not isinstance(vulns, list):
            unverifiable.append(f"{ascii(name)} の vulns が配列ではない")
            continue
        for vuln in vulns:
            if not isinstance(vuln, dict) or not isinstance(vuln.get("id"), str):
                unverifiable.append(f"{ascii(name)} に読めない脆弱性の記録がある: {ascii(vuln)}")
                continue
            # キーが無い・null は別名なしとして読む。それ以外の配列でない値（偽になる "" / 0 も含む）は検証不能
            aliases = vuln.get("aliases")
            if aliases is None:
                aliases = []
            if not isinstance(aliases, list):
                unverifiable.append(f"{ascii(name)} の aliases が配列ではない: {ascii(aliases)}")
                continue
            ids = {vuln["id"], *(a for a in aliases if isinstance(a, str))}
            if ids & allowed_ids:
                continue
            violations.append(
                f"{ascii(name)} {ascii(str(dep.get('version')))}: {ascii(vuln['id'])}"
                f"（別名 {ascii(sorted(a for a in aliases if isinstance(a, str)))}）"
            )

    for requirement in declared:
        req_name = _requirement_name(requirement)
        if req_name is None:
            unverifiable.append(f"宣言した依存の名前を読めない: {ascii(requirement)}")
        elif _normalize_name(req_name) not in seen:
            unverifiable.append(f"宣言した依存が pip-audit の結果に無い: {ascii(requirement)}")
    return violations, unverifiable


# ---------------------------------------------------------------------------
# eol
# ---------------------------------------------------------------------------


def lower_python_bound(spec: str) -> str | None:
    """`requires-python` の下限を major.minor で返す。下限が無い・読めないときは None。"""
    if not isinstance(spec, str):
        return None
    best: tuple[int, int] | None = None
    for part in spec.split(","):
        part = part.strip()
        if not part.startswith((">", "~=")):
            continue  # `==` `!=` `<` `<=` は下限を与えない
        match = _LOWER_SPEC_RE.match(part)
        if match is None:
            # 設計書に記載なし: 下限の演算子を持つのに版を読めない指定子があれば、
            # 下限を確定できないので「読めない」＝ None とする（安全側）
            return None
        candidate = (int(match.group(2)), int(match.group(3)))
        if best is None or candidate > best:
            best = candidate
    if best is None:
        return None
    return f"{best[0]}.{best[1]}"


def _collect_python_version_value(
    value: object, where: str, versions: list[str], unverifiable: list[str]
) -> None:
    """python-version の値 1 つ（配列なら要素ごと）を版として読む。"""
    if isinstance(value, list):
        for item in value:
            _collect_python_version_value(item, where, versions, unverifiable)
        return
    if not isinstance(value, str):
        unverifiable.append(f"{where}: python-version が文字列ではない: {ascii(value)}")
        return
    if "${{" in value:
        return  # 式は読み飛ばす（matrix 側の値で数える）
    match = _MAJOR_MINOR_RE.match(value.strip())
    if match is None:
        unverifiable.append(f"{where}: python-version を版として読めない: {ascii(value)}")
        return
    versions.append(f"{int(match.group(1))}.{int(match.group(2))}")


def collect_workflow_python_versions(workflows_dir: Path) -> tuple[list[str], list[str]]:
    """workflow の python-version（step の with・matrix の値・matrix.include）を集める。

    matrix.exclude は数えない。版を 1 件も読めないことの扱いは check_eol が行う。
    """
    versions: list[str] = []
    unverifiable: list[str] = []
    workflows_dir = Path(workflows_dir)
    if not workflows_dir.is_dir():
        return versions, unverifiable
    files = sorted([*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml")])
    if not files:
        return versions, unverifiable
    try:
        import yaml
    except ImportError:
        return versions, ["PyYAML が無いため workflow を読めない"]

    for path in files:
        where = path.name
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            unverifiable.append(f"{ascii(where)} を読めない: {ascii(str(exc))}")
            continue
        jobs = doc.get("jobs") if isinstance(doc, dict) else None
        if not isinstance(jobs, dict):
            continue
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            job_where = f"{ascii(where)} jobs.{ascii(str(job_name))}"
            strategy = job.get("strategy")
            matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
            # python-version を持ちうる表: matrix 本体・matrix.include の各要素・step の with
            carriers: list[object] = []
            if isinstance(matrix, dict):
                carriers.append(matrix)
                include = matrix.get("include")
                if isinstance(include, list):
                    carriers.extend(include)
            steps = job.get("steps")
            if isinstance(steps, list):
                carriers.extend(step.get("with") for step in steps if isinstance(step, dict))
            for carrier in carriers:
                if isinstance(carrier, dict) and "python-version" in carrier:
                    _collect_python_version_value(
                        carrier["python-version"], job_where, versions, unverifiable
                    )
    return versions, unverifiable


def _parse_eol_releases(eol_text: str) -> dict[str, datetime.date] | None:
    """endoflife.date の応答から {name: eolFrom} を作る。形が違えば None。"""
    try:
        data = json.loads(eol_text)
    except ValueError:
        return None
    result = data.get("result") if isinstance(data, dict) else None
    releases = result.get("releases") if isinstance(result, dict) else None
    if not isinstance(releases, list):
        return None
    table: dict[str, datetime.date] = {}
    for release in releases:
        if not isinstance(release, dict) or not isinstance(release.get("name"), str):
            continue
        eol_from = _parse_date(release.get("eolFrom"))
        if eol_from is not None:
            table[release["name"]] = eol_from
    # 設計書に記載なし: eolFrom を読めない release は表に入れず、その版を使っていれば
    # 「応答に無い版」として検証不能になる（使っていない版の欠けでは赤にしない）
    return table


def check_eol(
    requires_python: str | None,
    workflow_versions: list[str],
    eol_text: str,
    today: datetime.date,
    allowed_ids: set[str],
) -> tuple[list[str], list[str]]:
    """使っている Python の版のサポート期限を判定し (violations, unverifiable) を返す。"""
    violations: list[str] = []
    unverifiable: list[str] = []

    used: list[str] = []
    lower = lower_python_bound(requires_python) if requires_python is not None else None
    if lower is None:
        unverifiable.append(f"requires-python の下限を読めない: {ascii(requires_python)}")
    else:
        used.append(lower)
    if not workflow_versions:
        unverifiable.append("workflow から python-version を 1 件も読めない")
    for version in workflow_versions:
        if version not in used:
            used.append(version)

    table = _parse_eol_releases(eol_text)
    if table is None:
        unverifiable.append("endoflife.date の応答の形が違う")
        return violations, unverifiable

    for version in used:
        eol_from = table.get(version)
        if eol_from is None:
            unverifiable.append(f"Python {ascii(version)} が endoflife.date の応答に無い")
            continue
        if eol_from <= today and f"python-{version}" not in allowed_ids:
            violations.append(
                f"Python {ascii(version)} はサポート終了（eolFrom={eol_from.isoformat()}）"
            )
    return violations, unverifiable


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _default_audit_runner(argv: list[str]) -> object:
    # pip-audit 自身の出力は捨てる（結果は -o の JSON で読む・外部由来の文字列を端末に出さない）
    return subprocess.run(argv, capture_output=True, timeout=AUDIT_TIMEOUT_SECONDS, check=False)


def _default_eol_fetcher(url: str) -> str:
    with urllib.request.urlopen(url, timeout=EOL_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="依存の既知脆弱性（vuln）と Python のサポート終了（eol）を検査する"
    )
    parser.add_argument("checks", nargs="+", choices=CHECKS, metavar="check")
    parser.add_argument("--pyproject", type=Path, default=DEFAULT_PYPROJECT)
    parser.add_argument("--workflows-dir", type=Path, default=DEFAULT_WORKFLOWS_DIR)
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument("--osv-url", default=None)
    parser.add_argument("--eol-url", default=DEFAULT_EOL_URL)
    return parser


def _run_vuln(
    project: dict, osv_url: str | None, allowed: set[str], audit_runner: Callable
) -> tuple[list[str], list[str]]:
    declared = project.get("dependencies", [])
    if not isinstance(declared, list) or not all(isinstance(d, str) for d in declared):
        return [], ["[project].dependencies を読めない"]
    with tempfile.TemporaryDirectory() as tmp:
        requirements = Path(tmp) / "requirements.txt"
        output = Path(tmp) / "audit.json"
        with requirements.open("w", encoding="utf-8") as fh:
            for requirement in declared:
                fh.write(requirement + "\n")
        argv = build_pip_audit_argv(str(requirements), str(output), osv_url)
        try:
            audit_runner(argv)
        except Exception as exc:  # 起動できない・タイムアウト
            return [], [f"pip-audit を実行できない: {ascii(repr(exc))}"]
        try:
            text = output.read_text(encoding="utf-8") if output.exists() else None
        except (OSError, UnicodeDecodeError) as exc:
            return [], [f"pip-audit の JSON を読めない: {ascii(repr(exc))}"]
    return evaluate_audit_json(text, declared, allowed)


def _run_eol(
    project: dict,
    workflows_dir: Path,
    eol_url: str,
    today: datetime.date,
    allowed: set[str],
    eol_fetcher: Callable,
) -> tuple[list[str], list[str]]:
    requires_python = project.get("requires-python")
    if not isinstance(requires_python, str):
        requires_python = None
    versions, unverifiable = collect_workflow_python_versions(workflows_dir)
    try:
        eol_text = eol_fetcher(eol_url)
    except Exception as exc:  # 取得の失敗・タイムアウト
        return [], [*unverifiable, f"endoflife.date を取得できない: {ascii(repr(exc))}"]
    violations, more = check_eol(requires_python, versions, eol_text, today, allowed)
    return violations, [*unverifiable, *more]


def _report(title: str, violations: list[str], unverifiable: list[str]) -> None:
    print(f"[{title}] 違反 {len(violations)} 件・検証不能 {len(unverifiable)} 件")
    for item in violations:
        print(f"  違反: {item}")
    for item in unverifiable:
        print(f"  検証不能: {item}")


def main(
    argv: list[str] | None = None,
    *,
    audit_runner: Callable[[list[str]], object] | None = None,
    eol_fetcher: Callable[[str], str] | None = None,
    today: datetime.date | None = None,
) -> int:
    args = _build_parser().parse_args(argv)
    if audit_runner is None:
        audit_runner = _default_audit_runner
    if eol_fetcher is None:
        eol_fetcher = _default_eol_fetcher
    if today is None:
        today = datetime.datetime.now(datetime.timezone.utc).date()

    checks = list(dict.fromkeys(args.checks))
    any_violation = False
    any_unverifiable = False

    allowed, allow_violations = load_allowlist(args.allowlist, today)
    _report("allowlist", allow_violations, [])
    any_violation |= bool(allow_violations)

    project: dict | None = None
    try:
        data = tomllib.loads(Path(args.pyproject).read_text(encoding="utf-8"))
        project = data.get("project") if isinstance(data.get("project"), dict) else None
        project_error = None if project is not None else "[project] の表が無い"
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        project_error = f"pyproject.toml を読めない: {ascii(repr(exc))}"

    for check in checks:
        if project is None:
            violations, unverifiable = [], [project_error]
        elif check == "vuln":
            violations, unverifiable = _run_vuln(project, args.osv_url, allowed, audit_runner)
        else:
            violations, unverifiable = _run_eol(
                project, args.workflows_dir, args.eol_url, today, allowed, eol_fetcher
            )
        _report(check, violations, unverifiable)
        any_violation |= bool(violations)
        any_unverifiable |= bool(unverifiable)

    if any_violation:
        return 1
    if any_unverifiable:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
