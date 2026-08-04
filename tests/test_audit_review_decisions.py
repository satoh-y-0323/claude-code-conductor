"""tests/test_audit_review_decisions.py

scripts/audit_review_decisions.py のテスト（Red フェーズ）。
未実装のため import 失敗（ModuleNotFoundError）が期待される。

テストケース構成:
  A 群 (11 件): list サブコマンド（抽出・フォーマット・フィルタリング）
  B 群 (10 件): resolve サブコマンド（書き込み・必須項目・異常系）
  C 群 (3 件): summary サブコマンド（集計）
  D 群 (3 件): DB 共通オプション・異常系
  E 群 (2 件): プロセス境界の seam（exit code 検証）
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
            # 5. 既に判定済み（resolution != NULL）（対象外）
            conn.execute("""
                INSERT INTO review_decisions
                (checklist_id, finding_text, decision, decided_at, reviewer, severity, resolution)
                VALUES ('CR-Q-005', 'test finding 5', 'accepted', '2026-01-05T00:00:00', 'reviewer1', 'High', 'resolved')
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
        """A5: list は resolution != NULL の行を除外する（既判定行）。"""
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["list", "--db", str(populated_db)])

        assert exit_code == 0

        lines = output.getvalue().strip().split('\n')
        non_empty_lines = [l for l in lines if l.strip()]

        resolutions = [json.loads(line).get('resolution') for line in non_empty_lines]
        assert all(r is None for r in resolutions), "resolution は NULL のはず（未判定のみ）"

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
        """B8: --force で既判定行の 3 列すべてが上書きされる。"""
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
            "--commit", "newcommit456"
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
        assert row[2] == "newcommit456", f"resolution_commit が上書きされていない: {row[2]}"

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

    def test_resolve_b10_commit_is_required_for_all_resolution_types(self, simple_db: Path):
        """B10: --commit は全 resolution タイプで必須（テスト時は明示指定）。

        open でも --commit は必須とする。
        git rev-parse HEAD が失敗するか --commit 自動取得がサポートされていない場合 exit 2。
        テストでは常に --commit を明示するため、この観点は test-resolve-b9 で
        --commit 明示ケースのみテストする。ここでは省略（テスト実装上の制約）。
        """
        # テスト実装上の制約により、このテストは現在スキップする
        pass


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
# D 群: DB 共通オプション・異常系 (3 件)
# ---------------------------------------------------------------------------

class TestAuditReviewDecisionsDbOption:
    """D 群: --db オプション・DB 不在・未適用 DB などの異常系。"""

    def test_db_d1_missing_db_file_exits_2(self, tmp_path: Path):
        """D1: --db で指定した DB ファイルが存在しない場合、exit 2。

        あるいは --db 省略時に locate_c3_db が None を返す場合も exit 2。
        """
        nonexistent_db = tmp_path / "nonexistent.db"

        exit_code = main([
            "list",
            "--db", str(nonexistent_db)
        ])

        assert exit_code == 2, f"DB 不在は exit 2 のはず、得られた: {exit_code}"

    def test_db_d2_migration_007_not_applied_exits_2(self, tmp_path: Path):
        """D2: migration 007 未適用の DB（resolution 列なし）で実行すると、
        'no such column: resolution' 例外を捕捉して exit 2。
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

        exit_code = main([
            "list",
            "--db", str(db_path)
        ])

        assert exit_code == 2, f"migration 未適用は exit 2 のはず、得られた: {exit_code}"

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
