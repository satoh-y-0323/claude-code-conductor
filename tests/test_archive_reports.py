"""Red フェーズ: `.claude/skills/start/scripts/archive_reports.py` の実挙動テスト。

上流契約（SSOT）:
- `.claude/reports/architecture-report-20260809-154124.md`（改訂 2 → **改訂 4**）
  §0 INV-1〜INV-4 / ADR-2 / ADR-3 / §3-1〜§3-3c
- `.claude/reports/plan-report-20260809-164111.md` の `test-archive` タスク（性質 1〜12）
- `.claude/reports/plan-report-20260809-181356.md` の `test-findings` タスク（改訂 4 の性質 1〜6）

本ファイルの全テストは **必ず `--reports-dir` に `tmp_path` を渡す**。既定値経路
（実リポジトリの `.claude/reports`）は検査対象外である。既定値を使うテストを 1 本でも
書くと、フルスイート実行のたびに実リポジトリのレポートが `archive/` へ移動する。

起動方式は設計 §3-3b のとおり in-process import（`importlib.util.spec_from_file_location`）で
`main(argv) -> int` を呼び、**戻り値を exit code として扱う**。subprocess は使わない
（失敗注入と exit code 検査を同じ経路で測るため）。

検査対象スクリプトのパスは環境変数 `C3_ARCHIVE_SCRIPT_PATH` で差し替えられる
（confirm フェーズがスクリプトの写しにスタブを当てて走らせるため）。未設定時は実体パスを使う。

Red の理由（初版・性質 1〜12）: `archive_reports.py` が未実装のため `load_archive_module()` が
`FileNotFoundError` を送出して全件 failed になった。これは機能未実装による正しい失敗である。
※ 初版の 21 件は実装済みで現在は緑。以下は**改訂 4 の追加分**についての Red の理由である。

Red の理由（改訂 4・E レビュー指摘 9 件のうち振る舞いが変わる 6 件）:
`archive_reports.py` が改訂 4 の規定を未実装のため、以下がいずれも「現行の（是正前の）挙動」で失敗する。

- SR-V-002: 移動先 `archive/` が symlink でも検査せずに移動してしまう（設計 §3-3c 改訂 4 が未実装）
- SR-AI-001: env ゲート `C3_ARCHIVE_REPORTS_DIR_OK` が存在せず `--reports-dir` が無条件に通る
- CR-E-005: 「コピー成功・移動元の削除だけ失敗」が `FAILED` に丸められ `SOURCE_KEPT` 分類が無い
- SR-V-001: `_candidate_names` に上限が無く、候補を埋め尽くしても失敗にならない
- SR-NEW-1: `_one_line` が `\\r` / `\\n` / `\\t` しか潰さず、他の C0 制御文字と DEL が生で出る
  （かつ `main()` の他の `print` は helper を通っていない）
- SR-NEW-2: `_move_one` が `src` を読む直前に `is_symlink()` を再判定しない

Red の理由（改訂 5・E 再レビューの High 1 件 = CR-NEW / SR-V-002 再発）:
移動先の判定が `is_symlink()` のままであり、**NTFS ディレクトリジャンクションが素通りする**。
ジャンクションは `_winapi.CreateJunction` / `mklink /J` で**管理者権限なしに作成でき**、
`Path.is_symlink()` は `False`・`is_dir()` / `exists()` は `True` を返す（本開発機で実測）。
そのため是正前の実装は **exit 0 の成功扱い**で完走し、ファイルは外部ディレクトリへ移り、
移動元は削除される。設計 §3-3c 改訂 5 は判定を **realpath 封じ込め**
（`os.path.realpath(archive_dir)` が `os.path.realpath(reports_dir)` 直下の `archive` と
一致すること・不一致なら reparse の種別を問わず 1 件も処理せず exit 1）へ置き換えると規定する。

**env ゲート（SR-AI-001）と既存 21 件の関係**: 改訂 4 で `--reports-dir` は
`C3_ARCHIVE_REPORTS_DIR_OK=1` がある場合のみ受け付ける形になる。本ファイルの全テストは
`--reports-dir` を渡すため、モジュール全体に autouse fixture で env を明示 opt-in しておく
（設計 §3-3c 改訂 4 の「テスト・検証は env を明示設定して呼ぶ」）。
ゲートそのものを測るテストだけが `monkeypatch.delenv` で外す。
"""

from __future__ import annotations

import importlib.util
import io
import itertools
import os
import re
import sys
from datetime import datetime
from pathlib import Path, PurePath

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
# 改訂 4・SR-AI-001: `--reports-dir` の env ゲート
#
# 本ファイルの全テストは `--reports-dir` に一時ディレクトリを渡す（既定値経路は実リポジトリの
# `.claude/reports` であり、検査対象外）。改訂 4 でその引数が env ゲート下に入るため、
# モジュール全体で明示 opt-in しておく。**ゲート自体を測るテストだけが delenv で外す**。
# ---------------------------------------------------------------------------

REPORTS_DIR_OK_ENV = "C3_ARCHIVE_REPORTS_DIR_OK"


@pytest.fixture(autouse=True)
def _opt_in_to_reports_dir_override(monkeypatch):
    """設計 §3-3c 改訂 4: テスト・検証は env を明示設定して `--reports-dir` を使う。"""
    monkeypatch.setenv(REPORTS_DIR_OK_ENV, "1")


def run_capturing(module, monkeypatch, *argv: str) -> tuple[int, str, str]:
    """`main(argv)` を呼び、(exit code, stdout, stderr) を返す。

    モジュールロード後に `sys.stdout` / `sys.stderr` を差し替える（ロード時の reconfigure を
    壊さないため）。`print` は呼び出し時に `sys.stdout` を引くため、この差し替えで捕捉できる。
    """
    out_buffer = io.StringIO()
    err_buffer = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out_buffer)
    monkeypatch.setattr(sys, "stderr", err_buffer)
    rc = module.main(list(argv))
    return rc, out_buffer.getvalue(), err_buffer.getvalue()


def lines_with_token(text: str, token: str) -> list[str]:
    """行頭トークン `{token}\\t` で始まる行を返す（設計 §3-3 の失敗一覧書式）。"""
    return [line for line in text.splitlines() if line.startswith(f"{token}\t")]


# 改訂 4・SR-NEW-1: 出力に出てはいけない文字。
# `\n` / `\r` は行区切り、`\t` は `FAILED` / `SOURCE_KEPT` 行のフィールド区切りとして
# 契約が使うため除外する。それ以外の C0 制御文字（ESC・BEL 等）と DEL が対象。
_FORBIDDEN_OUTPUT_CHARS = frozenset(
    chr(code) for code in range(0x20) if chr(code) not in "\n\r\t"
) | {"\x7f"}


def assert_no_raw_control_chars(text: str, label: str) -> None:
    """`text` に生の C0 制御文字 / DEL が含まれないことを確認する（設計 §3-3 改訂 4）。"""
    found = sorted({char for char in text if char in _FORBIDDEN_OUTPUT_CHARS})
    assert found == [], (
        f"{label} に生の制御文字が出力された: {[hex(ord(c)) for c in found]}\n"
        "設計 §3-3【改訂 4・SR-NEW-1】は stdout / stderr に出す全てのファイル名・パス・"
        "失敗理由を同一 helper に通し、C0 制御文字と DEL を除去・置換することを求めている。\n"
        f"全文: {text!r}"
    )


def write_report_or_skip(directory: Path, name: str, data: bytes, mtime: float | None = None) -> Path:
    """制御文字入りの名前でファイルを作る。作れない FS では skip する。

    実測: Windows (NTFS) は `\\x01`-`\\x1f` を含むファイル名を `OSError: [Errno 22]` で拒否するが、
    DEL (`\\x7f`) は受け付ける。ESC を含む実ファイルはこの環境では作れないため、
    ESC は「失敗理由の文字列」経由でも測る（下の TestControlCharSanitisation を参照）。
    """
    try:
        return write_report(directory, name, data, mtime)
    except (OSError, ValueError) as exc:
        pytest.skip(f"この環境ではファイル名に制御文字を含められない: {exc!r}")


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


# ===========================================================================
# 以下は改訂 4（E レビュー差し戻し 9 件のうち振る舞いが変わる 6 件）の追加分。
# ===========================================================================


# ---------------------------------------------------------------------------
# 改訂 4 性質 1 / SR-V-002: 移動先ディレクトリが symlink なら 1 件も処理しない
# ---------------------------------------------------------------------------


class TestArchiveDirSymlinkIsRefused:
    """設計 §3-3c【改訂 4・SR-V-002】。

    `Path.mkdir(exist_ok=True)` はリンク先が実在ディレクトリなら素通りし、`os.open` の
    `O_EXCL` は最終コンポーネントにしか効かない。よって `{reports-dir}/archive` を任意
    ディレクトリへのリンクにされると、**移動先をすり替えたうえで移動元が削除される**。
    → `archive_dir` を使う前に `is_symlink()` を検査し、真なら 1 件も処理せず exit 1。

    Windows のシンボリックリンク作成には特権が要る（実測 `OSError: [WinError 1314]`）ため、
    改訂 4 では `Path.is_symlink` を差し替える主判定と実挙動テストの二段構成にしていた。

    **【改訂 5 で主判定を削除】** `Path.is_symlink` を差し替える主判定
    （`test_archive_dir_reported_as_symlink_stops_everything`）は、置き換えられた機構
    （`is_symlink()` 判定）そのものを測るためだけの seam であり、設計 §3-3c 改訂 5 の
    realpath 封じ込めでは何も保証しないため削除した。真のシンボリックリンクに対する拒否は、
    下の実挙動テスト（realpath 封じ込めでもリンク先へ解決されるため引き続き緑）と、
    新設の `TestArchiveDirJunctionIsRefused` / `TestArchiveDirMustResolveInsideReportsDir`
    がカバーする。
    """

    FIRST = "architecture-report-20260807-090501.md"
    SECOND = "plan-report-20260808-101112.md"

    def _setup(self, tmp_path: Path) -> Path:
        reports_dir = make_reports_dir(tmp_path)
        write_report(reports_dir, self.FIRST, LF_BODY, mtime=FIXED_MTIME_B)
        write_report(reports_dir, self.SECOND, CRLF_BODY, mtime=FIXED_MTIME_A)
        return reports_dir

    def test_real_symlinked_archive_dir_stops_everything(self, tmp_path):
        """性質 1（実挙動・特権が要る環境では skip）: 実 symlink の移動先でも同じ。"""
        module = load_archive_module()

        reports_dir = self._setup(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()

        archive_dir = reports_dir / "archive"
        try:
            archive_dir.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"この環境ではシンボリックリンクを作成できない: {exc}")

        rc = run_main(module, "--reports-dir", str(reports_dir))

        assert rc == 1, "移動先が symlink なら exit 1（設計 §3-3c 改訂 4）"
        leaked = sorted(p.name for p in outside.iterdir())
        assert leaked == [], f"リンク先に書き込みが漏れた: {leaked}"
        assert (reports_dir / self.FIRST).read_bytes() == LF_BODY, "移動元が削除・改変された"
        assert (reports_dir / self.SECOND).read_bytes() == CRLF_BODY, "移動元が削除・改変された"


# ---------------------------------------------------------------------------
# 改訂 4 性質 2 / SR-AI-001: `--reports-dir` の env ゲート
# ---------------------------------------------------------------------------


class TestReportsDirEnvGate:
    """設計 §3-3c【改訂 4・SR-AI-001】。

    `--reports-dir` は「テスト・検証専用」でありながら、任意ディレクトリへの破壊操作
    （移動元の削除）を開く唯一の入口になっている。`permissions.allow` の前方一致では
    塞ぎきれないため、env `C3_ARCHIVE_REPORTS_DIR_OK=1` を実効的な関所とする。
    """

    NAME = "plan-report-20260808-101112.md"

    def _setup(self, tmp_path: Path) -> Path:
        reports_dir = make_reports_dir(tmp_path)
        write_report(reports_dir, self.NAME, CRLF_BODY, mtime=FIXED_MTIME_A)
        return reports_dir

    def test_reports_dir_without_env_gate_processes_nothing(self, tmp_path, monkeypatch):
        """性質 2: env が無い状態の `--reports-dir` は 1 件も処理せず exit 1。"""
        module = load_archive_module()
        reports_dir = self._setup(tmp_path)
        monkeypatch.delenv(REPORTS_DIR_OK_ENV, raising=False)

        rc = run_main(module, "--reports-dir", str(reports_dir))

        assert rc == 1, (
            f"env {REPORTS_DIR_OK_ENV} が無いのに --reports-dir が受け付けられた"
            "（設計 §3-3c 改訂 4）"
        )
        assert (reports_dir / self.NAME).read_bytes() == CRLF_BODY, "移動元が処理された"
        archive_dir = reports_dir / "archive"
        moved = sorted(p.name for p in archive_dir.glob("*")) if archive_dir.exists() else []
        assert moved == [], f"ゲートを抜けて archive に書き込みが起きた: {moved}"

    def test_env_gate_is_what_makes_the_difference(self, tmp_path, monkeypatch):
        """性質 2: 同じ引数で env の有無だけを変えると結果が変わる（拒否 → 従来どおり）。

        「env 無しで拒否」と「env 有りで従来どおり動く」を 1 本にまとめている。分けると
        後者は現行実装でも自明に緑で、**是正前の挙動をテストしているだけ**になるため。
        """
        module = load_archive_module()
        reports_dir = self._setup(tmp_path)
        archive_dir = reports_dir / "archive"

        monkeypatch.delenv(REPORTS_DIR_OK_ENV, raising=False)
        rc_without = run_main(module, "--reports-dir", str(reports_dir))
        moved_without = (archive_dir / self.NAME).exists()

        monkeypatch.setenv(REPORTS_DIR_OK_ENV, "1")
        rc_with = run_main(module, "--reports-dir", str(reports_dir))

        assert (rc_without, moved_without) == (1, False), (
            f"env 無し: exit 1 かつ未処理であるべき（実際 rc={rc_without} moved={moved_without}）"
        )
        assert rc_with == 0, f"env 有り: 従来どおり成功すべき（実際 rc={rc_with}）"
        assert (archive_dir / self.NAME).read_bytes() == CRLF_BODY, "env 有りで移動していない"
        assert not (reports_dir / self.NAME).exists(), "env 有りで移動元が消えていない"


# ---------------------------------------------------------------------------
# 改訂 4 性質 3 / CR-E-005: コピー成功・移動元の削除だけ失敗 → SOURCE_KEPT
# ---------------------------------------------------------------------------


class TestSourceKeptWhenOnlyUnlinkFails:
    """設計 §3-3【改訂 4・CR-E-005】。

    初版は `src.unlink()` を排他生成〜書き込みの `try` の外側に置いたため、この経路が
    「移動 0 件 / 失敗 1 件」として報告され、**実態（移動先には正しい内容が既に存在する）**を
    表せていなかった。→ `FAILED` とは別の行頭トークン
    `SOURCE_KEPT\\t{移動元 basename}\\t{移動先 basename}\\t{理由}` を stderr に出し、
    集計にも独立項目を出す。exit code は 1。**移動先は削除しない**。

    失敗注入は `Path.unlink` を差し替えて行う（`_move_one` を丸ごと差し替えると
    「コピーは成功している」という前提そのものを作れないため）。`archive/` 配下の
    `unlink`（中途生成物の後始末）は素通りさせ、移動元の削除だけを失敗させる。
    """

    NAME = "plan-report-20260808-101112.md"
    RENAMED = f"plan-report-20260808-101112-archived-{STAMP_A}.md"

    # 集計の独立項目。設計の逐語は「コピー済み・移動元が残存: N 件」。
    # 文言の細部に依存しすぎないよう、ラベル語 + 件数の同居で判定する
    # （`/` を跨がせないことで「移動: 1 件 / ... 残存: 0 件」の誤一致を防ぐ）。
    KEPT_COUNT_RE = re.compile(r"(コピー済み|残存|SOURCE_KEPT)[^/\n]*?[:：]\s*1\s*件")

    def _setup(self, tmp_path: Path) -> Path:
        reports_dir = make_reports_dir(tmp_path)
        write_report(reports_dir, self.NAME, CRLF_BODY, mtime=FIXED_MTIME_A)
        return reports_dir

    @staticmethod
    def _is_the_source(path) -> bool:
        """`archive/` の外にある対象ファイル（＝移動元）か。"""
        pure = PurePath(path)
        return pure.name == TestSourceKeptWhenOnlyUnlinkFails.NAME and pure.parent.name != "archive"

    def _break_source_unlink(self, monkeypatch) -> None:
        """移動元の削除だけを失敗させる。

        `Path.unlink` と `os.unlink` の両方を塞ぐ。実装がどちらの API を選んでも
        同じ失敗注入が効くようにするため（`_move_one` の中途生成物の後始末は
        `archive/` 配下なので素通りする）。
        """
        original_path_unlink = Path.unlink
        original_os_unlink = os.unlink

        def fake_path_unlink(self, *args, **kwargs):
            if TestSourceKeptWhenOnlyUnlinkFails._is_the_source(self):
                raise OSError("injected unlink failure for test")
            return original_path_unlink(self, *args, **kwargs)

        def fake_os_unlink(path, *args, **kwargs):
            if TestSourceKeptWhenOnlyUnlinkFails._is_the_source(path):
                raise OSError("injected unlink failure for test")
            return original_os_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fake_path_unlink)
        monkeypatch.setattr(os, "unlink", fake_os_unlink)

    def test_copy_is_classified_as_source_kept_and_the_destination_survives(
        self, tmp_path, monkeypatch
    ):
        """性質 3: `SOURCE_KEPT` 分類・移動先はバイト同一で削除されない・移動元も残る・exit 1。

        「移動先が削除されないこと」を独立したテストにしない理由: 是正**前**の実装でも
        （`src.unlink()` が排他生成〜書き込みの `try` の外にあるため）その 1 点だけは
        たまたま成立しており、単独では is-red にならない＝既存の挙動を測るだけになる。
        分類（`SOURCE_KEPT`）と同じテストに畳み込むことで、是正で移動先を捨てる実装に
        なった場合もここで赤になる。
        """
        module = load_archive_module()
        reports_dir = self._setup(tmp_path)
        self._break_source_unlink(monkeypatch)

        rc, _out, err = run_capturing(module, monkeypatch, "--reports-dir", str(reports_dir))

        assert rc == 1, "コピー済み・移動元残存は要対応（exit 1・設計 §3-3 改訂 4）"

        dst = reports_dir / "archive" / self.NAME
        assert dst.is_file(), (
            "コピーに成功した移動先が削除された。"
            "設計 §3-3 改訂 4 は「移動先は削除しない（バイト同一のコピーを捨てない）」と規定している"
        )
        assert dst.read_bytes() == CRLF_BODY, "INV-4 違反: 移動先がバイト同一でない"
        assert (reports_dir / self.NAME).read_bytes() == CRLF_BODY, "移動元が消えた/改変された"

        kept = lines_with_token(err, "SOURCE_KEPT")
        assert len(kept) == 1, (
            "`SOURCE_KEPT\\t{移動元}\\t{移動先}\\t{理由}` が 1 件 1 行で stderr に出ていない。\n"
            f"stderr 全文: {err!r}"
        )
        assert lines_with_token(err, "FAILED") == [], (
            "コピー成功・削除失敗が `FAILED` に丸められている（CR-E-005 の是正対象）。\n"
            f"stderr 全文: {err!r}"
        )
        fields = kept[0].split("\t")
        assert len(fields) >= 4, f"書式は SOURCE_KEPT\\t移動元\\t移動先\\t理由: {kept[0]!r}"
        assert fields[1] == self.NAME, "2 列目は移動元の basename"
        assert fields[2] == self.NAME, "3 列目は移動先の basename（衝突が無いので同名）"

    def test_stdout_summary_has_an_independent_kept_count(self, tmp_path, monkeypatch):
        """性質 3: 集計に「コピー済み・移動元が残存」の件数が独立項目として出る。"""
        module = load_archive_module()
        reports_dir = self._setup(tmp_path)
        self._break_source_unlink(monkeypatch)

        rc, out, _err = run_capturing(module, monkeypatch, "--reports-dir", str(reports_dir))

        assert rc == 1
        assert self.KEPT_COUNT_RE.search(out), (
            "stdout の集計に「コピー済み・移動元が残存: 1 件」に相当する独立項目が無い"
            "（設計 §3-3 改訂 4）。\n"
            f"stdout 全文: {out!r}"
        )

    def test_rerun_duplication_is_either_avoided_or_reported(self, tmp_path, monkeypatch):
        """性質 3: 同じ引数で再実行したときの重複が、避けられるか報告から読み取れる。

        設計 §3-3 改訂 4 は重複排除を規定せず「移動先は削除しない」「`SOURCE_KEPT` で
        運用者が手で削除できる状態にする」と規定している。よって契約は
        **「重複が増えないこと」または「増えた実体が報告（`SOURCE_KEPT` の移動先列）から
        辿れること」**である。どちらの実装でも通るが、`SOURCE_KEPT` が出ないか、
        増えた実体が報告から辿れないなら赤になる。
        """
        module = load_archive_module()
        reports_dir = self._setup(tmp_path)
        self._break_source_unlink(monkeypatch)
        archive_dir = reports_dir / "archive"

        rc_first, _out1, err1 = run_capturing(
            module, monkeypatch, "--reports-dir", str(reports_dir)
        )
        assert rc_first == 1
        assert lines_with_token(err1, "SOURCE_KEPT"), f"1 回目に SOURCE_KEPT が無い: {err1!r}"
        after_first = sorted(p.name for p in archive_dir.glob("*.md"))

        rc_second, _out2, err2 = run_capturing(
            module, monkeypatch, "--reports-dir", str(reports_dir)
        )
        assert rc_second == 1
        kept2 = lines_with_token(err2, "SOURCE_KEPT")
        assert len(kept2) == 1, f"2 回目に SOURCE_KEPT が 1 行出ていない: {err2!r}"
        after_second = sorted(p.name for p in archive_dir.glob("*.md"))

        assert (reports_dir / self.NAME).read_bytes() == CRLF_BODY, "移動元が失われた"

        if after_second == after_first:
            # 重複を増やさない実装。移動先が消えていないことだけ押さえる。
            assert (archive_dir / self.NAME).read_bytes() == CRLF_BODY
            return

        added = sorted(set(after_second) - set(after_first))
        assert len(added) == 1, f"再実行で増えた実体が 1 件でない: {added}"
        reported_dst = kept2[0].split("\t")[2]
        assert reported_dst == added[0], (
            "再実行で増えたコピーが報告（SOURCE_KEPT の移動先列）から辿れない。\n"
            f"報告: {reported_dst!r} / 実際に増えた実体: {added}"
        )
        assert (archive_dir / added[0]).read_bytes() == CRLF_BODY
        assert after_first[0] in after_second, "1 回目のコピーが消えた（INV-1 / 改訂 4）"


# ---------------------------------------------------------------------------
# 改訂 4 性質 4 / SR-V-001: 衝突退避の候補生成に上限
# ---------------------------------------------------------------------------


class TestCandidateNameLimit:
    """設計 §3-3【改訂 4・SR-V-001】: 候補生成の上限は 1000。超過はその 1 件の失敗。

    上限が無いと、退避名を大量に事前配置されたときに 1 件の移動が O(N) の `os.open`
    試行を要する（軽度の DoS）。
    """

    STEM = "plan-report-20260808-101112"
    BLOCKED = f"{STEM}.md"
    SURVIVING = "architecture-report-20260807-090501.md"

    # 上限 1000 の読み方（候補総数か数値接尾辞の最大値か）に依存しないよう、
    # 余裕を持って `-1005` まで塞ぐ。
    LAST_INDEX = 1005

    def _blocker_names(self) -> list[str]:
        base = f"{self.STEM}-archived-{STAMP_A}"
        return (
            [self.BLOCKED, f"{base}.md"]
            + [f"{base}-{index}.md" for index in range(2, self.LAST_INDEX + 1)]
        )

    def test_exhausted_candidates_fail_that_one_and_others_continue(self, tmp_path, monkeypatch):
        """性質 4: 候補を上限まで埋めるとその 1 件が失敗し、他の対象の処理は継続する。"""
        module = load_archive_module()
        _assert_stamp_helpers_are_self_consistent()

        reports_dir = make_reports_dir(tmp_path)
        archive_dir = reports_dir / "archive"
        archive_dir.mkdir()
        blockers = self._blocker_names()
        for blocker in blockers:
            (archive_dir / blocker).write_bytes(b"")

        write_report(reports_dir, self.BLOCKED, CRLF_BODY, mtime=FIXED_MTIME_A)
        write_report(reports_dir, self.SURVIVING, LF_BODY, mtime=FIXED_MTIME_B)

        rc, _out, err = run_capturing(module, monkeypatch, "--reports-dir", str(reports_dir))

        assert rc == 1, (
            "候補を上限まで埋めても失敗にならない（設計 §3-3 改訂 4 の上限 1000 が未実装）"
        )
        failed = lines_with_token(err, "FAILED")
        assert [line.split("\t")[1] for line in failed] == [self.BLOCKED], (
            f"上限超過の 1 件が失敗として報告されていない。stderr 全文: {err!r}"
        )
        assert (reports_dir / self.BLOCKED).read_bytes() == CRLF_BODY, (
            "失敗した 1 件の移動元が消えた/改変された"
        )
        assert (archive_dir / self.SURVIVING).read_bytes() == LF_BODY, (
            "1 件失敗で残りの処理が打ち切られている（ADR-4 / 設計 §3-3）"
        )
        assert not (reports_dir / self.SURVIVING).exists()

        produced = sorted(p.name for p in archive_dir.glob("*.md"))
        assert len(produced) == len(blockers) + 1, (
            "上限を超えて候補が生成された（または事前配置が上書きされた）。"
            f" 期待 {len(blockers) + 1} 件 / 実際 {len(produced)} 件"
        )
        assert all((archive_dir / blocker).stat().st_size == 0 for blocker in blockers), (
            "INV-1 違反: 事前配置した既存ファイルが書き換わった"
        )


# ---------------------------------------------------------------------------
# 改訂 4 性質 5 / SR-NEW-1: 出力の制御文字を一元的に無害化する
# ---------------------------------------------------------------------------


class TestControlCharSanitisation:
    """設計 §3-3【改訂 4・SR-NEW-1】。

    `--reports-dir` が任意ディレクトリを指せる以上、そこにあるファイル名は攻撃者が制御でき、
    生の ANSI エスケープが端末表示を偽装しうる。**失敗理由の行だけでなく、移動・リネームの
    通常出力（stdout）も対象**である。

    **判定に使う制御文字の選び方（実測に基づく）**: Windows (NTFS) は `\\x01`-`\\x1f` を含む
    ファイル名を `OSError: [Errno 22] Invalid argument` で拒否する一方、DEL (`\\x7f`) は
    受け付ける（本開発機で実測）。よって
    - **主判定はファイル名の DEL**（クロスプラットフォームで実ファイルを作れる）
    - **ESC (`\\x1b`) と BEL (`\\x07`) は失敗理由の文字列経由**で測る（OS 依存なし）
    - ESC を含む実ファイル名の版は、作れない環境では skip する
    の 3 本立てにする。
    """

    DEL_NAME = "plan-report-20260808-101112\x7fx.md"
    ESC_NAME = "plan-report-20260808-101112\x1bx.md"
    ESC_REASON = "boom \x1b[31mRED\x1b[0m \x07bell"

    def test_rename_output_on_stdout_is_sanitised(self, tmp_path, monkeypatch):
        """性質 5: 通常出力（リネームの旧名→新名）に生の制御文字が出ない。"""
        module = load_archive_module()
        _assert_stamp_helpers_are_self_consistent()

        reports_dir = make_reports_dir(tmp_path)
        archive_dir = reports_dir / "archive"
        # 衝突させてリネーム経路（stdout に旧名→新名を出す）を通す。
        write_report_or_skip(archive_dir, self.DEL_NAME, b"OLD VERSION\n")
        write_report_or_skip(reports_dir, self.DEL_NAME, CRLF_BODY, mtime=FIXED_MTIME_A)

        rc, out, err = run_capturing(module, monkeypatch, "--reports-dir", str(reports_dir))

        assert rc == 0, f"衝突リネームは失敗ではない。stderr: {err!r}"
        assert "リネーム" in out or "->" in out, (
            f"リネームの通常出力が stdout に出ていない（前提が崩れている）: {out!r}"
        )
        assert_no_raw_control_chars(out, "stdout（リネームの通常出力）")

    def test_failure_line_name_and_reason_are_sanitised(self, tmp_path, monkeypatch):
        """性質 5: 失敗行のファイル名（DEL）と理由（ESC / BEL）が無害化される。"""
        module = load_archive_module()

        reports_dir = make_reports_dir(tmp_path)
        write_report_or_skip(reports_dir, self.DEL_NAME, CRLF_BODY, mtime=FIXED_MTIME_A)

        def fake_move_one(src, archive_dir):
            raise OSError(TestControlCharSanitisation.ESC_REASON)

        monkeypatch.setattr(module, "_move_one", fake_move_one)

        rc, out, err = run_capturing(module, monkeypatch, "--reports-dir", str(reports_dir))

        assert rc == 1
        assert lines_with_token(err, "FAILED"), f"失敗一覧が出ていない: {err!r}"
        assert_no_raw_control_chars(err, "stderr（FAILED 行のファイル名と理由）")
        assert_no_raw_control_chars(out, "stdout（集計）")

    def test_escape_char_in_a_real_filename_is_sanitised(self, tmp_path, monkeypatch):
        """性質 5（実挙動・ESC 名を作れない FS では skip）: ESC 入りの実ファイル名。"""
        module = load_archive_module()
        _assert_stamp_helpers_are_self_consistent()

        reports_dir = make_reports_dir(tmp_path)
        archive_dir = reports_dir / "archive"
        write_report_or_skip(archive_dir, self.ESC_NAME, b"OLD VERSION\n")
        write_report_or_skip(reports_dir, self.ESC_NAME, CRLF_BODY, mtime=FIXED_MTIME_A)

        rc, out, err = run_capturing(module, monkeypatch, "--reports-dir", str(reports_dir))

        assert rc == 0, f"衝突リネームは失敗ではない。stderr: {err!r}"
        assert_no_raw_control_chars(out, "stdout（ESC を含む実ファイル名）")
        assert_no_raw_control_chars(err, "stderr（ESC を含む実ファイル名）")


# ---------------------------------------------------------------------------
# 改訂 4 性質 6 / SR-NEW-2: 読み取り直前の symlink 再判定
# ---------------------------------------------------------------------------


class TestSymlinkRecheckJustBeforeRead:
    """設計 §3-3c【改訂 4・SR-NEW-2】。

    `_collect_targets` で symlink を除外した後、`_move_one` は別途 `src.read_bytes()` を
    呼ぶ。この間に `src` が symlink へ差し替えられると、差し替え先の実体が `.md` として
    アーカイブへコピーされうる。→ `_move_one` 内で読む直前に `src.is_symlink()` を再判定し、
    真ならその 1 件を失敗にする。

    差し替えの作り方: 列挙が終わった後にだけ「リンクに見える」状態にしたいので、
    契約シンボル `_move_one`（設計 §3-3b）へ入った時点でフラグを立てる。
    `Path.read_bytes` は差し替え先の内容（SECRET）を返す形にし、**SECRET が archive に
    現れないこと**でリンク先の内容がコピーされないことを測る。
    """

    LINK_NAME = "plan-report-20260808-101112.md"
    PLAIN_NAME = "architecture-report-20260807-090501.md"
    SECRET = b"LINK TARGET SECRET - must never reach archive\n"

    def test_target_that_becomes_a_symlink_before_read_fails_that_one(
        self, tmp_path, monkeypatch
    ):
        """性質 6: 列挙後・読み取り前に symlink 化した 1 件は失敗になり、内容はコピーされない。"""
        module = load_archive_module()

        reports_dir = make_reports_dir(tmp_path)
        write_report(reports_dir, self.LINK_NAME, CRLF_BODY, mtime=FIXED_MTIME_A)
        write_report(reports_dir, self.PLAIN_NAME, LF_BODY, mtime=FIXED_MTIME_B)
        archive_dir = reports_dir / "archive"

        state = {"swapped": False}
        link_name = self.LINK_NAME
        secret = self.SECRET

        def _is_the_swapped_target(path: Path) -> bool:
            return path.name == link_name and path.parent.name != "archive"

        original_move_one = module._move_one

        def move_one_after_swap(src, target_dir):
            # 列挙は既に終わっている。ここから先が「読み取り直前」の窓。
            if _is_the_swapped_target(Path(src)):
                state["swapped"] = True
            return original_move_one(src, target_dir)

        monkeypatch.setattr(module, "_move_one", move_one_after_swap)

        original_is_symlink = Path.is_symlink

        def fake_is_symlink(self) -> bool:
            if state["swapped"] and _is_the_swapped_target(self):
                return True
            return original_is_symlink(self)

        monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

        original_read_bytes = Path.read_bytes

        def fake_read_bytes(self) -> bytes:
            if state["swapped"] and _is_the_swapped_target(self):
                return secret
            return original_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

        rc, _out, err = run_capturing(module, monkeypatch, "--reports-dir", str(reports_dir))

        assert rc == 1, "読み取り直前に symlink 化した 1 件が失敗になっていない（設計 §3-3c 改訂 4）"
        failed = lines_with_token(err, "FAILED")
        assert [line.split("\t")[1] for line in failed] == [self.LINK_NAME], (
            f"失敗として報告された対象が違う。stderr 全文: {err!r}"
        )

        leaked = [
            path.name
            for path in archive_dir.glob("*")
            if path.is_file() and original_read_bytes(path) == secret
        ]
        assert leaked == [], f"リンク先の内容が archive にコピーされた: {leaked}"
        assert not (archive_dir / self.LINK_NAME).exists(), "symlink 化した対象が移動された"
        assert (reports_dir / self.LINK_NAME).exists(), "失敗した 1 件の移動元が削除された"
        assert (archive_dir / self.PLAIN_NAME).read_bytes() == LF_BODY, (
            "1 件失敗で残りの処理が打ち切られている（ADR-4）"
        )


# ===========================================================================
# 以下は改訂 5（E 再レビューの High 1 件 = CR-NEW / SR-V-002 の再発）の追加分。
#
# 設計 §3-3c【改訂 5】: 移動先の判定を `is_symlink()` から **realpath 封じ込め**へ置き換える。
#   `os.path.realpath(archive_dir)` が `os.path.realpath(reports_dir)` 直下の `archive` と
#   一致しなければ、reparse の種別を問わず **1 件も処理せず exit 1**。
#   検査箇所は改訂 4 と同じ 2 つ（`mkdir` の前・`os.open` の前）。
#   `src` 側（ファイル）の `is_symlink()` 判定は**据え置き**（ジャンクションはディレクトリ専用）。
# ===========================================================================

ARCHIVE_NAME = "archive"


@pytest.fixture
def junction():
    """NTFS ディレクトリジャンクションを作るファクトリ。作れない環境では skip する。

    `mklink /J` ではなく `_winapi.CreateJunction(target, link)` を使う（Python 3.8+ の
    非公開 API。本開発機では **管理者権限なしに成功する**ことを実測済み）。シェル経由の
    `mklink` はパスの受け渡しで失敗するため採らない。

    **skip に逃げる前に「作成を試みて失敗した」ことが分かる形にする**（設計上の要請）:
    skip 理由には試行した API と実際の例外を必ず載せる。

    **後始末**: `os.rmdir(link)` で **リンク自体を先に外す**。ジャンクションを張ったまま
    一時ディレクトリを消すと、リンク先を巻き込んで削除しうる（`shutil.rmtree` は
    ジャンクションを辿る）。`os.rmdir` はリンク先の中身を消さないことを実測済み。
    """
    created: list[Path] = []

    def _create(link: Path, target: Path) -> Path:
        target.mkdir(parents=True, exist_ok=True)
        try:
            import _winapi
        except ImportError as exc:
            pytest.skip(
                "ジャンクションの作成を試みられなかった（`_winapi` が import できない"
                f"＝非 Windows 環境）: {type(exc).__name__}: {exc}"
            )
        creator = getattr(_winapi, "CreateJunction", None)
        if creator is None:
            pytest.skip(
                "ジャンクションの作成を試みられなかった（`_winapi.CreateJunction` が無い）"
            )
        try:
            creator(str(target), str(link))
        except (OSError, NotImplementedError, ValueError) as exc:
            pytest.skip(
                f"ジャンクションの作成を試みたが失敗した（{link} -> {target}）: "
                f"{type(exc).__name__}: {exc}"
            )
        created.append(link)
        return link

    yield _create

    for link in reversed(created):
        try:
            os.rmdir(str(link))
        except OSError:
            pass


def assert_junction_is_invisible_to_is_symlink(link: Path) -> None:
    """本スライスの前提（ジャンクションは `is_symlink()` に捕まらない）を明示する。

    この前提が崩れると改訂 4 の `is_symlink()` 判定だけで塞げてしまい、以下のテストは
    「realpath 封じ込め」ではなく別の理由で緑になる（＝測っている性質が変わる）。
    """
    assert link.is_dir(), f"前提が崩れた: ジャンクションが is_dir() で見えない: {link}"
    assert not link.is_symlink(), (
        "前提が崩れた: この環境ではジャンクションが `is_symlink()` に捕まっている。\n"
        "本スライスは「`is_symlink()` では捕まらない reparse point がある」ことを前提に、"
        "判定を realpath 封じ込めへ置き換える（設計 §3-3c 改訂 5）。"
    )


class TestArchiveDirJunctionIsRefused:
    """設計 §3-3c【改訂 5】: アーカイブ先がジャンクションなら 1 件も処理せず exit 1。

    改訂 4 の `archive_dir.is_symlink()` は **NTFS ディレクトリジャンクションに `False` を
    返す**ため素通りする。`Path.mkdir(exist_ok=True)` はリンク先が実在ディレクトリなら
    素通りし、`os.open` の `O_EXCL` は最終コンポーネントにしか効かないため、
    **移動先をすり替えたうえで移動元が削除される**（是正前は exit 0 の成功扱い）。
    """

    FIRST = "architecture-report-20260807-090501.md"
    SECOND = "plan-report-20260808-101112.md"
    ONE = "requirements-report-20260808-101112.md"

    def _setup(self, tmp_path: Path) -> Path:
        reports_dir = make_reports_dir(tmp_path)
        write_report(reports_dir, self.FIRST, LF_BODY, mtime=FIXED_MTIME_B)
        write_report(reports_dir, self.SECOND, CRLF_BODY, mtime=FIXED_MTIME_A)
        return reports_dir

    def _assert_sources_untouched(self, reports_dir: Path) -> None:
        assert (reports_dir / self.FIRST).read_bytes() == LF_BODY, (
            "移動元が削除・改変された（1 件も処理してはならない）"
        )
        assert (reports_dir / self.SECOND).read_bytes() == CRLF_BODY, (
            "移動元が削除・改変された（1 件も処理してはならない）"
        )

    def test_junction_archive_dir_processes_nothing_and_exits_one(
        self, tmp_path, monkeypatch, junction
    ):
        """性質 1: `--reports-dir` 経路。ジャンクションの移動先へは 1 件も書かれず exit 1。"""
        module = load_archive_module()

        reports_dir = self._setup(tmp_path)
        outside = tmp_path / "outside"
        link = junction(reports_dir / ARCHIVE_NAME, outside)
        assert_junction_is_invisible_to_is_symlink(link)

        rc, _out, err = run_capturing(module, monkeypatch, "--reports-dir", str(reports_dir))

        assert rc == 1, (
            "アーカイブ先がジャンクションなのに exit 1 になっていない"
            "（設計 §3-3c 改訂 5: reparse の種別を問わず 1 件も処理せず exit 1）。\n"
            f"stderr: {err!r}"
        )
        leaked = sorted(p.name for p in outside.iterdir())
        assert leaked == [], (
            f"リンク先へ書き込みが漏れた: {leaked}（移動先すり替えの経路そのもの）"
        )
        self._assert_sources_untouched(reports_dir)

    def test_default_reports_dir_path_is_guarded_too(self, tmp_path, monkeypatch, junction):
        """性質 2: 既定経路（`--reports-dir` を渡さない形）でも同じ判定が効く。

        `.claude/reports/archive` をジャンクション化できる主体には env ゲートが一切関与しない
        （設計 §3-3c 改訂 5）。したがってゲートは本件の緩和策にならず、既定経路側でも
        判定が要る。**env は外した状態**で測ることでその点を明示する。

        **実リポジトリの `.claude/reports` は絶対に対象にしない**。既定値の解決規則
        （`__file__` から `parents[4]`・設計 §3-1）には依存せず、遅延評価される解決関数
        （設計 §3-3b「引数パース後に遅延評価する」）を一時ディレクトリへ差し替える。
        差し替えが効かない実装に備えて、破壊操作の直前でも対象を検査する安全網を置く。
        """
        module = load_archive_module()

        reports_dir = self._setup(tmp_path)
        outside = tmp_path / "outside"
        link = junction(reports_dir / ARCHIVE_NAME, outside)
        assert_junction_is_invisible_to_is_symlink(link)

        monkeypatch.setattr(module, "_default_reports_dir", lambda: reports_dir)
        assert module._default_reports_dir() == reports_dir, (
            "安全確認に失敗: 既定の対象ディレクトリを一時ディレクトリへ差し替えられていない"
        )

        # 安全網: 万一 seam が効かなくても、実リポジトリのレポートを 1 件も動かさない。
        original_archive_all = module._archive_all

        def guarded_archive_all(targets, archive_dir):
            assert Path(archive_dir) == reports_dir / ARCHIVE_NAME, (
                "安全網が発火: 破壊操作の対象が一時ディレクトリではない"
                f"（{archive_dir}）。テストの seam が実装に効いていない"
            )
            return original_archive_all(targets, archive_dir)

        monkeypatch.setattr(module, "_archive_all", guarded_archive_all)

        # env ゲートは本件の緩和策にならない（既定経路にはそもそも掛からない）。
        monkeypatch.delenv(REPORTS_DIR_OK_ENV, raising=False)
        assert REPORTS_DIR_OK_ENV not in os.environ

        rc, _out, err = run_capturing(module, monkeypatch)

        assert rc == 1, (
            "既定経路ではジャンクションの移動先が素通りしている"
            "（env ゲートは既定経路に掛からないため、判定側で塞ぐ必要がある）。\n"
            f"stderr: {err!r}"
        )
        leaked = sorted(p.name for p in outside.iterdir())
        assert leaked == [], f"既定経路でリンク先へ書き込みが漏れた: {leaked}"
        self._assert_sources_untouched(reports_dir)

    def test_only_the_junction_archive_makes_the_difference(
        self, tmp_path, monkeypatch, junction
    ):
        """性質 4 + 性質 1: 封じ込めが厳しすぎず、かつジャンクションだけを拒否する。

        同じ「ジャンクション経由で辿るツリー」の下に 2 つの対象ディレクトリを置き、
        **違いは `archive/` が実ディレクトリかジャンクションかだけ**にする。

        - `ok/`: `archive/` が実ディレクトリ → 従来どおり移動して exit 0
          （`--reports-dir` の値自体がジャンクションを含む形。`Path.resolve()` で
          リンクが解ける経路であり、封じ込めを素朴に実装すると正規経路を壊しうる）
        - `evil/`: `archive/` がジャンクション → 1 件も処理せず exit 1

        正常系だけを独立したテストにしないのは、是正**前**でも自明に緑で
        「既存の挙動を測るだけ」になるため。拒否と同じテストへ畳み込むことで、
        是正が厳しすぎて正常系を壊した場合も同じ場所で赤にできる。
        """
        module = load_archive_module()

        real_root = tmp_path / "real-root"
        entry = junction(tmp_path / "entry", real_root)
        assert_junction_is_invisible_to_is_symlink(entry)

        ok_dir = entry / "ok"
        ok_dir.mkdir()
        write_report(ok_dir, self.ONE, LF_BODY, mtime=FIXED_MTIME_B)
        rc_ok = run_main(module, "--reports-dir", str(ok_dir))
        moved_ok = (real_root / "ok" / ARCHIVE_NAME / self.ONE).is_file()

        evil_dir = entry / "evil"
        evil_dir.mkdir()
        write_report(evil_dir, self.ONE, CRLF_BODY, mtime=FIXED_MTIME_A)
        outside = tmp_path / "outside"
        junction(evil_dir / ARCHIVE_NAME, outside)
        rc_evil, _out, err = run_capturing(module, monkeypatch, "--reports-dir", str(evil_dir))

        assert (rc_ok, moved_ok) == (0, True), (
            "封じ込めが厳しすぎる: `archive/` が実ディレクトリなのに正常系が壊れた"
            f"（rc={rc_ok} moved={moved_ok}）。`--reports-dir` がジャンクション経由で"
            "渡される形は正規の利用形態でも起こりうる（利用先の `.claude` がリンク配下にある等）"
        )
        assert rc_evil == 1, (
            "同じツリーでも `archive/` がジャンクションなら 1 件も処理せず exit 1"
            f"（設計 §3-3c 改訂 5）。実際 rc={rc_evil}。stderr: {err!r}"
        )
        assert sorted(p.name for p in outside.iterdir()) == [], "リンク先へ書き込みが漏れた"
        assert (real_root / "evil" / self.ONE).read_bytes() == CRLF_BODY, (
            "拒否したのに移動元が削除・改変された"
        )


class TestArchiveDirMustResolveInsideReportsDir:
    """設計 §3-3c【改訂 5】の判定そのもの（reparse の種別に依存しない形）。

    ジャンクションも真のシンボリックリンクも作れない環境（特権不足・非 NTFS・
    非 Windows CI の組み合わせ）でも契約を測れるよう、**「アーカイブ先が対象ディレクトリの
    外へ解決される」状態をパス解決の層で直接作る**。設計が名指しする `os.path.realpath` と、
    実装が選びうる等価 API の `Path.resolve` の**両方**を差し替える。

    改訂 4 の `is_symlink()` は素通りするため、このテストは是正前には赤になる。
    真のシンボリックリンクに対する既存の拒否（`TestArchiveDirSymlinkIsRefused` の
    実挙動テスト）は「アーカイブ先が外へ解決される」の一事例であり、本判定に包含される。
    """

    NAME = "plan-report-20260808-101112.md"

    def test_archive_dir_resolving_outside_processes_nothing(self, tmp_path, monkeypatch):
        module = load_archive_module()

        reports_dir = make_reports_dir(tmp_path)
        write_report(reports_dir, self.NAME, CRLF_BODY, mtime=FIXED_MTIME_A)
        archive_dir = reports_dir / ARCHIVE_NAME
        archive_dir.mkdir()

        outside = tmp_path / "outside"
        outside.mkdir()
        outside_real = os.path.realpath(str(outside))

        original_realpath = os.path.realpath
        original_resolve = Path.resolve

        def is_the_archive_dir(value: object) -> bool:
            try:
                return PurePath(os.fspath(value)) == PurePath(archive_dir)
            except TypeError:
                return False

        def fake_realpath(path, *args, **kwargs):
            if is_the_archive_dir(path):
                return outside_real
            return original_realpath(path, *args, **kwargs)

        def fake_resolve(self, *args, **kwargs):
            if is_the_archive_dir(self):
                return Path(outside_real)
            return original_resolve(self, *args, **kwargs)

        monkeypatch.setattr(os.path, "realpath", fake_realpath)
        monkeypatch.setattr(Path, "resolve", fake_resolve)

        rc, _out, err = run_capturing(module, monkeypatch, "--reports-dir", str(reports_dir))

        assert rc == 1, (
            "アーカイブ先が対象ディレクトリの外へ解決されるのに exit 1 になっていない"
            "（設計 §3-3c 改訂 5 の realpath 封じ込めが未実装）。\n"
            f"stderr: {err!r}"
        )
        written = sorted(p.name for p in archive_dir.iterdir())
        assert written == [], f"1 件も処理してはならないのに移動先へ書き込まれた: {written}"
        assert sorted(p.name for p in outside.iterdir()) == [], "外部ディレクトリへ書き込まれた"
        assert (reports_dir / self.NAME).read_bytes() == CRLF_BODY, "移動元が削除・改変された"
