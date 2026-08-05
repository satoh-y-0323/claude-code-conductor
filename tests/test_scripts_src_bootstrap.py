"""
`scripts/` 配下で `c3` を import するファイルが、実行時に実際に import 解決される
`c3` モジュールが repo の `src/c3/` 配下を指すことを **振る舞い検証** で確認する。

architecture-report-20260804-235224.md ADR-10
【改訂 3-b・2026-08-05】検査方式を静的解析から振る舞い検証へ変更する に基づく。

## なぜ静的解析（AST）から振る舞い検証へ切り替えたか

本検査は過去 3 周連続で誤判定を出した:

1. 部分文字列一致 → `Call` 式ラップを見逃す（厳しすぎ）
2. `ast.walk` 全走査 → 無関係な `"src"` に反応（緩すぎ）
3. 既知構造の再帰＋完全一致 → `.join`/`.joinpath` のレシーバ型を見ない・
   `BinOp` を左右どちらか一方が真なら OK にする（緩すぎ）
   （詳細: `.claude/reports/test-report-confirm-e-round3-fixes.md` 検査4）

**根本原因**: `sys.path.insert(0, ...)` の第2引数は任意の式であり、それが実行時に
何を指すかは静的解析では原理的に決まらない
（`get_helper().join("unrelated_prefix", "src")` が何を返すかは実行しないと分からない）。
**決まらない問いを問うていた**のが本質的な誤り。個別の穴を塞ぎ続けても四周目・五周目が
出るため、問いそのものを変える: 構文ではなく**実際の解決先**を検査する。

## 検査内容

1. `scripts/*.py` のうち `c3` を import しているファイルを動的に列挙する
   （ハードコードしない。3本目が増えても黙って検査を免れない）
2. 各ファイルについて、`PYTHONPATH` を明示的に除いた subprocess で
   `runpy.run_path(<file>, run_name=<"__main__" 以外>)` によりモジュールレベルの
   コード（bootstrap 行を含む）を実行し、`sys.modules["c3"].__file__` を取得する
   （`run_name` を `"__main__"` 以外にすることで `if __name__ == "__main__":` 配下の
   処理は実行されない。ADR-10 改訂3-b の前提: 対象スクリプトは import 時に
   副作用を持たない = 現行2本は実測済み）
3. その `__file__` が repo の `src/c3/` 配下であることを確認する。
   site-packages 等それ以外を指していたら違反とする

書き方（`Path` / `os.path.join` / 自作ヘルパー）に一切依存せず、ADR-10 が保証したい
性質（配布元の実運用経路が repo の src/ を読むこと）そのものを直接測る。

## 前提と限界（正直に記録する）

- 対象スクリプトが `if __name__ == "__main__":` ガードを持ち、import 時に副作用が
  無いことが前提。将来 `scripts/` に import 時点で重い処理や外部依存を持つものが
  増えたら本方式は使えず再設計が要る
- 「原理的に穴がゼロ」ではなく「この問いに対しては正しい道具」という位置づけ
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
SRC_C3_DIR = (REPO_ROOT / "src" / "c3").resolve()

_SUBPROCESS_TIMEOUT_SECONDS = 15.0

_PROBE_CODE_TEMPLATE = """\
import runpy, sys
runpy.run_path({path!r}, run_name="c3_bootstrap_probe")
_c3 = sys.modules.get("c3")
print(_c3.__file__ if _c3 is not None else "__C3_NOT_IMPORTED__")
"""


# ---------------------------------------------------------------------------
# 対象ファイルの動的列挙
#
# これは「sys.path.insert がどこを指すか」という決まらない問いではなく、
# 「このファイルが c3 を import する構文を含むか」という静的に決定可能な問いの
# ため、AST のままでよい（振る舞い検証への切替の対象外）。
# ---------------------------------------------------------------------------


def _find_c3_imports(tree: ast.Module) -> list[int]:
    """`c3` を import している行番号のリストを返す。

    対象:
      - `import c3`
      - `import c3.db`
      - `from c3 import ...`
      - `from c3.db import ...`
    """
    import_lines: list[int] = []

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "c3" or alias.name.startswith("c3."):
                    import_lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "c3" or node.module.startswith("c3.")):
                import_lines.append(node.lineno)

    return import_lines


def _iter_scripts_importing_c3() -> list[Path]:
    """`scripts/*.py` のうち `c3` を import しているファイルを動的に列挙する。

    ハードコードされたファイル名リストに依存すると、将来 `scripts/` 配下に
    第3のファイルが追加され `c3` を import しても、リストへの追記漏れで
    この検査を黙ってすり抜ける（CR-NEW Medium 実測・E周回2）。走査結果を毎回
    AST で判定することでこれを防ぐ。
    """
    found: list[Path] = []
    for f in sorted(SCRIPTS_DIR.glob("*.py")):
        try:
            text = f.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(f))
        except (UnicodeDecodeError, OSError, SyntaxError):
            continue
        if _find_c3_imports(tree):
            found.append(f)
    return found


# ---------------------------------------------------------------------------
# 振る舞い検証本体
# ---------------------------------------------------------------------------


def resolve_c3_file_via_subprocess(
    script_path: Path,
    timeout: float = _SUBPROCESS_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """`script_path` のモジュールレベルコードを `PYTHONPATH` なしの subprocess で
    実行し、そこで import 解決される `c3.__file__` が repo の `src/c3/` 配下を
    指すかを検証する（振る舞い検証・ADR-10 改訂3-b）。

    純粋関数として対象パスを引数で受けるため、実運用ファイルだけでなく
    合成スクリプト（tmp_path 上のダミー）にもそのまま使える。

    returns: (有効?, エラーメッセージ)
      - 有効: (True, "")
      - 無効: (False, "<理由>")
    """
    env = os.environ.copy()
    # 親プロセスに PYTHONPATH が設定されていると、検査対象スクリプト自身の
    # ブートストラップの有無に関わらず c3 が解決できてしまい、検査が無意味に
    # なる。明示的に除いて実行する。
    env.pop("PYTHONPATH", None)

    code = _PROBE_CODE_TEMPLATE.format(path=str(script_path))
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            cwd=str(REPO_ROOT),
        )
    except subprocess.TimeoutExpired:
        return False, f"{script_path} の import 検証が {timeout} 秒でタイムアウトしました"

    if result.returncode != 0:
        return False, (
            f"{script_path} の import 検証プロセスが異常終了しました "
            f"(rc={result.returncode}): {result.stderr.strip()}"
        )

    resolved = result.stdout.strip()
    if not resolved or resolved == "__C3_NOT_IMPORTED__":
        return False, f"{script_path} を実行しても c3 が import されませんでした"

    resolved_path = Path(resolved).resolve()
    try:
        resolved_path.relative_to(SRC_C3_DIR)
    except ValueError:
        return False, (
            f"{script_path} の import は {resolved_path} を指しており、"
            f"repo の src/c3/ 配下（{SRC_C3_DIR}）ではありません"
        )
    return True, ""


# ---------------------------------------------------------------------------
# 本検査（追加時点で緑）
# ---------------------------------------------------------------------------


class TestScriptsResolveC3FromRepoSrc:
    """scripts/ 層の c3 import 解決先を振る舞い検証する。"""

    def test_target_set_is_not_empty(self):
        """走査対象の空集合ガード（typo による検査の空回りを防ぐ）。"""
        targets = _iter_scripts_importing_c3()
        assert targets, f"{SCRIPTS_DIR} 配下に c3 を import するファイルが1件も見つかりません"

    def test_target_set_includes_known_sentinel_files(self):
        """番兵: 既知の2ファイルが動的列挙結果に含まれること
        （動的列挙そのものが壊れていないことの裏取り）。
        """
        targets = {f.name for f in _iter_scripts_importing_c3()}
        required_files = {"audit_review_decisions.py", "check_deletions.py"}
        missing = required_files - targets
        assert not missing, (
            f"動的列挙結果に含まれていません: {missing}\n実測: {targets}"
        )

    def test_real_scripts_resolve_c3_from_repo_src(self):
        """誤検出しない側: `c3` を import する `scripts/*.py` 全件について、
        subprocess 実測で解決先が repo の src/c3/ 配下であることを確認する。
        """
        targets = _iter_scripts_importing_c3()
        assert targets, "走査対象が空です（test_target_set_is_not_empty で先に落ちるはず）"

        failures: list[str] = []
        for f in targets:
            ok, message = resolve_c3_file_via_subprocess(f)
            if not ok:
                failures.append(message)

        assert not failures, "c3 import 解決先の検証に失敗:\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# resolve_c3_file_via_subprocess() 単体の検出力（合成スクリプト）
# ---------------------------------------------------------------------------


class TestResolveC3FileViaSubprocessDetectsViolations:
    """`resolve_c3_file_via_subprocess()` 自体が振る舞いレベルで違反を検出できる
    ことの合成スクリプトでの裏取り。実運用ファイル（現状2件とも正しい）だけでは
    この関数の検出力そのものは裏取りできないため、合成スクリプトで直接検証する。

    - 「見逃さない」: ブートストラップが無い/壊れている/誤った場所を指す
      スクリプトは違反として検出される
    - 「誤検出しない」: 実運用と同型の正しいブートストラップは合格する
    の両方を検証する（片側だけの確認は不合格・plan §4）。
    """

    # --- 見逃さない（fail-open が無いこと） --------------------------------

    def test_missing_bootstrap_is_detected(self, tmp_path):
        """`sys.path.insert` が一切無いスクリプトは、site-packages 等の c3 を
        掴んでしまい違反として検出される。
        """
        f = tmp_path / "no_bootstrap.py"
        f.write_text("import c3\n", encoding="utf-8")
        ok, message = resolve_c3_file_via_subprocess(f)
        assert ok is False, "ブートストラップ無しのスクリプトが誤って合格しました"
        assert message

    def test_bootstrap_after_c3_import_is_detected(self, tmp_path):
        """`sys.path.insert` が `c3` の import より後にあると間に合わず違反になる。"""
        f = tmp_path / "wrong_order.py"
        f.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "import c3\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))\n",
            encoding="utf-8",
        )
        ok, message = resolve_c3_file_via_subprocess(f)
        assert ok is False, "順序違反のブートストラップが誤って合格しました"

    def test_bootstrap_pointing_to_wrong_dir_is_detected(self, tmp_path):
        """`src` ではなく別ディレクトリを insert すると違反になる。"""
        f = tmp_path / "wrong_dir.py"
        f.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'not_src'))\n"
            "import c3\n",
            encoding="utf-8",
        )
        ok, message = resolve_c3_file_via_subprocess(f)
        assert ok is False, "誤ったディレクトリへの insert が誤って合格しました"

    def test_pythonpath_leak_does_not_mask_missing_bootstrap(self, tmp_path, monkeypatch):
        """親プロセスに `PYTHONPATH=<repo>/src` が漏れていても、検査は明示的に
        それを除いて実行するため、ブートストラップの無いスクリプトは正しく
        違反として検出され続ける。

        実測（本タスク内で事前検証済み）: この `env.pop("PYTHONPATH", None)` を
        行わずに実行すると、ブートストラップの無いスクリプトでも漏れた
        `PYTHONPATH` 経由で repo の src を掴んでしまい、誤って合格する
        （fail-open）。除去することでこれを防いでいることを実測する。
        """
        monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT / "src"))
        f = tmp_path / "no_bootstrap_with_leaked_pythonpath.py"
        f.write_text("import c3\n", encoding="utf-8")
        ok, message = resolve_c3_file_via_subprocess(f)
        assert ok is False, (
            "PYTHONPATH が除去されずに漏れ、ブートストラップ無しのスクリプトが"
            f"誤って合格しました: {message}"
        )

    # --- 誤検出しない（過剰検出の非回帰） ------------------------------

    def test_valid_bootstrap_is_accepted(self, tmp_path):
        """実運用と同型の正しいブートストラップは合格する。"""
        f = tmp_path / "ok.py"
        f.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, str(Path({str(REPO_ROOT)!r}) / 'src'))\n"
            "import c3\n",
            encoding="utf-8",
        )
        ok, message = resolve_c3_file_via_subprocess(f)
        assert ok is True, f"正しいブートストラップが合格しません: {message}"

    def test_file_without_c3_import_is_exempt_from_enumeration(self):
        """`c3` を import しないファイルは、動的列挙が使う `_find_c3_imports()`
        で import 行なしと判定され、対象（振る舞い検証の対象）から除外される。
        """
        tree = ast.parse("import os\n")
        assert _find_c3_imports(tree) == []
