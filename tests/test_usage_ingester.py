"""tests/test_usage_ingester.py

v2.21.0 usage_ingester / db ヘルパー統合テスト（12 件）。

リアル DB（apply_pending_migrations で 001+002 適用）に対して検証する。
フィクスチャは tmp_path にコピーまたは動的構築する。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from c3.migrate import apply_pending_migrations
from c3.usage_ingester import ingest_session
from c3.db import get_ingest_offset


# ---------------------------------------------------------------------------
# ヘルパー / フィクスチャ
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "usage"

# テスト用の架空 UUID
FAKE_SESSION_ID = "aaaabbbb-cccc-dddd-eeee-ffffffffffff"
AGENT_ID_DEADBEEF = "agent-deadbeef"


def _make_db(tmp_path: Path) -> Path:
    """tmp_path に c3.db を作成して 001+002 migration を適用する。"""
    db_path = tmp_path / "c3.db"
    apply_pending_migrations(db_path)
    return db_path


def _make_project_dir(tmp_path: Path) -> Path:
    """tmp_path 内に project_dir を作成して返す。"""
    project_dir = tmp_path / "projects" / "test-slug"
    project_dir.mkdir(parents=True)
    return project_dir


def _copy_mainline(project_dir: Path, session_id: str = FAKE_SESSION_ID) -> Path:
    """フィクスチャの mainline.jsonl を project_dir/<session_id>.jsonl にコピー。"""
    src = FIXTURES_DIR / "mainline.jsonl"
    dst = project_dir / f"{session_id}.jsonl"
    shutil.copy(src, dst)
    return dst


def _copy_subagent(
    project_dir: Path,
    session_id: str = FAKE_SESSION_ID,
    agent_id: str = AGENT_ID_DEADBEEF,
) -> tuple[Path, Path]:
    """フィクスチャの subagent jsonl + meta.json を project_dir/<session>/subagents/ にコピー。"""
    subagents_dir = project_dir / session_id / "subagents"
    subagents_dir.mkdir(parents=True)
    jsonl_src = FIXTURES_DIR / "subagents" / f"{agent_id}.jsonl"
    meta_src = FIXTURES_DIR / "subagents" / f"{agent_id}.meta.json"
    jsonl_dst = subagents_dir / f"{agent_id}.jsonl"
    meta_dst = subagents_dir / f"{agent_id}.meta.json"
    shutil.copy(jsonl_src, jsonl_dst)
    shutil.copy(meta_src, meta_dst)
    return jsonl_dst, meta_dst


def _read_all_cost_runs(db_path: Path) -> list[dict]:
    """agent_cost_runs の全行を dict のリストで返す。"""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM agent_cost_runs ORDER BY agent_id, model"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# テストケース
# ---------------------------------------------------------------------------


class TestIngestSubagent:
    """T1: subagent jsonl 取り込み → meta.json の agentType で agent_cost_runs に行が入る"""

    def test_subagent_ingest_uses_meta_agent_type(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        db = _make_db(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        _copy_subagent(project_dir)

        monkeypatch.setenv("C3_DB_PATH", str(db))
        result = ingest_session(
            session_id=FAKE_SESSION_ID,
            project_dir=project_dir,
            db_path=db,
        )

        rows = _read_all_cost_runs(db)
        # subagent の agent_id が "agent-deadbeef" で入る
        subagent_rows = [r for r in rows if r["agent_id"] == AGENT_ID_DEADBEEF]
        assert len(subagent_rows) >= 1, f"subagent 行がない: {rows}"
        # meta.json の agentType='developer' が使われる
        assert subagent_rows[0]["agent_type"] == "developer", (
            f"agent_type が developer でない: {subagent_rows[0]['agent_type']}"
        )
        assert result.runs_upserted >= 1


class TestIngestMainline:
    """T2: mainline jsonl → agent_type='mainline', agent_id='mainline'"""

    def test_mainline_ingest_uses_mainline_sentinel(
        self, tmp_path: Path
    ):
        db = _make_db(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        _copy_mainline(project_dir)

        result = ingest_session(
            session_id=FAKE_SESSION_ID,
            project_dir=project_dir,
            db_path=db,
        )

        rows = _read_all_cost_runs(db)
        mainline_rows = [r for r in rows if r["agent_id"] == "mainline"]
        assert len(mainline_rows) >= 1, f"mainline 行がない: {rows}"
        assert all(r["agent_type"] == "mainline" for r in mainline_rows), (
            f"agent_type が mainline でない行がある: {mainline_rows}"
        )
        assert result.runs_upserted >= 1


class TestModelAggregation:
    """T3: 同一 model の複数 requestId が合算される"""

    def test_multiple_requests_same_model_are_summed(
        self, tmp_path: Path
    ):
        db = _make_db(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        # mainline.jsonl には opus モデルで 2 行ある (req-0001, req-0002)
        _copy_mainline(project_dir)

        ingest_session(
            session_id=FAKE_SESSION_ID,
            project_dir=project_dir,
            db_path=db,
        )

        rows = _read_all_cost_runs(db)
        opus_rows = [r for r in rows if "opus" in r["model"] and r["agent_id"] == "mainline"]
        assert len(opus_rows) == 1, (
            f"同一モデルが複数行になっている: {opus_rows}"
        )
        # input_tokens: req-0001(100) + req-0002(150) = 250
        assert opus_rows[0]["input_tokens"] == 250, (
            f"input_tokens の合算が誤り: {opus_rows[0]['input_tokens']}"
        )


class TestMultipleModels:
    """T4: 複数 model が別行に分離される（PK に model 含む）"""

    def test_different_models_create_separate_rows(
        self, tmp_path: Path
    ):
        db = _make_db(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        # mainline.jsonl には opus (req-0001, req-0002) と haiku (req-0003) の 2 モデル
        _copy_mainline(project_dir)

        ingest_session(
            session_id=FAKE_SESSION_ID,
            project_dir=project_dir,
            db_path=db,
        )

        rows = _read_all_cost_runs(db)
        mainline_rows = [r for r in rows if r["agent_id"] == "mainline"]
        models = {r["model"] for r in mainline_rows}
        assert len(models) == 2, f"モデル数が 2 でない: {models}"
        assert any("opus" in m for m in models), "opus モデルがない"
        assert any("haiku" in m for m in models), "haiku モデルがない"


class TestIncrementalIngest:
    """T5: 増分 — 2 回目 ingest は offset 以降のみ処理、行重複なし（冪等）"""

    def test_incremental_ingest_no_duplication(
        self, tmp_path: Path
    ):
        db = _make_db(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        _copy_mainline(project_dir)

        # 1 回目
        ingest_session(
            session_id=FAKE_SESSION_ID,
            project_dir=project_dir,
            db_path=db,
        )
        rows_after_first = _read_all_cost_runs(db)

        # 2 回目（同じファイルを再度 ingest）
        ingest_session(
            session_id=FAKE_SESSION_ID,
            project_dir=project_dir,
            db_path=db,
        )
        rows_after_second = _read_all_cost_runs(db)

        # 行数は変わらない（PK upsert + offset で冪等）
        assert len(rows_after_first) == len(rows_after_second), (
            f"2 回目で行が増えた: {len(rows_after_first)} -> {len(rows_after_second)}"
        )
        # 2 回目は offset が最終行に達しているので新規 upsert は 0（再実行でも同値になる）
        # runs_upserted は upsert を呼んだ回数なので 0 以上だが行増加なし
        assert len(rows_after_second) >= 1


class TestParseErrorOffsetNotUpdated:
    """T6: parse error 行で中断 → offset 据え置き → 次回リトライ可"""

    def test_parse_error_preserves_offset(
        self, tmp_path: Path
    ):
        db = _make_db(tmp_path)
        project_dir = _make_project_dir(tmp_path)

        # 正常行 1 行 + 不正 JSON 1 行 のファイルを作成
        jsonl_path = project_dir / f"{FAKE_SESSION_ID}.jsonl"
        jsonl_path.write_text(
            '{"type":"user","message":{"role":"user","content":"hi"}}\n'
            "NOT VALID JSON LINE\n",
            encoding="utf-8",
        )

        ingest_session(
            session_id=FAKE_SESSION_ID,
            project_dir=project_dir,
            db_path=db,
        )

        # offset が 0 のまま（parse error で据え置き）
        file_key = f"{FAKE_SESSION_ID}:mainline"
        offset = get_ingest_offset(file_key, db_path=db)
        assert offset == 0, f"parse error 後に offset が更新されてしまった: {offset}"


class TestBrokenEndNoException:
    """T7: 末尾破損行で例外伝播せず（result に error 名）"""

    def test_broken_end_no_exception_propagation(
        self, tmp_path: Path
    ):
        db = _make_db(tmp_path)
        project_dir = _make_project_dir(tmp_path)

        # 末尾が不完全な JSON（切れた行）
        jsonl_path = project_dir / f"{FAKE_SESSION_ID}.jsonl"
        jsonl_path.write_text(
            '{"type":"assistant","isSidechain":false,"message":{"role":"assistant",'
            '"model":"claude-opus-4-7-20260101","usage":{"input_tokens":10,'
            '"output_tokens":5,"cache_read_input_tokens":0,'
            '"cache_creation_input_tokens":0}}}\n'
            '{"type":"assistant","broken":true',  # 末尾切れ
            encoding="utf-8",
        )

        # 例外が伝播しないこと
        result = ingest_session(
            session_id=FAKE_SESSION_ID,
            project_dir=project_dir,
            db_path=db,
        )
        # エラー名が result に入っている
        assert len(result.errors) > 0, "末尾破損なのに errors が空"


class TestMetaMissingUnknown:
    """T8: meta 欠損 → agent_type='unknown'"""

    def test_missing_meta_results_in_unknown_agent_type(
        self, tmp_path: Path
    ):
        db = _make_db(tmp_path)
        project_dir = _make_project_dir(tmp_path)

        # subagent jsonl のみ作成（meta.json なし）
        subagents_dir = project_dir / FAKE_SESSION_ID / "subagents"
        subagents_dir.mkdir(parents=True)
        jsonl_path = subagents_dir / "agent-deadbeef.jsonl"
        jsonl_path.write_text(
            '{"type":"assistant","isSidechain":true,"message":{"role":"assistant",'
            '"model":"claude-sonnet-4-6-20260101","usage":{"input_tokens":50,'
            '"output_tokens":25,"cache_read_input_tokens":0,'
            '"cache_creation_input_tokens":0}}}\n',
            encoding="utf-8",
        )

        ingest_session(
            session_id=FAKE_SESSION_ID,
            project_dir=project_dir,
            db_path=db,
        )

        rows = _read_all_cost_runs(db)
        subagent_rows = [r for r in rows if r["agent_id"] == "agent-deadbeef"]
        assert len(subagent_rows) >= 1, f"subagent 行がない: {rows}"
        assert subagent_rows[0]["agent_type"] == "unknown", (
            f"meta 欠損時に agent_type='unknown' でない: {subagent_rows[0]['agent_type']}"
        )


class TestUnknownModelCostZero:
    """T9: 不明モデル → total_cost_usd=0.0 で記録される（非例外）"""

    def test_unknown_model_stored_with_zero_cost(
        self, tmp_path: Path
    ):
        db = _make_db(tmp_path)
        project_dir = _make_project_dir(tmp_path)

        jsonl_path = project_dir / f"{FAKE_SESSION_ID}.jsonl"
        jsonl_path.write_text(
            '{"type":"assistant","isSidechain":false,"message":{"role":"assistant",'
            '"model":"unknown-model-xyz-9999","usage":{"input_tokens":100,'
            '"output_tokens":50,"cache_read_input_tokens":0,'
            '"cache_creation_input_tokens":0}}}\n',
            encoding="utf-8",
        )

        result = ingest_session(
            session_id=FAKE_SESSION_ID,
            project_dir=project_dir,
            db_path=db,
        )

        assert result.runs_upserted >= 1, "不明モデルで upsert が行われていない"
        rows = _read_all_cost_runs(db)
        unknown_rows = [r for r in rows if r["model"] == "unknown-model-xyz-9999"]
        assert len(unknown_rows) == 1, f"不明モデルの行がない: {rows}"
        assert unknown_rows[0]["total_cost_usd"] == 0.0, (
            f"不明モデルのコストが 0 でない: {unknown_rows[0]['total_cost_usd']}"
        )


class TestInvalidSessionIdNoop:
    """T10: 非 UUID session_id → no-op（空 result、書き込みなし）"""

    def test_invalid_session_id_is_noop(
        self, tmp_path: Path
    ):
        db = _make_db(tmp_path)
        project_dir = _make_project_dir(tmp_path)

        result = ingest_session(
            session_id="../../etc/passwd",
            project_dir=project_dir,
            db_path=db,
        )

        assert result.skipped_invalid_session is True
        assert result.runs_upserted == 0
        rows = _read_all_cost_runs(db)
        assert len(rows) == 0, "traversal が成立してしまった"


class TestSymlinkSkipped:
    """T11: symlink の jsonl はスキップ"""

    def test_symlink_jsonl_is_skipped(
        self, tmp_path: Path
    ):
        db = _make_db(tmp_path)
        project_dir = _make_project_dir(tmp_path)

        # 正規ファイルを別の場所に作成し、project_dir に symlink を置く
        real_file = tmp_path / "real_mainline.jsonl"
        real_file.write_text(
            '{"type":"assistant","isSidechain":false,"message":{"role":"assistant",'
            '"model":"claude-opus-4-7-20260101","usage":{"input_tokens":10,'
            '"output_tokens":5,"cache_read_input_tokens":0,'
            '"cache_creation_input_tokens":0}}}\n',
            encoding="utf-8",
        )
        symlink_path = project_dir / f"{FAKE_SESSION_ID}.jsonl"
        try:
            symlink_path.symlink_to(real_file)
        except OSError:
            pytest.skip("symlink 作成不可（OS または権限の制限）")

        ingest_session(
            session_id=FAKE_SESSION_ID,
            project_dir=project_dir,
            db_path=db,
        )

        rows = _read_all_cost_runs(db)
        assert len(rows) == 0, f"symlink がスキップされず書き込まれた: {rows}"


class TestInvalidSessionIdStrictUUID:
    """T13: SR M-1 — 非 UUID 構造の session_id は skipped_invalid_session=True で弾かれる"""

    def test_hyphen_only_session_id_is_rejected(self, tmp_path: Path):
        """ハイフン 36 個（旧 RE は通過していた）は弾かれる。"""
        db = _make_db(tmp_path)
        project_dir = _make_project_dir(tmp_path)

        result = ingest_session(
            session_id="------------------------------------",  # ハイフン 36 個
            project_dir=project_dir,
            db_path=db,
        )

        assert result.skipped_invalid_session is True, (
            "ハイフン 36 個が UUID として受け入れられてしまった"
        )
        assert result.runs_upserted == 0

    def test_non_uuid_structure_is_rejected(self, tmp_path: Path):
        """UUID 8-4-4-4-12 構造でない文字列（長さは 36 だが構造違反）は弾かれる。"""
        db = _make_db(tmp_path)
        project_dir = _make_project_dir(tmp_path)

        # 長さ 36 だが "8-4-4-4-12" 構造になっていない（ハイフン位置が違う）
        result = ingest_session(
            session_id="aaaaaaaabbbbccccddddeeeeffffffffff00",  # ハイフンなし・36 文字
            project_dir=project_dir,
            db_path=db,
        )

        assert result.skipped_invalid_session is True, (
            "非 UUID 構造文字列が受け入れられてしまった"
        )
        assert result.runs_upserted == 0


class TestDescriptionTruncation:
    """T14: SR M-2 — 512 文字超の description が切り捨てられて DB に入る"""

    def test_long_description_is_truncated_to_512(self, tmp_path: Path):
        """513 文字の description が 512 文字に切り捨てられることを確認。"""
        db = _make_db(tmp_path)
        project_dir = _make_project_dir(tmp_path)

        # meta.json に 513 文字の description を持つ subagent を構築
        subagents_dir = project_dir / FAKE_SESSION_ID / "subagents"
        subagents_dir.mkdir(parents=True)
        long_description = "x" * 513  # 513 文字
        meta = {"agentType": "developer", "description": long_description}
        (subagents_dir / "agent-deadbeef.meta.json").write_text(
            json.dumps(meta), encoding="utf-8"
        )
        (subagents_dir / "agent-deadbeef.jsonl").write_text(
            '{"type":"assistant","isSidechain":true,"message":{"role":"assistant",'
            '"model":"claude-sonnet-4-6-20260101","usage":{"input_tokens":10,'
            '"output_tokens":5,"cache_read_input_tokens":0,'
            '"cache_creation_input_tokens":0}}}\n',
            encoding="utf-8",
        )

        ingest_session(
            session_id=FAKE_SESSION_ID,
            project_dir=project_dir,
            db_path=db,
        )

        rows = _read_all_cost_runs(db)
        subagent_rows = [r for r in rows if r["agent_id"] == "agent-deadbeef"]
        assert len(subagent_rows) >= 1, f"subagent 行がない: {rows}"
        stored_description = subagent_rows[0]["description"]
        assert stored_description is not None, "description が None になっている"
        assert len(stored_description) == 512, (
            f"description が 512 文字に切り捨てられていない: len={len(stored_description)}"
        )


class TestNonAssistantIgnored:
    """T12: type != "assistant" レコードは無視される"""

    def test_non_assistant_records_are_ignored(
        self, tmp_path: Path
    ):
        db = _make_db(tmp_path)
        project_dir = _make_project_dir(tmp_path)

        jsonl_path = project_dir / f"{FAKE_SESSION_ID}.jsonl"
        jsonl_path.write_text(
            # user レコード（無視されるべき）
            '{"type":"user","isSidechain":false,"message":{"role":"user","content":"hi"}}\n'
            # system レコード（無視されるべき）
            '{"type":"system","isSidechain":false,"content":"system message"}\n'
            # assistant レコード（処理されるべき）
            '{"type":"assistant","isSidechain":false,"message":{"role":"assistant",'
            '"model":"claude-haiku-4-5-20260101","usage":{"input_tokens":20,'
            '"output_tokens":10,"cache_read_input_tokens":0,'
            '"cache_creation_input_tokens":0}}}\n',
            encoding="utf-8",
        )

        result = ingest_session(
            session_id=FAKE_SESSION_ID,
            project_dir=project_dir,
            db_path=db,
        )

        rows = _read_all_cost_runs(db)
        # assistant レコード 1 件のみ記録される
        assert len(rows) == 1, f"user/system レコードが記録されてしまった: {rows}"
        assert rows[0]["input_tokens"] == 20
        assert result.runs_upserted == 1


class TestSafeResolvedFile:
    """T15: _safe_resolved_file（SR-V-002 パス traversal 検証の中核）の直接単体テスト。

    _ingest_jsonl / _read_agent_meta の重複検証を集約したヘルパー。各分岐
    （実在ファイル / 非存在 / ディレクトリ / 範囲外 / symlink）を個別に固定する。
    """

    def test_valid_file_returns_resolved_path(self, tmp_path: Path):
        from c3.usage_ingester import _safe_resolved_file  # noqa: PLC0415

        root = tmp_path.resolve()
        f = root / "ok.jsonl"
        f.write_text("{}", encoding="utf-8")
        assert _safe_resolved_file(f, root, log_label="t") == f.resolve()

    def test_nonexistent_returns_none(self, tmp_path: Path):
        from c3.usage_ingester import _safe_resolved_file  # noqa: PLC0415

        root = tmp_path.resolve()
        assert _safe_resolved_file(root / "missing.jsonl", root, log_label="t") is None

    def test_directory_returns_none(self, tmp_path: Path):
        from c3.usage_ingester import _safe_resolved_file  # noqa: PLC0415

        root = tmp_path.resolve()
        # root 自身（ディレクトリ）は is_file=False のため None
        assert _safe_resolved_file(root, root, log_label="t") is None

    def test_outside_project_returns_none(self, tmp_path: Path):
        from c3.usage_ingester import _safe_resolved_file  # noqa: PLC0415

        root = (tmp_path / "proj").resolve()
        root.mkdir()
        outside = tmp_path / "outside.jsonl"
        outside.write_text("{}", encoding="utf-8")
        assert _safe_resolved_file(outside, root, log_label="t") is None

    def test_symlink_returns_none(self, tmp_path: Path):
        from c3.usage_ingester import _safe_resolved_file  # noqa: PLC0415

        root = tmp_path.resolve()
        real = tmp_path / "real.jsonl"
        real.write_text("{}", encoding="utf-8")
        link = root / "link.jsonl"
        try:
            link.symlink_to(real)
        except OSError:
            pytest.skip("symlink 作成不可（OS または権限の制限）")
        assert _safe_resolved_file(link, root, log_label="t") is None
