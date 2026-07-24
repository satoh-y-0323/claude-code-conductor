"""Tests for the shared exclusion list (``c3._excludes``)."""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from c3._excludes import EXCLUDE_PATTERNS, KEEP_PATTERNS, should_skip

REPO_ROOT = Path(__file__).resolve().parent.parent
HATCH_BUILD_PATH = REPO_ROOT / "hatch_build.py"
MODE_LINE_SCRIPT = (
    REPO_ROOT / ".claude" / "skills" / "autonomous-mode" / "scripts" / "mode_line.py"
)


def test_keeps_framework_files():
    assert not should_skip("agents/architect.md")
    assert not should_skip("skills/dev-workflow.md")
    assert not should_skip("commands/develop.md")
    assert not should_skip("hooks/pre_tool.py")
    assert not should_skip("skills/dev-workflow/references/code-review-checklist.md")
    assert not should_skip("settings.json")
    assert not should_skip("CLAUDE.md")
    assert not should_skip("docs/settings.json.md")


def test_excludes_personal_files():
    assert should_skip("reports/plan-report-20260427-232152.md")
    assert should_skip("reports/test-report-20260429-203045.md")
    assert should_skip("memory/sessions/20260427.tmp")
    assert should_skip("memory/sessions/20260501.tmp")
    assert should_skip("memory/patterns.json")
    assert should_skip("memory/agent-audit.log")
    assert should_skip("tmp/scratch.txt")
    assert should_skip("docs/decisions.md")
    assert should_skip("docs/taxonomy.md")
    assert should_skip("docs/game-studios-research.md")
    assert should_skip("settings.local.json")


def test_excludes_pycache_at_any_depth():
    """``__pycache__/*.pyc`` artefacts must never ship in the wheel or be
    copied by ``c3 init`` / ``c3 update``. They appear when the dev or
    user runs hooks locally; the build hook reads from the filesystem
    so without an explicit rule they sneak into the bundle.
    """
    assert should_skip("hooks/__pycache__/pre_tool.cpython-311.pyc")
    assert should_skip("__pycache__/foo.pyc")
    assert should_skip("hooks/__pycache__/stop.cpython-311.pyo")
    assert should_skip("agents/sub/__pycache__/x.pyc")
    # bare .pyc/.pyo (legacy layout) are also filtered defensively
    assert should_skip("hooks/legacy.pyc")
    assert should_skip("hooks/legacy.pyo")
    # but .py source files are framework files and stay
    assert not should_skip("hooks/pre_tool.py")


def test_keep_overrides_exclude_for_gitkeep():
    assert not should_skip("reports/.gitkeep")
    assert not should_skip("memory/.gitkeep")
    assert not should_skip("memory/sessions/.gitkeep")
    assert not should_skip("tmp/.gitkeep")


def test_keep_patterns_actually_protect_against_excludes():
    """KEEP_PATTERNS exist to defend specific paths. They are still useful
    even when not strictly needed today (defense against future EXCLUDE
    additions). This test just confirms the KEEP list is non-empty and
    every entry passes the should_skip filter.
    """
    assert KEEP_PATTERNS, "KEEP_PATTERNS should not be empty"
    for keep in KEEP_PATTERNS:
        assert not should_skip(keep), f"{keep!r} should be retained"


# ---------------------------------------------------------------------------
# T4-1: 3 ファイル同期の同一性検査（architecture-report-20260714-213000.md §6）
#
# hatch_build.py はリポジトリルート直下にあり package 化されていないため
# importlib.util でファイルパスから直接ロードする。hatch_build.py は
# ``hatchling.builders.hooks.plugin.interface`` に依存する import 文を持つため、
# hatchling 未インストール環境では exec_module が ImportError で失敗し得る。
# その場合は正規表現でタプル本文を抽出して比較するテキスト比較にフォールバックする
# （どちらの経路でも同期漏れを検知できることが受け入れ条件）。
# ---------------------------------------------------------------------------

def _load_hatch_build_patterns_via_importlib() -> tuple[tuple[str, ...], tuple[str, ...]]:
    spec = importlib.util.spec_from_file_location(
        "_hatch_build_under_test_excludes", HATCH_BUILD_PATH
    )
    assert spec and spec.loader, f"could not build import spec for {HATCH_BUILD_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EXCLUDE_PATTERNS, module.KEEP_PATTERNS


def _strip_comment_lines(body: str) -> str:
    """タプル本文から行頭コメント行を除去する。

    ``# v2.14.1: ... isolation:"worktree" で生成する ...`` のような日本語コメント行は
    引用符付き文字列（``"worktree"``）を含むため、コメントを除去せず素朴に
    ``"..."`` を正規表現抽出すると誤ってコメント内の引用符もパターンとして
    拾ってしまう（実機で確認済みの落とし穴）。行頭が ``#`` の行は丸ごと除外する。
    """
    return "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )


def _extract_pattern_tuple_via_regex(text: str, const_name: str) -> tuple[str, ...]:
    """``NAME: tuple[str, ...] = (\n    "a",\n    "b",\n)`` 形式のタプル本文から
    文字列リテラルだけを正規表現で抽出する（フォールバック比較用）。
    """
    match = re.search(
        rf"{const_name}\s*:\s*tuple\[str,\s*\.\.\.\]\s*=\s*\((.*?)\n\)",
        text,
        re.DOTALL,
    )
    assert match, f"{const_name} definition not found via regex fallback"
    body = _strip_comment_lines(match.group(1))
    return tuple(re.findall(r'"((?:[^"\\]|\\.)*)"', body))


def _load_hatch_build_patterns_via_regex_fallback() -> tuple[tuple[str, ...], tuple[str, ...]]:
    text = HATCH_BUILD_PATH.read_text(encoding="utf-8")
    exclude = _extract_pattern_tuple_via_regex(text, "EXCLUDE_PATTERNS")
    keep = _extract_pattern_tuple_via_regex(text, "KEEP_PATTERNS")
    return exclude, keep


def _load_hatch_build_patterns() -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        return _load_hatch_build_patterns_via_importlib()
    except ImportError:
        # hatchling 未インストール環境（BuildHookInterface が import できない）
        return _load_hatch_build_patterns_via_regex_fallback()


def test_hatch_build_exclude_patterns_match_c3_excludes():
    """hatch_build.py の EXCLUDE_PATTERNS は c3._excludes.EXCLUDE_PATTERNS と
    タプル完全一致でなければならない（CLAUDE.md §2 の 3 ファイル同期グループ）。
    過去 defect: v1.1.0 で state/tier_selection.json が wheel に混入した
    （3 ファイル間の手動同期漏れ）。
    """
    hatch_exclude, _hatch_keep = _load_hatch_build_patterns()
    assert hatch_exclude == EXCLUDE_PATTERNS


def test_hatch_build_keep_patterns_match_c3_excludes():
    """KEEP_PATTERNS も同様にタプル完全一致でなければならない。"""
    _hatch_exclude, hatch_keep = _load_hatch_build_patterns()
    assert hatch_keep == KEEP_PATTERNS


# ---------------------------------------------------------------------------
# T4-2: autonomous-mode skill 配布対象検査（architecture-report-20260714-213000.md §9-1）
#
# v2.53.0 配布切替で autonomous-mode skill の除外定義を削除し配布対象化した
# （旧仕様: 配布除外での熟成対象）。SKILL.md 本体もサブパスも配布対象になる。
# dev-workflow は巻き込み事故がないことも併せて検査する。
# ---------------------------------------------------------------------------

def test_autonomous_mode_skill_is_included_in_distribution():
    # v2.53.0 配布切替で除外解除された（旧: should_skip(...) is True）
    assert should_skip("skills/autonomous-mode/SKILL.md") is False
    assert should_skip("skills/autonomous-mode/scripts/mode_line.py") is False


def test_dev_workflow_skill_not_caught_up_in_autonomous_mode_exclusion():
    assert should_skip("skills/dev-workflow/SKILL.md") is False


# ---------------------------------------------------------------------------
# T4-3: モード行有効性判定の純関数テスト（DC-GP-005・architecture §3-3/§6）
#
# 位置づけ: この純関数（.claude/skills/autonomous-mode/scripts/mode_line.py）は
# 実運用のデータフロー（LLM が「モード:」行を解釈する経路）からは呼ばれない。
# 誤発動ゼロの一次防御は test-report に列挙する 5a 手動検証チェックリストであり、
# 本テストは判定仕様の回帰検出を担う補助（仕様のリファレンス実装）である。
#
# 「有効宣言 → 有効」の assert は実環境 ``~/.claude/plans/`` の実在に依存させない
# （サイクル 3 DC-GP-001）。tmp_path に許可ルート相当ディレクトリとダミー plan を
# 作成し、``DEFAULT_ALLOWED_ROOT`` を monkeypatch で差し替えてから判定を呼ぶ。
# ---------------------------------------------------------------------------

def _load_mode_line_module():
    if not MODE_LINE_SCRIPT.is_file():
        raise FileNotFoundError(f"mode_line.py not found: {MODE_LINE_SCRIPT}")
    spec = importlib.util.spec_from_file_location(
        "_mode_line_under_test_excludes", MODE_LINE_SCRIPT
    )
    assert spec and spec.loader, f"could not build import spec for {MODE_LINE_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_missing_plan_token_is_invalid():
    """(ii) 「モード: 自律」に plan= が欠けている場合は無効（HITL）。"""
    mode_line = _load_mode_line_module()
    assert mode_line.is_valid_autonomous_mode("モード: 自律") is False
    assert mode_line.is_valid_autonomous_mode("モード: 自律 cycles=C-3/1") is False


@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_nonexistent_plan_is_invalid(tmp_path, monkeypatch):
    """(iii) plan= はあるが指す先のファイルが実在しない場合は無効（HITL）。"""
    mode_line = _load_mode_line_module()
    allowed_root = tmp_path / "plans"
    allowed_root.mkdir()
    monkeypatch.setattr(mode_line, "DEFAULT_ALLOWED_ROOT", str(allowed_root))

    missing_plan = allowed_root / "does-not-exist.md"
    line = f"モード: 自律 plan={missing_plan}"
    assert mode_line.is_valid_autonomous_mode(line) is False


@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_outside_allowed_root_is_invalid(tmp_path, monkeypatch):
    """(iv) plan= が許可ルート外の実在ファイルを指す場合も無効（HITL）。
    誤発動ゼロの穴（任意の実在ファイルを plan= に指定するだけで有効化される）を
    塞ぐ封じ込め型防御（許可ルート配下判定）の回帰検出。
    """
    mode_line = _load_mode_line_module()
    allowed_root = tmp_path / "plans"
    allowed_root.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_plan = outside_dir / "sneaky.md"
    outside_plan.write_text("dummy", encoding="utf-8")
    monkeypatch.setattr(mode_line, "DEFAULT_ALLOWED_ROOT", str(allowed_root))

    line = f"モード: 自律 plan={outside_plan}"
    assert mode_line.is_valid_autonomous_mode(line) is False


@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_valid_declaration_is_valid(tmp_path, monkeypatch):
    """(v) 許可ルート配下の実在 plan を指す有効宣言は有効（ゲート付け替え対象）。
    ``~/.claude/plans/`` の実在に依存しない密閉化（サイクル 3 DC-GP-001）。
    """
    mode_line = _load_mode_line_module()
    allowed_root = tmp_path / "plans"
    allowed_root.mkdir()
    dummy_plan = allowed_root / "dazzling-cooking-dusk.md"
    dummy_plan.write_text("# dummy plan", encoding="utf-8")
    monkeypatch.setattr(mode_line, "DEFAULT_ALLOWED_ROOT", str(allowed_root))

    line = f"モード: 自律 plan={dummy_plan}"
    assert mode_line.is_valid_autonomous_mode(line) is True


@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_extracts_plan_path_with_spaces(tmp_path, monkeypatch):
    """CR-NEW-4 / SR-NEW: 空白を含む plan-path を正しく抽出する。

    _extract_plan_path は「plan= 以降を行末まで取り込み、後続に " cycles=" トークン
    がある場合のみ分離する」正規表現方式で、スペース含みパスに対応する。
    T2 引用符対応により _extract_plan_path は (path, reason_code) tuple を返す。
    """
    mode_line = _load_mode_line_module()
    allowed_root = tmp_path / "plans"
    allowed_root.mkdir()
    plan_with_spaces = allowed_root / "my plan.md"
    plan_with_spaces.write_text("# plan", encoding="utf-8")
    monkeypatch.setattr(mode_line, "DEFAULT_ALLOWED_ROOT", str(allowed_root))

    # スペース入りパスの抽出テスト
    rest1 = f"plan={plan_with_spaces}"
    extracted1, reason1 = mode_line._extract_plan_path(rest1)
    assert extracted1 == str(plan_with_spaces), f"expected {plan_with_spaces}, got {extracted1}"
    assert reason1 is None, f"expected no reason code, got {reason1}"

    # スペース入りパス + cycles= トークンの抽出テスト
    rest2 = f"plan={plan_with_spaces} cycles=C-3/1"
    extracted2, reason2 = mode_line._extract_plan_path(rest2)
    assert extracted2 == str(plan_with_spaces), f"expected {plan_with_spaces}, got {extracted2}"
    assert reason2 is None, f"expected no reason code, got {reason2}"

    # スペース入りパスが有効宣言として機能する
    line = f"モード: 自律 plan={plan_with_spaces}"
    assert mode_line.is_valid_autonomous_mode(line) is True


@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_rejects_symlink_to_outside_allowed_root(tmp_path, monkeypatch):
    """SR-V-002: symlink/junction による許可ルート偽装ケース。

    tmp_path 内許可ルートに、許可ルート外の実在ファイルを指す symlink を作成。
    is_valid_autonomous_mode が False を返す（封じ込め防御が効いている）ことを
    確認する。os.symlink が失敗する環境（Windows 非管理者等）では、管理者権限
    不要な NTFS ディレクトリ junction（``_winapi.CreateJunction``）にフォールバック
    して同じ偽装構図を作る。junction も使えない環境（非 Windows で symlink 不可等）
    のみ skip する。
    """
    mode_line = _load_mode_line_module()
    allowed_root = tmp_path / "plans"
    allowed_root.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_plan = outside_dir / "real-plan.md"
    outside_plan.write_text("# real plan", encoding="utf-8")

    monkeypatch.setattr(mode_line, "DEFAULT_ALLOWED_ROOT", str(allowed_root))

    # 第 1 経路: 許可ルート内にファイル symlink を作成し、許可ルート外の実ファイルを指す
    symlink_path = allowed_root / "sneaky-link.md"
    try:
        os.symlink(outside_plan, symlink_path)
    except (OSError, NotImplementedError) as symlink_err:
        # 第 2 経路（Windows 非特権フォールバック）: 許可ルート内に junction を作り、
        # 許可ルート外のディレクトリを指す。plan は junction 越しのパスで指定する。
        try:
            import _winapi
        except ImportError:
            pytest.skip(f"os.symlink unsupported and _winapi unavailable: {symlink_err}")
        if not hasattr(_winapi, "CreateJunction"):
            pytest.skip(f"os.symlink unsupported and _winapi.CreateJunction missing: {symlink_err}")
        junction_dir = allowed_root / "sneaky-junction"
        try:
            _winapi.CreateJunction(str(outside_dir), str(junction_dir))
        except (OSError, NotImplementedError) as junction_err:
            pytest.skip(
                f"neither os.symlink nor CreateJunction usable: "
                f"symlink={symlink_err} junction={junction_err}"
            )
        # junction 越しに許可ルート外の実ファイルを指すパス
        sneaky_plan = junction_dir / "real-plan.md"
        assert sneaky_plan.is_file(), "junction 経由で外部ファイルが見えること"
        line = f"モード: 自律 plan={sneaky_plan}"
        assert mode_line.is_valid_autonomous_mode(line) is False
        return

    # symlink を指す行は無効化される（realpath 正規化がシンボリックリンク解決）
    line = f"モード: 自律 plan={symlink_path}"
    assert mode_line.is_valid_autonomous_mode(line) is False


# ---------------------------------------------------------------------------
# T4-3b: mode_line.py の CLI 呼び出し契約テスト（SR-AI-001・Round 3）
#
# SKILL.md（init-session Step5 / autonomous-mode 必須チェック(b)）が指示する
# 「Bash 経由で mode_line.py を呼び出して判定を得る」経路を回帰検出する。
# 標準入力にモード行 1 行を渡し、stdout の機械可読 1 行（VALID/INVALID + TAB 区切り）
# と exit code（0/1）を検査する。skipif は他の T4-3 テストと同一条件。
# ---------------------------------------------------------------------------


def _run_mode_line_cli(mode_line_text: str, env: dict | None = None) -> subprocess.CompletedProcess:
    # encoding="utf-8" を明示する。text=True の既定 locale エンコーディング（Windows は
    # cp932）だと、実運用の Bash ツール経由（UTF-8 バイト列）と挙動が食い違うため。
    # mode_line.py は stdin を UTF-8 に reconfigure しており、その契約に揃える。
    return subprocess.run(
        [sys.executable, str(MODE_LINE_SCRIPT)],
        input=mode_line_text,
        capture_output=True,
        encoding="utf-8",
        env=env,
    )


@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_cli_invalid_missing_plan_token(tmp_path):
    """CLI 契約: plan= 欠落のモード行は INVALID + 理由コード + exit 1。"""
    result = _run_mode_line_cli("モード: 自律 cycles=C-3/1")
    assert result.returncode == 1, result.stderr
    line = result.stdout.rstrip("\r\n")
    status, _, detail = line.partition("\t")
    assert status == "INVALID"
    assert detail == "no_plan_token"


@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_cli_valid_declaration(tmp_path):
    """CLI 契約: 許可ルート配下の実在 plan を指す有効宣言は VALID + 正規化パス + exit 0。

    子プロセスは ``DEFAULT_ALLOWED_ROOT = ~/.claude/plans`` を実行時に評価するため、
    ``USERPROFILE`` / ``HOME`` を tmp_path に差し替えて許可ルートを密閉化する
    （実 ``~/.claude/plans`` の実在に依存しない）。
    """
    fake_home = tmp_path
    allowed_root = fake_home / ".claude" / "plans"
    allowed_root.mkdir(parents=True)
    plan_file = allowed_root / "dazzling-cooking-dusk.md"
    plan_file.write_text("# dummy plan", encoding="utf-8")

    env = dict(os.environ)
    env["USERPROFILE"] = str(fake_home)
    env["HOME"] = str(fake_home)

    result = _run_mode_line_cli(f"モード: 自律 plan={plan_file}", env=env)
    assert result.returncode == 0, result.stderr
    line = result.stdout.rstrip("\r\n")
    status, _, detail = line.partition("\t")
    assert status == "VALID"
    # 正規化済み絶対パスが返り、実 plan ファイルに解決する
    assert os.path.realpath(detail) == os.path.realpath(str(plan_file))


@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_cli_invalid_utf8_decode_error(tmp_path):
    """CLI 契約（SR-NEW-1）: 不正 UTF-8 バイト列 stdin は生の例外を出さず
    INVALID\tdecode_error / exit 1 の契約内に封じ込める（fail-closed）。"""
    # bytes 入力を渡すため text デコードを経由しない（encoding 未指定 = bytes モード）。
    result = subprocess.run(
        [sys.executable, str(MODE_LINE_SCRIPT)],
        input=b"\xff\xfe\x00\x80 not valid utf-8",
        capture_output=True,
    )
    assert result.returncode == 1, result.stderr
    line = result.stdout.decode("utf-8", errors="replace").rstrip("\r\n")
    status, _, detail = line.partition("\t")
    assert status == "INVALID"
    assert detail == "decode_error"


@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_cli_valid_with_trailing_newline(tmp_path):
    """CLI 契約: grep パイプ経路相当（trailing 改行付き）の有効宣言も VALID / exit 0。

    grep -m1 でセッションファイルから抽出した行は末尾に改行が付いて届くため、
    trailing 改行を除去してから判定する挙動を回帰検出する。
    """
    fake_home = tmp_path
    allowed_root = fake_home / ".claude" / "plans"
    allowed_root.mkdir(parents=True)
    plan_file = allowed_root / "dazzling-cooking-dusk.md"
    plan_file.write_text("# dummy plan", encoding="utf-8")

    env = dict(os.environ)
    env["USERPROFILE"] = str(fake_home)
    env["HOME"] = str(fake_home)

    # grep パイプ相当: 抽出行は trailing 改行付きで mode_line.py に届く
    result = _run_mode_line_cli(f"モード: 自律 plan={plan_file}\n", env=env)
    assert result.returncode == 0, result.stderr
    line = result.stdout.rstrip("\r\n")
    status, _, detail = line.partition("\t")
    assert status == "VALID"
    assert os.path.realpath(detail) == os.path.realpath(str(plan_file))
