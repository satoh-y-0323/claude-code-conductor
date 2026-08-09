"""Red フェーズ: `.claude/skills/start/scripts/archive_reports.py` の実挙動テスト。

上流契約（SSOT）:
- `.claude/reports/architecture-report-20260809-154124.md`（改訂 2）
  §0 INV-1〜INV-4 / ADR-2 / ADR-3 / §3-1〜§3-3c
- `.claude/reports/plan-report-20260809-164111.md` の `test-archive` タスク（性質 1〜12）

本ファイルの全テストは **必ず `--reports-dir` に `tmp_path` を渡す**。既定値経路
（実リポジトリの `.claude/reports`）は検査対象外である。既定値を使うテストを 1 本でも
書くと、フルスイート実行のたびに実リポジトリのレポートが `archive/` へ移動する。

起動方式は設計 §3-3b のとおり in-process import（`importlib.util.spec_from_file_location`）で
`main(argv) -> int` を呼び、**戻り値を exit code として扱う**。subprocess は使わない
（失敗注入と exit code 検査を同じ経路で測るため）。

検査対象スクリプトのパスは環境変数 `C3_ARCHIVE_SCRIPT_PATH` で差し替えられる
（confirm フェーズがスクリプトの写しにスタブを当てて走らせるため）。未設定時は実体パスを使う。

Red の理由: `archive_reports.py` が未実装のため `load_archive_module()` が
`FileNotFoundError` を送出して全件 failed になる。これは機能未実装による正しい失敗である。
"""

from __future__ import annotations

import importlib.util
import io
import itertools
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 検査対象スクリプトの解決 & in-process ロード
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCRIPT_PATH = (
    REPO_ROOT / ".claude" / "skills" / "start" / "scripts" / "archive_reports.py"
)
SCRIPT_PATH_ENV = "C3_ARCHIVE_SCRIPT_PATH"

_LOAD_COUNTER = itertools.count()


def script_path() -> Path:
    """検査対象スクリプトの実パスを返す。

    `C3_ARCHIVE_SCRIPT_PATH` が設定されていればそちらを優先する（confirm フェーズの
    スタブ検査用の seam）。環境変数は **呼び出しのたびに読む**（import 時に固定しない）。
    """
    override = os.environ.get(SCRIPT_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return DEFAULT_SCRIPT_PATH


def load_archive_module():
    """`archive_reports.py` を in-process import して返す（テストごとに新規ロード）。

    未実装の間は `FileNotFoundError` を送出する。`pytest.mark.skipif` を使わないのは、
    skip では「失敗する Red」の証跡が残らないため。
    """
    path = script_path()
    if not path.is_file():
        raise FileNotFoundError(
            f"archive_reports.py not found: {path}\n"
            "Red フェーズでは未実装のため、この失敗が期待される理由である。"
        )
    spec = importlib.util.spec_from_file_location(
        f"_archive_reports_under_test_{next(_LOAD_COUNTER)}", path
    )
    assert spec is not None and spec.loader is not None, (
        f"could not build import spec for {path}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 固定 mtime（実行時刻依存のフレークを潰す）
#
# ADR-2 の接尾辞は **ローカル時刻** で組む。UTC で組むと JST で 9 時間ずれる。
# `datetime(...)` は naive のためローカル時刻として解釈され、`.timestamp()` は
# その壁時計に対応する epoch 秒を返す。したがって
# `datetime.fromtimestamp(FIXED_MTIME_A).strftime("%Y%m%d-%H%M%S") == STAMP_A` が
# どのタイムゾーンでも成立する（下の自己整合 assert で固定する）。
# ---------------------------------------------------------------------------

FIXED_MTIME_A = datetime(2026, 8, 8, 22, 15, 30).timestamp()
STAMP_A = "20260808-221530"

FIXED_MTIME_B = datetime(2026, 8, 7, 9, 5, 1).timestamp()
STAMP_B = "20260807-090501"

# 改行変換をすり抜けさせないため、サンプルの 1 件は必ず CRLF を含める（INV-4）。
CRLF_BODY = b"# report\r\n\r\n- \xe6\x97\xa5\xe6\x9c\xac\xe8\xaa\x9e\r\nend\r\n"
LF_BODY = b"# report\n\n- lf only\nend\n"


def _assert_stamp_helpers_are_self_consistent() -> None:
    """期待接尾辞がローカル時刻由来であることの自己整合チェック。"""
    assert datetime.fromtimestamp(FIXED_MTIME_A).strftime("%Y%m%d-%H%M%S") == STAMP_A
    assert datetime.fromtimestamp(FIXED_MTIME_B).strftime("%Y%m%d-%H%M%S") == STAMP_B


# ---------------------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------------------


def make_reports_dir(tmp_path: Path) -> Path:
    """`--reports-dir` に渡す一時ディレクトリを作って返す。"""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    return reports_dir


def write_report(directory: Path, name: str, data: bytes, mtime: float | None = None) -> Path:
    """`directory/name` にバイト列で書き、必要なら mtime を固定する。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def run_main(module, *argv: str) -> int:
    """`main(argv)` を呼び、戻り値（= exit code）を返す。"""
    return module.main(list(argv))


# ---------------------------------------------------------------------------
# 性質 1 / 2 / 7（衝突時）: 既存は 1 バイトも変わらず、移動元は別名でバイト同一に残る
# ---------------------------------------------------------------------------


class TestCollisionKeepsBothCopies:
    """ADR-2 / ADR-3 / INV-1 / INV-4。"""

    def test_existing_archive_file_is_byte_identical_after_run(self, tmp_path):
        """性質 1: `archive/` に同名・別内容がある状態で実行しても既存が 1 バイトも変わらない。"""
        module = load_archive_module()
        _assert_stamp_helpers_are_self_consistent()

        reports_dir = make_reports_dir(tmp_path)
        archive_dir = reports_dir / "archive"
        name = "plan-report-20260808-101112.md"

        existing_bytes = b"OLD VERSION\nkeep me exactly\n"
        write_report(archive_dir, name, existing_bytes)
        write_report(reports_dir, name, CRLF_BODY, mtime=FIXED_MTIME_A)

        rc = run_main(module, "--reports-dir", str(reports_dir))

        assert rc == 0, "衝突は失敗ではない（リネームして両方残すのが ADR-2）"
        assert (archive_dir / name).read_bytes() == existing_bytes, (
            "INV-1 違反: 既存の archive ファイルが書き換わった"
        )

    def test_source_survives_under_renamed_name_with_identical_bytes(self, tmp_path):
        """性質 2: 衝突時、移動元の内容が ADR-2 の別名で `archive/` にバイト同一で存在する。"""
        module = load_archive_module()
        _assert_stamp_helpers_are_self_consistent()

        reports_dir = make_reports_dir(tmp_path)
        archive_dir = reports_dir / "archive"
        name = "plan-report-20260808-101112.md"

        write_report(archive_dir, name, b"OLD VERSION\n")
        write_report(reports_dir, name, CRLF_BODY, mtime=FIXED_MTIME_A)

        rc = run_main(module, "--reports-dir", str(reports_dir))
        assert rc == 0

        renamed = archive_dir / f"plan-report-20260808-101112-archived-{STAMP_A}.md"
        assert renamed.is_file(), (
            "ADR-2 の別名が見つからない。"
            f" archive の中身: {sorted(p.name for p in archive_dir.iterdir())}"
        )
        assert renamed.read_bytes() == CRLF_BODY, (
            "INV-4 違反: 移動先がバイト同一でない（改行変換・encoding 変換を疑う）"
        )
        assert not (reports_dir / name).exists(), "移動元が残っている"

    def test_second_collision_appends_numeric_suffix_and_keeps_all(self, tmp_path):
        """性質 3: リネーム後の名前もさらに衝突した場合に 3 つとも残る。"""
        module = load_archive_module()
        _assert_stamp_helpers_are_self_consistent()

        reports_dir = make_reports_dir(tmp_path)
        archive_dir = reports_dir / "archive"
        name = "plan-report-20260808-101112.md"
        renamed_name = f"plan-report-20260808-101112-archived-{STAMP_A}.md"

        first_bytes = b"FIRST\n"
        second_bytes = b"SECOND\n"
        write_report(archive_dir, name, first_bytes)
        write_report(archive_dir, renamed_name, second_bytes)
        write_report(reports_dir, name, CRLF_BODY, mtime=FIXED_MTIME_A)

        rc = run_main(module, "--reports-dir", str(reports_dir))
        assert rc == 0

        third = archive_dir / f"plan-report-20260808-101112-archived-{STAMP_A}-2.md"
        assert third.is_file(), (
            "同秒衝突の退避名（-2）が見つからない。"
            f" archive の中身: {sorted(p.name for p in archive_dir.iterdir())}"
        )
        assert third.read_bytes() == CRLF_BODY
        assert (archive_dir / name).read_bytes() == first_bytes, "INV-1 違反"
        assert (archive_dir / renamed_name).read_bytes() == second_bytes, "INV-1 違反"
        assert not (reports_dir / name).exists()

    def test_mtime_is_preserved_on_the_renamed_copy(self, tmp_path):
        """性質 7（衝突ケース）: 別名で退避された側の mtime が移動元の mtime と一致する。"""
        module = load_archive_module()
        _assert_stamp_helpers_are_self_consistent()

        reports_dir = make_reports_dir(tmp_path)
        archive_dir = reports_dir / "archive"
        name = "plan-report-20260808-101112.md"

        write_report(archive_dir, name, b"OLD VERSION\n")
        write_report(reports_dir, name, CRLF_BODY, mtime=FIXED_MTIME_A)

        rc = run_main(module, "--reports-dir", str(reports_dir))
        assert rc == 0

        renamed = archive_dir / f"plan-report-20260808-101112-archived-{STAMP_A}.md"
        assert renamed.is_file()
        assert renamed.stat().st_mtime == pytest.approx(FIXED_MTIME_A, abs=1e-3), (
            "移動先の mtime が移動時刻に置き換わっている（os.utime による復元漏れ）"
        )


# ---------------------------------------------------------------------------
# 性質 4 / 5 / 6 / 7（通常ケース）
# ---------------------------------------------------------------------------


class TestPlainMove:
    """衝突が無いケースの退行防止（AC-3）。"""

    def test_moves_file_and_keeps_bytes_identical(self, tmp_path):
        """性質 4: 衝突が無ければ `archive/` へ移動し、移動元が消え、バイト同一である。"""
        module = load_archive_module()
        _assert_stamp_helpers_are_self_consistent()

        reports_dir = make_reports_dir(tmp_path)
        archive_dir = reports_dir / "archive"
        archive_dir.mkdir()
        name = "requirements-report-20260808-101112.md"
        write_report(reports_dir, name, CRLF_BODY, mtime=FIXED_MTIME_A)

        rc = run_main(module, "--reports-dir", str(reports_dir))

        assert rc == 0
        assert not (reports_dir / name).exists(), "移動元が消えていない"
        assert (archive_dir / name).read_bytes() == CRLF_BODY, (
            "INV-4 違反: 移動先がバイト同一でない"
        )

    def test_mtime_is_preserved_on_plain_move(self, tmp_path):
        """性質 7（通常ケース）: 移動先の mtime が移動元の mtime と一致する。"""
        module = load_archive_module()

        reports_dir = make_reports_dir(tmp_path)
        name = "architecture-report-20260807-090501.md"
        write_report(reports_dir, name, LF_BODY, mtime=FIXED_MTIME_B)

        rc = run_main(module, "--reports-dir", str(reports_dir))
        assert rc == 0

        moved = reports_dir / "archive" / name
        assert moved.is_file()
        assert moved.stat().st_mtime == pytest.approx(FIXED_MTIME_B, abs=1e-3), (
            "移動先の mtime が移動時刻に置き換わっている（os.utime による復元漏れ）"
        )

    def test_archive_stays_flat(self, tmp_path):
        """性質 5: 実行後も `archive/` 直下がフラットである（INV-2）。

        `recall_index.py` の report ソースは `archive/*.md` の**非再帰** glob のため、
        サブディレクトリ化すると既存 archive が `c3 recall` から外れる。
        """
        module = load_archive_module()
        _assert_stamp_helpers_are_self_consistent()

        reports_dir = make_reports_dir(tmp_path)
        archive_dir = reports_dir / "archive"
        collided = "plan-report-20260808-101112.md"
        write_report(archive_dir, collided, b"OLD VERSION\n")
        write_report(reports_dir, collided, CRLF_BODY, mtime=FIXED_MTIME_A)
        write_report(reports_dir, "code-review-report-20260807-090501.md", LF_BODY,
                     mtime=FIXED_MTIME_B)

        rc = run_main(module, "--reports-dir", str(reports_dir))
        assert rc == 0

        subdirs = sorted(p.name for p in archive_dir.iterdir() if p.is_dir())
        assert subdirs == [], f"INV-2 違反: archive 直下にディレクトリができた: {subdirs}"

    def test_creates_archive_dir_when_missing(self, tmp_path):
        """性質 6: `archive/` が存在しない状態から実行しても成功する。

        `_excludes.py` の `reports/*` により `archive/` は配布されないため、
        `c3 init` 直後の利用先には `archive/` が存在しない。
        """
        module = load_archive_module()

        reports_dir = make_reports_dir(tmp_path)
        archive_dir = reports_dir / "archive"
        assert not archive_dir.exists(), "前提: archive はまだ存在しない"
        name = "plan-report-20260807-090501.md"
        write_report(reports_dir, name, LF_BODY, mtime=FIXED_MTIME_B)

        rc = run_main(module, "--reports-dir", str(reports_dir))

        assert rc == 0
        assert archive_dir.is_dir(), "archive/ が作られていない"
        assert (archive_dir / name).read_bytes() == LF_BODY
        assert not (reports_dir / name).exists()


# ---------------------------------------------------------------------------
# 性質 8 / 9: 途中失敗
# ---------------------------------------------------------------------------


class TestPartialFailure:
    """ADR-3「失敗時は移動元を残す」／ ADR-4「1 件ごとの確認」／ §3-3 の失敗一覧書式。

    失敗注入は設計 §3-3b の `_move_one` を monkeypatch して `OSError` を送出する形で行う。
    `chmod 0o500` は Windows で効かず空の緑か skip になり、「同名ディレクトリを置く」は
    実装からは衝突扱いに見えるため、いずれも性質 8 を測れない。
    """

    FAILING = "plan-report-20260808-101112.md"
    SURVIVING = "architecture-report-20260807-090501.md"

    def _setup(self, tmp_path: Path) -> Path:
        reports_dir = make_reports_dir(tmp_path)
        write_report(reports_dir, self.FAILING, CRLF_BODY, mtime=FIXED_MTIME_A)
        write_report(reports_dir, self.SURVIVING, LF_BODY, mtime=FIXED_MTIME_B)
        return reports_dir

    def _inject(self, module, monkeypatch):
        original = module._move_one

        def fake_move_one(src, archive_dir):
            if Path(src).name == self.FAILING:
                raise OSError("injected failure for test")
            return original(src, archive_dir)

        monkeypatch.setattr(module, "_move_one", fake_move_one)

    def test_remaining_targets_are_still_processed_and_exit_code_is_one(
        self, tmp_path, monkeypatch
    ):
        """性質 8: 1 件失敗しても残りは処理され、exit 1 になる。"""
        module = load_archive_module()
        reports_dir = self._setup(tmp_path)
        self._inject(module, monkeypatch)

        rc = run_main(module, "--reports-dir", str(reports_dir))

        assert rc == 1, "1 件でも失敗したら exit 1（設計 §3-3）"
        moved = reports_dir / "archive" / self.SURVIVING
        assert moved.is_file(), "失敗 1 件で残りの処理が打ち切られている（ロールバックはしない）"
        assert moved.read_bytes() == LF_BODY

    def test_failure_list_is_written_to_stderr_in_the_contracted_format(
        self, tmp_path, monkeypatch
    ):
        """性質 8: stderr に `FAILED\\t{basename}\\t{理由}` が 1 件 1 行で出る（設計 §3-3）。"""
        module = load_archive_module()
        reports_dir = self._setup(tmp_path)
        self._inject(module, monkeypatch)

        # モジュールロード後に差し替える（ロード時の reconfigure を壊さないため）。
        err_buffer = io.StringIO()
        monkeypatch.setattr(sys, "stderr", err_buffer)
        rc = run_main(module, "--reports-dir", str(reports_dir))
        captured_err = err_buffer.getvalue()

        assert rc == 1
        failed_lines = [
            line for line in captured_err.splitlines() if line.startswith("FAILED\t")
        ]
        assert len(failed_lines) == 1, (
            "失敗一覧が 1 件 1 行で stderr に出ていない。"
            f" stderr 全文: {captured_err!r}"
        )
        fields = failed_lines[0].split("\t")
        assert len(fields) >= 3, f"書式は FAILED\\t{{basename}}\\t{{理由}}: {failed_lines[0]!r}"
        assert fields[1] == self.FAILING, "2 列目は移動元の basename"

    def test_failed_source_is_not_deleted(self, tmp_path, monkeypatch):
        """性質 9: 失敗した 1 件については移動元が消えていない（残す方に倒す）。"""
        module = load_archive_module()
        reports_dir = self._setup(tmp_path)
        self._inject(module, monkeypatch)

        rc = run_main(module, "--reports-dir", str(reports_dir))

        assert rc == 1
        src = reports_dir / self.FAILING
        assert src.is_file(), "失敗したのに移動元が削除された（データ消失）"
        assert src.read_bytes() == CRLF_BODY, "移動元が改変された"


# ---------------------------------------------------------------------------
# 性質 10: --phase の絞り込み
# ---------------------------------------------------------------------------


class TestPhaseFilter:
    """設計 §3-2 のフェーズ名 → 接頭辞対応。"""

    ALL_REPORTS = {
        "requirements": "requirements-report-20260808-101112.md",
        "architecture": "architecture-report-20260808-101112.md",
        "plan": "plan-report-20260808-101112.md",
        "code-review": "code-review-report-20260808-101112.md",
        "security-review": "security-review-report-20260808-101112.md",
        "design-review": "design-review-report-20260808-101112.md",
    }

    def _setup(self, tmp_path: Path) -> Path:
        reports_dir = make_reports_dir(tmp_path)
        for name in self.ALL_REPORTS.values():
            write_report(reports_dir, name, LF_BODY, mtime=FIXED_MTIME_B)
        return reports_dir

    @pytest.mark.parametrize(
        ("phase", "expected_moved"),
        [
            ("requirements", {"requirements"}),
            ("architecture", {"architecture"}),
            ("plan", {"plan"}),
            ("review", {"code-review", "security-review", "design-review"}),
        ],
    )
    def test_phase_moves_only_the_matching_prefixes(self, tmp_path, phase, expected_moved):
        """性質 10: `--phase` の絞り込みが効く。"""
        module = load_archive_module()
        reports_dir = self._setup(tmp_path)

        rc = run_main(module, "--reports-dir", str(reports_dir), "--phase", phase)
        assert rc == 0

        archive_dir = reports_dir / "archive"
        for key, name in self.ALL_REPORTS.items():
            if key in expected_moved:
                assert (archive_dir / name).is_file(), f"{phase}: {name} が移動されていない"
                assert not (reports_dir / name).exists(), f"{phase}: {name} の移動元が残っている"
            else:
                assert (reports_dir / name).is_file(), f"{phase}: 対象外の {name} が移動された"
                assert not (archive_dir / name).exists(), f"{phase}: 対象外の {name} が archive にある"

    def test_repeated_phase_flags_are_combined(self, tmp_path):
        """性質 10: `--phase` は繰り返し指定できる（設計 §3-1）。"""
        module = load_archive_module()
        reports_dir = self._setup(tmp_path)

        rc = run_main(
            module,
            "--reports-dir", str(reports_dir),
            "--phase", "requirements",
            "--phase", "plan",
        )
        assert rc == 0

        archive_dir = reports_dir / "archive"
        assert (archive_dir / self.ALL_REPORTS["requirements"]).is_file()
        assert (archive_dir / self.ALL_REPORTS["plan"]).is_file()
        assert (reports_dir / self.ALL_REPORTS["architecture"]).is_file()

    def test_unknown_phase_moves_nothing_and_exits_one(self, tmp_path):
        """性質 10: 未知値では 1 件も移動せず exit 1（部分適用を作らない・設計 §3-2）。

        設計 §3-3b により exit code は `main` の**戻り値**で表す（例外で表現しない）。
        `argparse` の `choices=` で `SystemExit(2)` になる実装は契約違反である。
        """
        module = load_archive_module()
        reports_dir = self._setup(tmp_path)

        rc = run_main(module, "--reports-dir", str(reports_dir), "--phase", "bogus-phase")

        assert rc == 1, "未知の --phase は exit 1"
        archive_dir = reports_dir / "archive"
        for name in self.ALL_REPORTS.values():
            assert (reports_dir / name).is_file(), f"未知 phase なのに {name} が移動された"
        moved = sorted(p.name for p in archive_dir.glob("*.md")) if archive_dir.exists() else []
        assert moved == [], f"未知 phase なのに archive に何か入った: {moved}"


# ---------------------------------------------------------------------------
# 性質 11: --reports-dir の検証
# ---------------------------------------------------------------------------


class TestReportsDirValidation:
    """設計 §3-3c: 存在しない / ディレクトリでない場合は exit 1（作成しない）。"""

    def test_missing_reports_dir_exits_one_and_creates_nothing(self, tmp_path):
        """性質 11: 存在しないディレクトリを渡すと exit 1（勝手に作らない）。"""
        module = load_archive_module()
        missing = tmp_path / "no-such-dir"

        rc = run_main(module, "--reports-dir", str(missing))

        assert rc == 1, "存在しない --reports-dir は exit 1（設計 §3-3c）"
        assert not missing.exists(), "作るのは archive/ だけ。--reports-dir 自体は作らない"

    def test_reports_dir_pointing_at_a_file_exits_one(self, tmp_path):
        """性質 11: ディレクトリでないパスを渡すと exit 1。"""
        module = load_archive_module()
        not_a_dir = tmp_path / "reports.md"
        not_a_dir.write_bytes(LF_BODY)

        rc = run_main(module, "--reports-dir", str(not_a_dir))

        assert rc == 1, "ディレクトリでない --reports-dir は exit 1（設計 §3-3c）"
        assert not_a_dir.read_bytes() == LF_BODY, "渡されたファイルが改変された"


# ---------------------------------------------------------------------------
# 性質 12: シンボリックリンクを辿らない
# ---------------------------------------------------------------------------


class TestSymlinkEntriesAreSkipped:
    """設計 §3-3c: 移動対象の列挙時に `is_symlink()` のエントリは処理しない。

    Windows のシンボリックリンク作成には特権が要り、本開発機では
    `OSError: [WinError 1314]` になる（実測）。そのため
    (a) `Path.is_symlink` を差し替えて**契約そのもの**を特権なしで測るテストと、
    (b) 実シンボリックリンクを作れる環境（POSIX の CI 等）でのみ走る実挙動テストの
    2 本を置く。(a) が主判定である。
    """

    LINK_NAME = "plan-report-20260808-101112.md"
    PLAIN_NAME = "architecture-report-20260807-090501.md"

    def test_entry_reported_as_symlink_is_not_moved(self, tmp_path, monkeypatch):
        """性質 12（主判定・特権不要）: `is_symlink()` が True のエントリは移動されない。

        設計 §3-3c が判定手段として `is_symlink()` を名指ししているため、
        `pathlib.Path.is_symlink` を差し替えて「リンクとして見えるエントリ」を作る。
        """
        module = load_archive_module()

        reports_dir = make_reports_dir(tmp_path)
        write_report(reports_dir, self.LINK_NAME, CRLF_BODY, mtime=FIXED_MTIME_A)
        write_report(reports_dir, self.PLAIN_NAME, LF_BODY, mtime=FIXED_MTIME_B)

        original_is_symlink = Path.is_symlink
        link_target = (reports_dir / self.LINK_NAME).resolve()

        def fake_is_symlink(self) -> bool:
            try:
                same = self.resolve() == link_target
            except OSError:
                same = False
            if same:
                return True
            return original_is_symlink(self)

        monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

        rc = run_main(module, "--reports-dir", str(reports_dir))

        assert rc == 0, "リンクのスキップは失敗ではない"
        assert (reports_dir / self.LINK_NAME).is_file(), (
            "symlink 扱いのエントリが移動された（設計 §3-3c 違反）"
        )
        archive_dir = reports_dir / "archive"
        assert not (archive_dir / self.LINK_NAME).exists()
        assert (archive_dir / self.PLAIN_NAME).is_file(), "通常ファイルまで巻き添えで残った"

    def test_real_symlink_entry_is_not_moved(self, tmp_path):
        """性質 12（実挙動・特権が要る環境では skip）: 実シンボリックリンクを移動しない。"""
        module = load_archive_module()

        reports_dir = make_reports_dir(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        real_target = write_report(outside, "real-target.md", CRLF_BODY, mtime=FIXED_MTIME_A)
        write_report(reports_dir, self.PLAIN_NAME, LF_BODY, mtime=FIXED_MTIME_B)

        link = reports_dir / self.LINK_NAME
        try:
            link.symlink_to(real_target)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"この環境ではシンボリックリンクを作成できない: {exc}")

        rc = run_main(module, "--reports-dir", str(reports_dir))

        assert rc == 0
        assert link.is_symlink(), "symlink が移動・削除された（設計 §3-3c 違反）"
        assert real_target.read_bytes() == CRLF_BODY, "リンク先の実体が改変された"
        assert (reports_dir / "archive" / self.PLAIN_NAME).is_file()
