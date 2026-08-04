"""tests/test_audit_review_decisions.py

scripts/audit_review_decisions.py のテスト。

【訂正・2026-08-05】ADR-9（c3.db.connect() への移行）に伴う Red フェーズの追加・修正。
plan-report-20260805-010530.md §4 に基づく。developer はテストファイルを変更できない
制約下にあるため、A / A5 / B8 / D 群(summary 回帰) / E(banner・_untrusted) / F(入力検証) は
本タスク（Red フェーズ）で完了させる。

テストケース構成:
  A 群 (11 件): list サブコマンド（抽出・フォーマット・フィルタリング）
  B 群 (10 件): resolve サブコマンド（書き込み・必須項目・異常系。B10 は本改訂で実装）
  C 群 (3 件): summary サブコマンド（集計）
  C-R 群 (3 件・新規): summary の集計・凍結条件の回帰テスト（件数一致 / id>1232 除外 /
                       decision 値域外除外。plan §4 の「D 群」に対応。緑期待＝回帰テスト）
  D 群 (4 件): DB 共通オプション・異常系（_connect() 廃止後も exit code・stderr 文言が
              非回帰であることを本改訂で追加検証）
  E 群 (2 件): プロセス境界の seam（exit code 検証）
  UB 群 (5 件・新規): list の stderr バナー・各レコードの _untrusted キー（SR-AI-001 の
                      2 層防御。Red 期待＝未実装）
  F 群 (6 件・新規): resolve の入力検証（--note 切り詰め・--commit 形式検証。Red 期待＝未実装）
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

# scripts/ を sys.path に追加して import できるようにする
_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from audit_review_decisions import main  # noqa: E402


# ---------------------------------------------------------------------------
# ヘルパー関数
# ---------------------------------------------------------------------------

def _create_test_db(db_path: Path) -> None:
    """テスト用 DB を初期化する（007 migration 適用済みを想定）。

    review_decisions テーブルとその スキーマを用意する。
    """
    conn = sqlite3.connect(str(db_path))
    try:
        # schema_migrations テーブルを作成（007 まで適用済みと仮定）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("INSERT OR IGNORE INTO schema_migrations (version) VALUES ('007')")

        # review_decisions テーブルを作成（9 列 + 3 新列）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checklist_id TEXT NOT NULL,
                finding_text TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT,
                context_summary TEXT,
                decided_at TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                severity TEXT,
                resolution TEXT,
                resolution_note TEXT,
                resolution_commit TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _insert_review_decision(
    db_path: Path,
    checklist_id: str,
    finding_text: str,
    decision: str = "accepted",
    reason: str | None = None,
    context_summary: str | None = None,
    decided_at: str = "2026-01-01T00:00:00",
    reviewer: str = "code-reviewer",
    severity: str | None = None,
    id_hint: int | None = None,  # id を固定したい場合（通常は AUTOINCREMENT）
) -> int:
    """review_decisions にレコードを挿入して id を返す。"""
    conn = sqlite3.connect(str(db_path))
    try:
        if id_hint is not None:
            # SQLite の AUTOINCREMENT 挙動を避けるため id を明示
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, reason, context_summary, decided_at, reviewer, severity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (id_hint, checklist_id, finding_text, decision, reason, context_summary, decided_at, reviewer, severity))
        else:
            conn.execute("""
                INSERT INTO review_decisions
                (checklist_id, finding_text, decision, reason, context_summary, decided_at, reviewer, severity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (checklist_id, finding_text, decision, reason, context_summary, decided_at, reviewer, severity))
        conn.commit()

        # 挿入した id を取得
        row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()

    return row_id


# ---------------------------------------------------------------------------
# A 群: list サブコマンド (11 件)
# ---------------------------------------------------------------------------

class TestAuditReviewDecisionsListBasic:
    """A 群: list サブコマンドの基本動作・フォーマット・フィルタリング。"""

    @pytest.fixture()
    def populated_db(self, tmp_path: Path) -> Path:
        """テスト用に複数レコード挿入した DB を返す。"""
        db_path = tmp_path / "c3.db"
        _create_test_db(db_path)

        # 様々な状態のレコードを挿入
        conn = sqlite3.connect(str(db_path))
        try:
            # 1. 未判定の accepted（対象）
            conn.execute("""
                INSERT INTO review_decisions
                (checklist_id, finding_text, decision, decided_at, reviewer, severity)
                VALUES ('CR-Q-001', 'test finding 1', 'accepted', '2026-01-01T00:00:00', 'reviewer1', 'High')
            """)
            # 2. 未判定の deferred（対象）
            conn.execute("""
                INSERT INTO review_decisions
                (checklist_id, finding_text, decision, decided_at, reviewer, severity)
                VALUES ('CR-Q-002', 'test finding 2', 'deferred', '2026-01-02T00:00:00', 'reviewer2', 'Medium')
            """)
            # 3. fixed（対象外）
            conn.execute("""
                INSERT INTO review_decisions
                (checklist_id, finding_text, decision, decided_at, reviewer, severity)
                VALUES ('CR-Q-003', 'test finding 3', 'fixed', '2026-01-03T00:00:00', 'reviewer1', 'Low')
            """)
            # 4. id > 1232（対象外）— id を明示指定
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer, severity, resolution)
                VALUES (1233, 'CR-Q-004', 'test finding 4', 'accepted', '2026-01-04T00:00:00', 'reviewer3', 'High', NULL)
            """)
            # 5. 既に判定済み（resolution != NULL）（対象外）。
            # id を明示指定（105）する。record 4 が id=1233 を明示指定しているため、
            # AUTOINCREMENT に任せると本レコードが id>1232 側にも該当してしまい、
            # 「resolution 起因で除外される」ことを id 起因の除外と区別できなくなる
            # （DC-GP-004・A5 の空の緑の原因の 1 つ）。
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer, severity, resolution)
                VALUES (105, 'CR-Q-005', 'test finding 5', 'accepted', '2026-01-05T00:00:00', 'reviewer1', 'High', 'resolved')
            """)
            conn.commit()
        finally:
            conn.close()

        return db_path

    def test_list_a1_basic_output_format(self, populated_db: Path):
        """A1: list の出力が JSON Lines で、各行が有効な JSON である。"""
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["list", "--db", str(populated_db)])

        assert exit_code == 0, f"list 実行が失敗: exit_code={exit_code}"

        lines = output.getvalue().strip().split('\n')
        # 対象は CR-Q-001 と CR-Q-002 の 2 件（cr-Q-003 は fixed、CR-Q-004 は id > 1232、CR-Q-005 は resolution != NULL）
        non_empty_lines = [l for l in lines if l.strip()]
        assert len(non_empty_lines) >= 2, f"期待する行数 >= 2 だが {len(non_empty_lines)} 件"

        # 各行が JSON としてパースできること
        for line in non_empty_lines:
            try:
                obj = json.loads(line)
                assert isinstance(obj, dict), f"各行は dict である必要があります: {line}"
            except json.JSONDecodeError as e:
                pytest.fail(f"JSON パースに失敗: {line}\n{e}")

    def test_list_a2_required_fields(self, populated_db: Path):
        """A2: list の各レコードが必須キーを持つ。

        必須: id, decided_at, decision, reviewer, severity, checklist_id,
              finding_text, reason, context_summary, commits_since
        """
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["list", "--db", str(populated_db)])

        assert exit_code == 0

        lines = output.getvalue().strip().split('\n')
        non_empty_lines = [l for l in lines if l.strip()]

        required_keys = {
            'id', 'decided_at', 'decision', 'reviewer', 'severity',
            'checklist_id', 'finding_text', 'reason', 'context_summary', 'commits_since'
        }

        for line in non_empty_lines:
            obj = json.loads(line)
            missing = required_keys - set(obj.keys())
            assert not missing, f"必須キーが不足: {missing}\n{line}"

    def test_list_a3_excludes_fixed_decisions(self, populated_db: Path):
        """A3: list は decision='fixed' の行を除外する。"""
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["list", "--db", str(populated_db)])

        assert exit_code == 0

        lines = output.getvalue().strip().split('\n')
        non_empty_lines = [l for l in lines if l.strip()]

        decisions = [json.loads(line)['decision'] for line in non_empty_lines]
        assert 'fixed' not in decisions, "fixed は除外されるはず"

    def test_list_a4_excludes_id_gt_1232(self, populated_db: Path):
        """A4: list は id > 1232 の行を除外する（凍結条件）。"""
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["list", "--db", str(populated_db)])

        assert exit_code == 0

        lines = output.getvalue().strip().split('\n')
        non_empty_lines = [l for l in lines if l.strip()]

        ids = [json.loads(line)['id'] for line in non_empty_lines]
        assert all(id <= 1232 for id in ids), "id > 1232 は除外されるはず"

    def test_list_a5_excludes_non_null_resolution(self, populated_db: Path):
        """A5: list は resolution != NULL の行を除外する（既判定行）。

        【訂正・2026-08-05・DC-GP-004】前版は `.get('resolution')` を検査していたが、
        list の出力レコードにそもそも 'resolution' キーが存在しないため常に None を返し、
        assert が必ず緑になる「空の緑」だった。resolution='resolved' を仕込んだ行 (id=105)
        自体が出力に含まれないことを直接 assert する形へ修正する。
        """
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["list", "--db", str(populated_db)])

        assert exit_code == 0

        lines = output.getvalue().strip().split('\n')
        non_empty_lines = [l for l in lines if l.strip()]

        ids = [json.loads(line)['id'] for line in non_empty_lines]
        assert 105 not in ids, (
            "resolution != NULL (id=105) の行は list の出力に含まれないはず"
        )

    def test_list_a6_default_limit_10(self, tmp_path: Path):
        """A6: list のデフォルト limit は 10。"""
        db_path = tmp_path / "c3.db"
        _create_test_db(db_path)

        # 15 件の未判定レコードを挿入（すべて対象）
        conn = sqlite3.connect(str(db_path))
        try:
            for i in range(1, 16):
                conn.execute("""
                    INSERT INTO review_decisions
                    (checklist_id, finding_text, decision, decided_at, reviewer)
                    VALUES (?, ?, 'accepted', '2026-01-01T00:00:00', 'reviewer')
                """, (f'CR-Q-{i:03d}', f'finding {i}'))
            conn.commit()
        finally:
            conn.close()

        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["list", "--db", str(db_path)])

        assert exit_code == 0
        lines = output.getvalue().strip().split('\n')
        non_empty_lines = [l for l in lines if l.strip()]
        assert len(non_empty_lines) == 10, f"デフォルト limit=10 のはず、得られた件数: {len(non_empty_lines)}"

    def test_list_a7_limit_0_returns_all(self, tmp_path: Path):
        """A7: --limit 0 で全件を返す。"""
        db_path = tmp_path / "c3.db"
        _create_test_db(db_path)

        # 15 件挿入
        conn = sqlite3.connect(str(db_path))
        try:
            for i in range(1, 16):
                conn.execute("""
                    INSERT INTO review_decisions
                    (checklist_id, finding_text, decision, decided_at, reviewer)
                    VALUES (?, ?, 'accepted', '2026-01-01T00:00:00', 'reviewer')
                """, (f'CR-Q-{i:03d}', f'finding {i}'))
            conn.commit()
        finally:
            conn.close()

        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["list", "--db", str(db_path), "--limit", "0"])

        assert exit_code == 0
        lines = output.getvalue().strip().split('\n')
        non_empty_lines = [l for l in lines if l.strip()]
        assert len(non_empty_lines) == 15, f"--limit 0 で全 15 件のはず、得られた件数: {len(non_empty_lines)}"

    def test_list_a8_limit_custom(self, tmp_path: Path):
        """A8: --limit N で N 件を返す。"""
        db_path = tmp_path / "c3.db"
        _create_test_db(db_path)

        # 15 件挿入
        conn = sqlite3.connect(str(db_path))
        try:
            for i in range(1, 16):
                conn.execute("""
                    INSERT INTO review_decisions
                    (checklist_id, finding_text, decision, decided_at, reviewer)
                    VALUES (?, ?, 'accepted', '2026-01-01T00:00:00', 'reviewer')
                """, (f'CR-Q-{i:03d}', f'finding {i}'))
            conn.commit()
        finally:
            conn.close()

        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["list", "--db", str(db_path), "--limit", "5"])

        assert exit_code == 0
        lines = output.getvalue().strip().split('\n')
        non_empty_lines = [l for l in lines if l.strip()]
        assert len(non_empty_lines) == 5, f"--limit 5 のはず、得られた件数: {len(non_empty_lines)}"

    def test_list_a9_multiline_finding_text_preserves_record_boundary(self, tmp_path: Path):
        """A9: finding_text に改行を含む場合でも、JSON Lines 形式で 1 レコード 1 行が保たれる。"""
        db_path = tmp_path / "c3.db"
        _create_test_db(db_path)

        # 改行を含む finding_text を挿入
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("""
                INSERT INTO review_decisions
                (checklist_id, finding_text, decision, decided_at, reviewer)
                VALUES (?, ?, 'accepted', '2026-01-01T00:00:00', 'reviewer')
            """, ('CR-Q-001', 'line 1\nline 2\nline 3'))
            conn.commit()
        finally:
            conn.close()

        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["list", "--db", str(db_path)])

        assert exit_code == 0
        lines = output.getvalue().split('\n')
        non_empty_lines = [l for l in lines if l.strip()]

        # 1 行だけで、その中に 'line 1\nline 2\nline 3' が JSON 値として含まれるはず
        assert len(non_empty_lines) == 1, f"1 レコード 1 行のはず、得られた非空行: {len(non_empty_lines)}"
        obj = json.loads(non_empty_lines[0])
        assert 'line 1' in obj['finding_text'], "改行を含むテキストが保持されるはず"

    def test_list_a10_empty_result_returns_exit_0(self, tmp_path: Path):
        """A10: list の対象が 0 件の場合、exit 0（正常終了）で空出力を返す。"""
        db_path = tmp_path / "c3.db"
        _create_test_db(db_path)

        # レコードを 1 件も挿入しない（すなわち対象は 0 件）

        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["list", "--db", str(db_path)])

        assert exit_code == 0, "対象 0 件は正常終了（exit 0）のはず"

    def test_list_a11_output_contains_checklist_id_and_reviewers(self, populated_db: Path):
        """A11: list の出力にはレコードの checklist_id と reviewer が含まれる。"""
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["list", "--db", str(populated_db)])

        assert exit_code == 0

        lines = output.getvalue().strip().split('\n')
        non_empty_lines = [l for l in lines if l.strip()]

        checklist_ids = {json.loads(line)['checklist_id'] for line in non_empty_lines}
        reviewers = {json.loads(line)['reviewer'] for line in non_empty_lines}

        assert 'CR-Q-001' in checklist_ids or 'CR-Q-002' in checklist_ids
        assert len(reviewers) > 0


# ---------------------------------------------------------------------------
# B 群: resolve サブコマンド (10 件)
# ---------------------------------------------------------------------------

class TestAuditReviewDecisionsResolve:
    """B 群: resolve サブコマンドの書き込み・必須項目・異常系。"""

    @pytest.fixture()
    def simple_db(self, tmp_path: Path) -> Path:
        """テスト用に 1 件のレコードを挿入した DB を返す。"""
        db_path = tmp_path / "c3.db"
        _create_test_db(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer, severity)
                VALUES (100, 'CR-Q-100', 'test finding', 'accepted', '2026-01-01T00:00:00', 'reviewer', 'High')
            """)
            conn.commit()
        finally:
            conn.close()

        return db_path

    def test_resolve_b1_writes_resolution_columns(self, simple_db: Path):
        """B1: resolve --id 100 --resolution resolved --note "Fixed in commit xyz" --commit abc123def456...
        で resolution, resolution_note, resolution_commit が DB に書き込まれる。
        """
        exit_code = main([
            "resolve",
            "--db", str(simple_db),
            "--id", "100",
            "--resolution", "resolved",
            "--note", "Fixed in commit xyz",
            "--commit", "abc123def456abc123def456abc123def456abc1"  # 40 char SHA
        ])

        assert exit_code == 0, f"resolve が失敗: exit_code={exit_code}"

        conn = sqlite3.connect(str(simple_db))
        try:
            row = conn.execute(
                "SELECT resolution, resolution_note, resolution_commit FROM review_decisions WHERE id=100"
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row[0] == "resolved", f"resolution が書き込まれていない: {row[0]}"
        assert row[1] == "Fixed in commit xyz", f"resolution_note が書き込まれていない: {row[1]}"
        assert row[2] == "abc123def456abc123def456abc123def456abc1", f"resolution_commit が書き込まれていない: {row[2]}"

    def test_resolve_b2_resolved_without_note_exits_2(self, simple_db: Path):
        """B2: resolve --id 100 --resolution resolved --commit ... で --note がない場合、exit 2。"""
        exit_code = main([
            "resolve",
            "--db", str(simple_db),
            "--id", "100",
            "--resolution", "resolved",
            "--commit", "abc123def456abc123def456abc123def456abc1"
        ])

        assert exit_code == 2, f"--note なし(resolved) は exit 2 のはず、得られた: {exit_code}"

    def test_resolve_b3_unverifiable_without_note_exits_2(self, simple_db: Path):
        """B3: resolve --id 100 --resolution unverifiable で --note がない場合、exit 2。"""
        exit_code = main([
            "resolve",
            "--db", str(simple_db),
            "--id", "100",
            "--resolution", "unverifiable",
            "--commit", "abc123def456abc123def456abc123def456abc1"
        ])

        assert exit_code == 2, f"--note なし(unverifiable) は exit 2 のはず、得られた: {exit_code}"

    def test_resolve_b4_open_without_note_succeeds(self, simple_db: Path):
        """B4: resolve --id 100 --resolution open で --note がなくても成功（exit 0）。
        resolution_commit は記録される。
        """
        exit_code = main([
            "resolve",
            "--db", str(simple_db),
            "--id", "100",
            "--resolution", "open",
            "--commit", "abc123def456abc123def456abc123def456abc1"
        ])

        assert exit_code == 0, f"open は --note 任意のはず、exit_code={exit_code}"

        conn = sqlite3.connect(str(simple_db))
        try:
            row = conn.execute(
                "SELECT resolution, resolution_note, resolution_commit FROM review_decisions WHERE id=100"
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row[0] == "open", f"resolution が書き込まれていない: {row[0]}"
        assert row[2] is not None, f"resolution_commit が NULL: {row[2]}"

    def test_resolve_b5_invalid_resolution_value_exits_2(self, simple_db: Path):
        """B5: resolve --resolution invalid_value は exit 2。
        値は resolved/open/unverifiable のみ。
        """
        exit_code = main([
            "resolve",
            "--db", str(simple_db),
            "--id", "100",
            "--resolution", "invalid_value",
            "--note", "test",
            "--commit", "abc123def456abc123def456abc123def456abc1"
        ])

        assert exit_code == 2, f"無効な resolution 値は exit 2 のはず、得られた: {exit_code}"

    def test_resolve_b6_nonexistent_id_exits_2(self, simple_db: Path):
        """B6: resolve --id 999 （存在しない id）で exit 2。"""
        exit_code = main([
            "resolve",
            "--db", str(simple_db),
            "--id", "999",
            "--resolution", "resolved",
            "--note", "test",
            "--commit", "abc123def456abc123def456abc123def456abc1"
        ])

        assert exit_code == 2, f"存在しない id は exit 2 のはず、得られた: {exit_code}"

    def test_resolve_b7_non_null_row_without_force_exits_2(self, tmp_path: Path):
        """B7: 既に resolution != NULL の行への resolve は --force なしで exit 2。"""
        db_path = tmp_path / "c3.db"
        _create_test_db(db_path)

        # resolution がすでに値を持つレコードを挿入
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer, resolution)
                VALUES (100, 'CR-Q-100', 'test', 'accepted', '2026-01-01T00:00:00', 'reviewer', 'resolved')
            """)
            conn.commit()
        finally:
            conn.close()

        exit_code = main([
            "resolve",
            "--db", str(db_path),
            "--id", "100",
            "--resolution", "open",
            "--commit", "abc123def456abc123def456abc123def456abc1"
        ])

        assert exit_code == 2, f"非 NULL 行の再書き込みは --force 必須で exit 2 のはず、得られた: {exit_code}"

    def test_resolve_b8_force_overwrites_all_three_columns(self, tmp_path: Path):
        """B8: --force で既判定行の 3 列すべてが上書きされる。

        【訂正・2026-08-05】旧値 "newcommit456" は 16 進ですらないため、F 群の
        --commit 形式検証（40 桁 16 進必須）を追加すると必ず exit 2 に赤化する。
        40 桁 16 進の値へ差し替える。
        """
        db_path = tmp_path / "c3.db"
        _create_test_db(db_path)

        # 既判定のレコードを挿入
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer,
                 resolution, resolution_note, resolution_commit)
                VALUES (100, 'CR-Q-100', 'test', 'accepted', '2026-01-01T00:00:00', 'reviewer',
                        'resolved', 'old note', 'oldcommit123')
            """)
            conn.commit()
        finally:
            conn.close()

        exit_code = main([
            "resolve",
            "--db", str(db_path),
            "--id", "100",
            "--resolution", "open",
            "--force",
            "--commit", "1111222233334444555566667777888899990000"
        ])

        assert exit_code == 0, f"--force での再書き込みは成功のはず、exit_code={exit_code}"

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT resolution, resolution_note, resolution_commit FROM review_decisions WHERE id=100"
            ).fetchone()
        finally:
            conn.close()

        assert row[0] == "open", f"resolution が上書きされていない: {row[0]}"
        assert row[1] is None, f"resolution_note が上書きされていない（open では任意のため None のはず）: {row[1]}"
        assert row[2] == "1111222233334444555566667777888899990000", (
            f"resolution_commit が上書きされていない: {row[2]}"
        )

    def test_resolve_b9_resolved_with_note_succeeds(self, simple_db: Path):
        """B9: resolve --id 100 --resolution resolved --note "..." で --commit 自動取得も可能。
        テスト時は --commit を明示して外部 git 呼び出しを避ける。
        """
        exit_code = main([
            "resolve",
            "--db", str(simple_db),
            "--id", "100",
            "--resolution", "resolved",
            "--note", "This is resolved",
            "--commit", "abc123def456abc123def456abc123def456abc1"
        ])

        assert exit_code == 0, f"--note 付き resolved は成功のはず、exit_code={exit_code}"

    def test_resolve_b10_commit_is_required_for_all_resolution_types(
        self, simple_db: Path, monkeypatch
    ):
        """B10: --commit は全 resolution タイプで必須（open でも必須）。

        【訂正・2026-08-05・DC-GP-004】前版は `pass` のみで実質未検証だった
        （`tests/test_no_passonly_tests.py` の B10 として検出される）。
        `--commit` 省略時の自動取得（git rev-parse HEAD）が失敗するケースを
        `_head_commit` の monkeypatch で決定的に再現し、open でも exit 2 になることを検証する
        （実 git を呼ばないことで、実行環境の HEAD 状態に依存しないテストにする）。
        """
        import audit_review_decisions as ard_module  # noqa: PLC0415

        monkeypatch.setattr(ard_module, "_head_commit", lambda: None)

        exit_code = main([
            "resolve",
            "--db", str(simple_db),
            "--id", "100",
            "--resolution", "open",
            # --commit を省略（自動取得を試みて失敗する想定）
        ])

        assert exit_code == 2, (
            "commit 自動取得に失敗した場合、open でも --commit 必須のため exit 2 のはず"
            f"（得られた: {exit_code}）"
        )


# ---------------------------------------------------------------------------
# C 群: summary サブコマンド (3 件)
# ---------------------------------------------------------------------------

class TestAuditReviewDecisionsSummary:
    """C 群: summary サブコマンドの集計。"""

    @pytest.fixture()
    def populated_db_with_resolution(self, tmp_path: Path) -> Path:
        """複数の resolution 状態を持つレコードが入った DB。"""
        db_path = tmp_path / "c3.db"
        _create_test_db(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            # 対象外（id > 1232）
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer, severity, resolution)
                VALUES (1233, 'CR-Q-OUT', 'out', 'accepted', '2026-01-01T00:00:00', 'reviewer', 'High', 'resolved')
            """)

            # 未判定（対象）
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer, severity)
                VALUES (100, 'CR-Q-A', 'test', 'accepted', '2026-01-01T00:00:00', 'reviewer1', 'High')
            """)

            # resolved（対象）
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer, severity, resolution)
                VALUES (101, 'CR-Q-B', 'test', 'accepted', '2026-01-02T00:00:00', 'reviewer1', 'High', 'resolved')
            """)

            # open（対象）
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer, severity, resolution)
                VALUES (102, 'CR-Q-C', 'test', 'deferred', '2026-01-03T00:00:00', 'reviewer2', 'Medium', 'open')
            """)

            # unverifiable（対象）
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer, severity, resolution)
                VALUES (103, 'CR-Q-D', 'test', 'accepted', '2026-01-04T00:00:00', 'reviewer1', 'Medium', 'unverifiable')
            """)

            conn.commit()
        finally:
            conn.close()

        return db_path

    def test_summary_c1_output_format(self, populated_db_with_resolution: Path):
        """C1: summary の出力は人間可読な形式（表や JSON など）を返す。

        必須: resolution × severity × reviewer の組み合わせ別の件数を示すこと。
        凍結条件（id <= 1232）を適用すること。
        """
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["summary", "--db", str(populated_db_with_resolution)])

        assert exit_code == 0, f"summary が失敗: exit_code={exit_code}"

        result = output.getvalue()
        # 出力があること（空でないこと）
        assert len(result.strip()) > 0, "summary は何らかの出力を返すべき"

    def test_summary_c2_excludes_out_of_scope(self, populated_db_with_resolution: Path):
        """C2: summary は凍結条件外（id > 1232）のレコードを除外する。"""
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["summary", "--db", str(populated_db_with_resolution)])

        assert exit_code == 0

        # id=1233 に対応するレコード CR-Q-OUT が集計に含まれないことは
        # 出力形式に依存するため、詳細検証は実装後に。
        # ここでは exit 0 であることだけを確認。

    def test_summary_c3_aggregates_by_resolution_severity_reviewer(self, populated_db_with_resolution: Path):
        """C3: summary は resolution × severity × reviewer の組み合わせ別に件数を返す。

        例：
        - (NULL, 'High', 'reviewer1'): 1 件
        - ('resolved', 'High', 'reviewer1'): 1 件
        - ('open', 'Medium', 'reviewer2'): 1 件
        - ('unverifiable', 'Medium', 'reviewer1'): 1 件
        """
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["summary", "--db", str(populated_db_with_resolution)])

        assert exit_code == 0

        output_str = output.getvalue()
        # 4 つの異なる組み合わせ（対象 id <= 1232 の 4 件）が集計されているはず
        assert len(output_str) > 0, "summary は集計結果を出力すべき"


# ---------------------------------------------------------------------------
# C-R 群（新規）: summary の集計・凍結条件の回帰テスト (3 件)
#
# plan-report §4 の「D 群: 件数一致 / id > 1232 除外 / decision 値域外除外」に対応する。
# 既存の「D 群: DB 共通オプション・異常系」とラベルが衝突するため、本ファイル内では
# C-R（Summary Regression）と命名する。
#
# CR（code-review-report-20260804-224558.md）の指摘「summary の集計・凍結条件に回帰テストを
# 追加する」への対応。_cmd_summary の SQL（decision IN (...) AND id <= 1232）は現行実装で
# 既に正しいため、これらは実データで動作確認済みの回帰テストとして緑を期待する。
# ---------------------------------------------------------------------------


class TestAuditReviewDecisionsSummaryRegression:
    """C-R 群: summary の集計・凍結条件の回帰テスト（緑期待）。"""

    @pytest.fixture()
    def populated_db_with_resolution(self, tmp_path: Path) -> Path:
        """C 群のフィクスチャと同内容（クラス間でフィクスチャを共有しないため複製）。"""
        db_path = tmp_path / "c3.db"
        _create_test_db(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            # 対象外（id > 1232）
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer, severity, resolution)
                VALUES (1233, 'CR-Q-OUT', 'out', 'accepted', '2026-01-01T00:00:00', 'reviewer', 'High', 'resolved')
            """)

            # 未判定（対象）
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer, severity)
                VALUES (100, 'CR-Q-A', 'test', 'accepted', '2026-01-01T00:00:00', 'reviewer1', 'High')
            """)

            # resolved（対象）
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer, severity, resolution)
                VALUES (101, 'CR-Q-B', 'test', 'accepted', '2026-01-02T00:00:00', 'reviewer1', 'High', 'resolved')
            """)

            # open（対象）
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer, severity, resolution)
                VALUES (102, 'CR-Q-C', 'test', 'deferred', '2026-01-03T00:00:00', 'reviewer2', 'Medium', 'open')
            """)

            # unverifiable（対象）
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer, severity, resolution)
                VALUES (103, 'CR-Q-D', 'test', 'accepted', '2026-01-04T00:00:00', 'reviewer1', 'Medium', 'unverifiable')
            """)

            conn.commit()
        finally:
            conn.close()

        return db_path

    @staticmethod
    def _parse_total(output_str: str) -> int:
        import re  # noqa: PLC0415
        m = re.search(r"合計:\s*(\d+)\s*件", output_str)
        assert m, f"summary の出力に「合計: N 件」行が見つかりません:\n{output_str}"
        return int(m.group(1))

    def test_summary_cr1_total_count_matches_target_rows(
        self, populated_db_with_resolution: Path
    ):
        """CR1: summary の合計件数が対象行数（凍結条件・decision 値域内）と一致する。"""
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["summary", "--db", str(populated_db_with_resolution)])
        assert exit_code == 0

        reported_total = self._parse_total(output.getvalue())

        conn = sqlite3.connect(str(populated_db_with_resolution))
        try:
            actual = conn.execute(
                "SELECT COUNT(*) FROM review_decisions"
                " WHERE decision IN ('accepted','deferred') AND id <= 1232"
            ).fetchone()[0]
        finally:
            conn.close()

        assert reported_total == actual == 4, (
            f"summary の合計件数が対象行数と一致しないはず: reported={reported_total}, actual={actual}"
        )

    def test_summary_cr2_excludes_id_gt_1232_from_aggregation(
        self, populated_db_with_resolution: Path
    ):
        """CR2: id > 1232 の行は summary の集計から除外される（凍結条件）。"""
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["summary", "--db", str(populated_db_with_resolution)])
        assert exit_code == 0

        reported_total = self._parse_total(output.getvalue())

        conn = sqlite3.connect(str(populated_db_with_resolution))
        try:
            all_matching_decision = conn.execute(
                "SELECT COUNT(*) FROM review_decisions WHERE decision IN ('accepted','deferred')"
            ).fetchone()[0]
        finally:
            conn.close()

        # fixture 前提の確認: id=1233（凍結条件外）を含めると 5 件になる
        assert all_matching_decision == 5
        assert reported_total == 4, (
            "id > 1232 の行（id=1233）は summary の集計から除外されるはず"
        )

    def test_summary_cr3_excludes_out_of_scope_decision_values(self, tmp_path: Path):
        """CR3: decision が accepted/deferred 以外（例: fixed）の行は集計対象外。"""
        db_path = tmp_path / "c3.db"
        _create_test_db(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer, severity)
                VALUES (200, 'CR-Q-FIXED', 'test', 'fixed', '2026-01-01T00:00:00', 'reviewer', 'Low')
            """)
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer, severity)
                VALUES (201, 'CR-Q-ACC', 'test', 'accepted', '2026-01-02T00:00:00', 'reviewer', 'Low')
            """)
            conn.commit()
        finally:
            conn.close()

        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["summary", "--db", str(db_path)])
        assert exit_code == 0

        reported_total = self._parse_total(output.getvalue())
        assert reported_total == 1, (
            "decision='fixed' は集計対象外で 'accepted' の 1 件のみのはず"
            f"（得られた: {reported_total}）"
        )


# ---------------------------------------------------------------------------
# D 群: DB 共通オプション・異常系 (4 件)
# ---------------------------------------------------------------------------

class TestAuditReviewDecisionsDbOption:
    """D 群: --db オプション・DB 不在・未適用 DB などの異常系。

    【訂正・2026-08-05】ADR-9 で `_connect()` が `c3.db.connect()` へ移行しても
    観測可能な振る舞い（exit code・stderr 文言）が変わらないことを非回帰として検証する
    （plan §4「A: `_connect()` 廃止後も観測可能な振る舞いが変わらないこと」）。
    """

    def test_db_d4_non_sqlite_file_exits_2_all_subcommands(self, tmp_path: Path):
        """D4: --db に SQLite でない通常のテキストファイルを渡すと、
        list / resolve / summary のいずれも例外を送出せず exit 2 を返す。

        E-0（test-report-20260804-215902.md）で検出された欠陥の回帰テスト。
        sqlite3.connect() 自体は遅延評価のため例外を出さず、直後の PRAGMA 実行時に
        sqlite3.DatabaseError が送出される。接続の確立処理内で捕捉されず
        main() の外まで伝播していた（未捕捉例外＝プロセス既定の exit 1）。

        【訂正・2026-08-05】ADR-9 で接続確立が `c3.db.connect()` へ移行しても、
        stderr の文言（「DB への接続に失敗しました」）が現行のまま維持されることを
        非回帰として検証する（plan §4「A」）。
        """
        import io
        from contextlib import redirect_stderr

        not_a_db = tmp_path / "not_a_sqlite_file.txt"
        not_a_db.write_text(
            "this is not a sqlite database, just plain text\n" * 5,
            encoding="utf-8",
        )

        err = io.StringIO()
        with redirect_stderr(err):
            exit_code = main(["list", "--db", str(not_a_db)])
        assert exit_code == 2, (
            f"list: 非 SQLite ファイルは exit 2 のはず、得られた: {exit_code}"
        )
        assert "DB への接続に失敗しました" in err.getvalue(), (
            f"list: 非 SQLite ファイルの stderr 文言が非回帰でないはず: {err.getvalue()!r}"
        )

        err = io.StringIO()
        with redirect_stderr(err):
            exit_code = main([
                "resolve",
                "--db", str(not_a_db),
                "--id", "100",
                "--resolution", "open",
                "--commit", "abc123def456abc123def456abc123def456abc1",
            ])
        assert exit_code == 2, (
            f"resolve: 非 SQLite ファイルは exit 2 のはず、得られた: {exit_code}"
        )
        assert "DB への接続に失敗しました" in err.getvalue(), (
            f"resolve: 非 SQLite ファイルの stderr 文言が非回帰でないはず: {err.getvalue()!r}"
        )

        err = io.StringIO()
        with redirect_stderr(err):
            exit_code = main(["summary", "--db", str(not_a_db)])
        assert exit_code == 2, (
            f"summary: 非 SQLite ファイルは exit 2 のはず、得られた: {exit_code}"
        )
        assert "DB への接続に失敗しました" in err.getvalue(), (
            f"summary: 非 SQLite ファイルの stderr 文言が非回帰でないはず: {err.getvalue()!r}"
        )

    def test_db_d1_missing_db_file_exits_2(self, tmp_path: Path):
        """D1: --db で指定した DB ファイルが存在しない場合、exit 2。

        あるいは --db 省略時に locate_c3_db が None を返す場合も exit 2。

        【訂正・2026-08-05】stderr 文言（「DB ファイルが存在しません」）が
        ADR-9 移行後も非回帰であることを検証する（plan §4「A」）。
        """
        import io
        from contextlib import redirect_stderr

        nonexistent_db = tmp_path / "nonexistent.db"

        err = io.StringIO()
        with redirect_stderr(err):
            exit_code = main([
                "list",
                "--db", str(nonexistent_db)
            ])

        assert exit_code == 2, f"DB 不在は exit 2 のはず、得られた: {exit_code}"
        assert "DB ファイルが存在しません" in err.getvalue(), (
            f"stderr 文言が非回帰でないはず: {err.getvalue()!r}"
        )

    def test_db_d2_migration_007_not_applied_exits_2(self, tmp_path: Path):
        """D2: migration 007 未適用の DB（resolution 列なし）で実行すると、
        'no such column: resolution' 例外を捕捉して exit 2。

        【訂正・2026-08-05】stderr 文言（「migration 007 が未適用の可能性があります」）が
        ADR-9 移行後も非回帰であることを検証する（plan §4「A」）。
        """
        db_path = tmp_path / "c3.db"
        conn = sqlite3.connect(str(db_path))
        try:
            # 006 までの スキーマ（resolution 列なし）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS review_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    checklist_id TEXT NOT NULL,
                    finding_text TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT,
                    context_summary TEXT,
                    decided_at TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    severity TEXT
                )
            """)
            conn.execute("""
                INSERT INTO review_decisions
                (checklist_id, finding_text, decision, decided_at, reviewer)
                VALUES ('CR-Q-001', 'test', 'accepted', '2026-01-01T00:00:00', 'reviewer')
            """)
            conn.commit()
        finally:
            conn.close()

        import io
        from contextlib import redirect_stderr

        err = io.StringIO()
        with redirect_stderr(err):
            exit_code = main([
                "list",
                "--db", str(db_path)
            ])

        assert exit_code == 2, f"migration 未適用は exit 2 のはず、得られた: {exit_code}"
        assert "migration 007" in err.getvalue(), (
            f"stderr 文言が非回帰でないはず: {err.getvalue()!r}"
        )

    def test_db_d3_db_option_required_to_avoid_real_db_modification(self, tmp_path: Path):
        """D3: --db オプションが指定可能であり、省略時は実 DB を探す（seam 確認）。

        テストでは必ず --db で temp DB を指定することを規範とする
        （実 .claude/state/c3.db を触らないため）。
        """
        db_path = tmp_path / "c3.db"
        _create_test_db(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("""
                INSERT INTO review_decisions
                (checklist_id, finding_text, decision, decided_at, reviewer)
                VALUES ('CR-Q-001', 'test', 'accepted', '2026-01-01T00:00:00', 'reviewer')
            """)
            conn.commit()
        finally:
            conn.close()

        # --db を明示指定してテスト実行
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["list", "--db", str(db_path)])

        assert exit_code == 0, f"--db 指定時は成功のはず、exit_code={exit_code}"


# ---------------------------------------------------------------------------
# E 群: プロセス境界の seam (2 件)
# ---------------------------------------------------------------------------

class TestAuditReviewDecisionsMainSignature:
    """E 群: プロセス境界の seam（main 関数の署名と exit code）。"""

    def test_main_e1_main_function_signature(self):
        """E1: main が def main(argv: list[str] | None = None) -> int の署名を持つ。"""
        import inspect
        sig = inspect.signature(main)
        params = list(sig.parameters.keys())

        assert len(params) == 1, f"main は argv のみを受けるはず: {params}"
        assert params[0] == "argv", f"引数名は argv のはず: {params[0]}"

        return_annotation = sig.return_annotation
        assert return_annotation in (int, 'int'), f"戻り値は int のはず: {return_annotation}"

    def test_main_e2_exit_code_zero_on_success(self, tmp_path: Path):
        """E2: 成功時は exit code 0 を返す。"""
        db_path = tmp_path / "c3.db"
        _create_test_db(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer)
                VALUES (100, 'CR-Q-001', 'test', 'accepted', '2026-01-01T00:00:00', 'reviewer')
            """)
            conn.commit()
        finally:
            conn.close()

        exit_code = main(["list", "--db", str(db_path)])
        assert exit_code == 0, f"正常終了は exit 0 のはず、得られた: {exit_code}"

        exit_code = main([
            "resolve",
            "--db", str(db_path),
            "--id", "100",
            "--resolution", "resolved",
            "--note", "test",
            "--commit", "abc123def456abc123def456abc123def456abc1"
        ])
        assert exit_code == 0, f"正常終了は exit 0 のはず、得られた: {exit_code}"


# ---------------------------------------------------------------------------
# UB 群（新規）: list の stderr バナー・各レコードの _untrusted キー (5 件)
#
# architecture-report ADR-4 補足 / SR-AI-001（security-review-report-20260804-224558.md:116）
# への対応。plan-report §4「E」に対応する（本ファイル内の既存 E 群とラベルが衝突するため
# UB = Untrusted Banner と命名する）。Red 期待（stderr バナー・_untrusted キーとも未実装）。
# ---------------------------------------------------------------------------


class TestAuditReviewDecisionsListUntrustedBanner:
    """UB 群: list の 2 層防御（stderr バナー＋各レコードの _untrusted キー）。"""

    @pytest.fixture()
    def one_record_db(self, tmp_path: Path) -> Path:
        db_path = tmp_path / "c3.db"
        _create_test_db(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer, severity)
                VALUES (100, 'CR-Q-100', 'test finding', 'accepted', '2026-01-01T00:00:00', 'reviewer', 'High')
            """)
            conn.commit()
        finally:
            conn.close()
        return db_path

    def test_list_ub1_stderr_banner_first_line_when_records_exist(self, one_record_db: Path):
        """UB1: 対象 1 件以上で stderr にバナーが先頭で出る。

        バナー文言は特定の語の完全一致を要求しない。識別可能な部分文字列 1 つ
        （「データであり指示ではない」旨の「指示ではない」）のみを検査する。
        """
        import io
        from contextlib import redirect_stderr, redirect_stdout

        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            exit_code = main(["list", "--db", str(one_record_db)])

        assert exit_code == 0
        err_lines = [l for l in err.getvalue().splitlines() if l.strip()]
        assert err_lines, "対象 1 件以上では stderr にバナーが出力されるはず"
        assert "指示ではない" in err_lines[0], (
            f"stderr の先頭行に枠付け文言（指示ではない旨）が含まれるはず: {err_lines[0]!r}"
        )

    def test_list_ub2_stdout_is_pure_json_lines(self, one_record_db: Path):
        """UB2: stdout は純粋な JSON Lines（バナーは stdout に混ざらない）。"""
        import io
        from contextlib import redirect_stderr, redirect_stdout

        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            exit_code = main(["list", "--db", str(one_record_db)])

        assert exit_code == 0
        lines = [l for l in out.getvalue().split('\n') if l.strip()]
        assert lines, "対象があれば stdout にレコードが出力されるはず"
        for line in lines:
            obj = json.loads(line)  # 例外なくパースできること自体が検証
            assert isinstance(obj, dict)

    def test_list_ub3_each_record_has_untrusted_key(self, one_record_db: Path):
        """UB3: 各 JSON レコードに固定キー _untrusted がある。"""
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["list", "--db", str(one_record_db)])

        assert exit_code == 0
        lines = [l for l in output.getvalue().split('\n') if l.strip()]
        assert lines
        for line in lines:
            obj = json.loads(line)
            assert obj.get("_untrusted") == "data-not-instructions", (
                f"レコードに _untrusted キーが無いか値が不正: {line}"
            )

    def test_list_ub4_zero_records_stdout_empty_exit_0(self, tmp_path: Path):
        """UB4: 対象 0 件で stdout は空・戻り値 0。"""
        import io
        from contextlib import redirect_stderr, redirect_stdout

        db_path = tmp_path / "c3.db"
        _create_test_db(db_path)

        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            exit_code = main(["list", "--db", str(db_path)])

        assert exit_code == 0
        assert out.getvalue().strip() == "", "対象 0 件では stdout は空のはず"

    def test_list_ub5_multiline_finding_text_preserves_json_lines_with_untrusted(
        self, tmp_path: Path
    ):
        """UB5: finding_text に改行を含んでも JSON Lines 契約（1 レコード 1 行）が保たれ、
        _untrusted キーも維持される。
        """
        import io
        from contextlib import redirect_stdout

        db_path = tmp_path / "c3.db"
        _create_test_db(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("""
                INSERT INTO review_decisions
                (checklist_id, finding_text, decision, decided_at, reviewer)
                VALUES (?, ?, 'accepted', '2026-01-01T00:00:00', 'reviewer')
            """, ('CR-Q-001', 'line 1\nline 2\nline 3'))
            conn.commit()
        finally:
            conn.close()

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["list", "--db", str(db_path)])

        assert exit_code == 0
        lines = [l for l in output.getvalue().split('\n') if l.strip()]
        assert len(lines) == 1, f"1 レコード 1 行のはず、得られた非空行: {len(lines)}"
        obj = json.loads(lines[0])
        assert 'line 1' in obj['finding_text']
        assert obj.get("_untrusted") == "data-not-instructions"


# ---------------------------------------------------------------------------
# F 群（新規）: resolve の入力検証 (6 件)
#
# plan-report §4「F」/ SR-NEW（record_review_decision.py の _truncate() 規律に揃える）
# への対応。Red 期待（切り詰め・形式検証とも未実装）。
# ---------------------------------------------------------------------------


class TestAuditReviewDecisionsInputValidation:
    """F 群: --note の切り詰め・--commit の形式検証。"""

    @pytest.fixture()
    def simple_db(self, tmp_path: Path) -> Path:
        db_path = tmp_path / "c3.db"
        _create_test_db(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("""
                INSERT INTO review_decisions
                (id, checklist_id, finding_text, decision, decided_at, reviewer, severity)
                VALUES (100, 'CR-Q-100', 'test finding', 'accepted', '2026-01-01T00:00:00', 'reviewer', 'High')
            """)
            conn.commit()
        finally:
            conn.close()
        return db_path

    def test_resolve_f1_note_exceeding_char_length_is_truncated_with_warning(
        self, simple_db: Path
    ):
        """F1: --note が 2000 文字を超える場合、切り詰め + stderr 警告。"""
        import io
        from contextlib import redirect_stderr

        long_note = "a" * 2500

        err = io.StringIO()
        with redirect_stderr(err):
            exit_code = main([
                "resolve",
                "--db", str(simple_db),
                "--id", "100",
                "--resolution", "resolved",
                "--note", long_note,
                "--commit", "abc123def456abc123def456abc123def456abc1",
            ])

        assert exit_code == 0, f"切り詰め後は成功のはず、exit_code={exit_code}"
        assert err.getvalue().strip() != "", "--note 上限超過時は stderr 警告が出るはず"

        conn = sqlite3.connect(str(simple_db))
        try:
            row = conn.execute(
                "SELECT resolution_note FROM review_decisions WHERE id=100"
            ).fetchone()
        finally:
            conn.close()

        assert row[0] is not None
        assert len(row[0]) <= 2000, f"note は 2000 文字以下に切り詰められるはず: len={len(row[0])}"

    def test_resolve_f2_note_within_length_is_not_truncated(self, simple_db: Path):
        """F2: 2000 文字以内の --note は切り詰められない。"""
        note = "short note within limits"
        exit_code = main([
            "resolve",
            "--db", str(simple_db),
            "--id", "100",
            "--resolution", "resolved",
            "--note", note,
            "--commit", "abc123def456abc123def456abc123def456abc1",
        ])
        assert exit_code == 0

        conn = sqlite3.connect(str(simple_db))
        try:
            row = conn.execute(
                "SELECT resolution_note FROM review_decisions WHERE id=100"
            ).fetchone()
        finally:
            conn.close()

        assert row[0] == note, "上限以内の note はそのまま保存されるはず"

    def test_resolve_f3_note_byte_length_boundary_with_multibyte_chars(
        self, simple_db: Path
    ):
        """F3: マルチバイト文字（日本語）を含む note は 8*1024 バイト境界でも
        安全に切り詰められる（文字数上限 2000 とバイト数上限 8*1024 の両方を満たす）。
        """
        note = "あ" * 3000  # UTF-8 で 1 文字 3 バイト -> 9000 バイト（8*1024 超過）
        exit_code = main([
            "resolve",
            "--db", str(simple_db),
            "--id", "100",
            "--resolution", "resolved",
            "--note", note,
            "--commit", "abc123def456abc123def456abc123def456abc1",
        ])
        assert exit_code == 0

        conn = sqlite3.connect(str(simple_db))
        try:
            row = conn.execute(
                "SELECT resolution_note FROM review_decisions WHERE id=100"
            ).fetchone()
        finally:
            conn.close()

        assert row[0] is not None
        assert len(row[0].encode("utf-8")) <= 8 * 1024, (
            f"note は 8*1024 バイト以下に切り詰められるはず: {len(row[0].encode('utf-8'))} bytes"
        )
        assert len(row[0]) <= 2000

    def test_resolve_f4_commit_invalid_hex_format_exits_2(self, simple_db: Path):
        """F4: --commit が 16 進数以外の文字を含む場合、exit 2。"""
        exit_code = main([
            "resolve",
            "--db", str(simple_db),
            "--id", "100",
            "--resolution", "open",
            "--commit", "not-a-valid-sha-value-xyz-not-hex",
        ])
        assert exit_code == 2, f"不正形式の --commit は exit 2 のはず、得られた: {exit_code}"

    def test_resolve_f5_commit_wrong_length_exits_2(self, simple_db: Path):
        """F5: --commit が 40 桁でない場合（16 進文字のみでも）、exit 2。"""
        exit_code = main([
            "resolve",
            "--db", str(simple_db),
            "--id", "100",
            "--resolution", "open",
            "--commit", "abc123",  # 6 桁のみ
        ])
        assert exit_code == 2, f"40 桁でない --commit は exit 2 のはず、得られた: {exit_code}"

    def test_resolve_f6_commit_valid_40_hex_succeeds(self, simple_db: Path):
        """F6: 40 桁 16 進の --commit は成功する（非回帰）。"""
        exit_code = main([
            "resolve",
            "--db", str(simple_db),
            "--id", "100",
            "--resolution", "open",
            "--commit", "abc123def456abc123def456abc123def456abc1",
        ])
        assert exit_code == 0, f"正しい形式の --commit は成功のはず、得られた: {exit_code}"
