"""セッションログ (jsonl) を読み、モデル単価で USD 換算して c3.db に蓄積する ingester。

対象ファイル:
  - <project_dir>/<session_id>.jsonl (mainline)
  - <project_dir>/<session_id>/subagents/agent-*.jsonl (subagent)
  - <project_dir>/<session_id>/subagents/agent-*.meta.json (subagent メタ情報)

設計判断 (plan-report T2):
  - session_id を ^[0-9a-fA-F-]{36}$ で validate (traversal 防止)
  - jsonl/meta パスは .resolve() 後 is_relative_to(project_dir.resolve()) で配下検証
  - symlink はスキップ (SR-V-002)
  - 例外は type(exc).__name__ のみ (SR-R-001)
  - offset は行数ベース。parse error なく読み切れた時のみ更新 (冪等性の核)
  - mainline の agent_id sentinel は固定文字列 'mainline'
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from c3.db import (
    get_ingest_offset,
    insert_agent_cost_run,
    set_ingest_offset,
)
from c3.pricing import compute_cost_usd

logger = logging.getLogger(__name__)

# session_id の許容パターン: UUID 8-4-4-4-12 構造を強制する
# traversal 防止のため厳格に検証する（SR-V-002 対応）
_SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# DB 格納前の長さ制限（PII 永続化リスク低減 / SR-K-003 対応）
MAX_DESCRIPTION_LEN = 512
MAX_ATTRIBUTION_SKILL_LEN = 128

# jsonl 1 行あたりのバイト上限（DoS 防止 / SR-V-001 対応）
MAX_LINE_BYTES = 10 * 1024 * 1024  # 10 MB / line


@dataclass
class IngestResult:
    """ingest_session の結果を保持するデータクラス。

    Attributes:
        session_id: 処理対象のセッション ID。
        files_processed: 正常に処理した jsonl ファイル数。
        runs_upserted: insert_agent_cost_run を呼んだ回数（upsert 数）。
        errors: 発生したエラーの type 名リスト（SR-R-001 準拠: 型名のみ）。
        skipped_invalid_session: session_id 検証失敗で何もしなかった場合 True。
    """
    session_id: str
    files_processed: int = 0
    runs_upserted: int = 0
    errors: list[str] = field(default_factory=list)
    skipped_invalid_session: bool = False


def ingest_session(
    *,
    session_id: str,
    project_dir: Path,
    db_path: Path | None = None,
) -> IngestResult:
    """セッションログを走査し agent_cost_runs / usage_ingest_state を更新する。

    Args:
        session_id: セッション UUID 文字列。^[0-9a-fA-F-]{36}$ でない場合は no-op。
        project_dir: Claude Code の projects/<slug>/ ディレクトリ。
            mainline は <project_dir>/<session_id>.jsonl を探す。
            subagent は <project_dir>/<session_id>/subagents/ 以下を探す。
        db_path: c3.db のパス。省略時は locate_c3_db() で探索。

    Returns:
        IngestResult。例外を投げない。エラーは result.errors に type 名を格納する。
    """
    result = IngestResult(session_id=session_id)

    # session_id validate (traversal 防止)
    if not _SESSION_ID_RE.match(session_id):
        logger.debug("ingest_session: invalid session_id format (skipped)")
        result.skipped_invalid_session = True
        return result

    resolved_project = project_dir.resolve()

    # --- mainline jsonl ---
    mainline_path = project_dir / f"{session_id}.jsonl"
    _ingest_jsonl(
        jsonl_path=mainline_path,
        session_id=session_id,
        agent_id="mainline",
        is_sidechain=False,
        project_dir=resolved_project,
        result=result,
        db_path=db_path,
    )

    # --- subagent jsonl ---
    subagents_dir = project_dir / session_id / "subagents"
    if subagents_dir.is_dir() and not subagents_dir.is_symlink():
        try:
            for jsonl_path in sorted(subagents_dir.glob("agent-*.jsonl")):
                # symlink はスキップ
                if jsonl_path.is_symlink():
                    logger.debug("ingest_session: symlink skipped")
                    continue

                # agent_id は "agent-<id>" 部分 (拡張子除く stem)
                agent_id = jsonl_path.stem  # 例: "agent-deadbeef"

                _ingest_jsonl(
                    jsonl_path=jsonl_path,
                    session_id=session_id,
                    agent_id=agent_id,
                    is_sidechain=True,
                    project_dir=resolved_project,
                    result=result,
                    db_path=db_path,
                )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(type(exc).__name__)
            logger.debug("ingest_session: subagents scan error: %s", type(exc).__name__)

    return result


def _safe_resolved_file(
    path: Path, project_dir: Path, *, log_label: str
) -> Path | None:
    """symlink でない実在ファイルを resolve し、project_dir 配下なら resolved Path を返す。

    検証に失敗したら None を返す（symlink / 非ファイル / project_dir 外）。symlink・
    範囲外は debug ログを出す。``is_symlink()`` / ``is_file()`` / ``resolve()`` の
    例外（マウント切れ等の稀な OSError）は送出し、呼び出し側の try で扱う
    （_ingest_jsonl は result.errors に記録、_read_agent_meta は debug ログ）。
    SR-V-002 のパス traversal 対策を 1 箇所に集約する。
    """
    if path.is_symlink():
        logger.debug("%s: symlink skipped", log_label)
        return None
    if not path.is_file():
        return None
    resolved = path.resolve()
    if not resolved.is_relative_to(project_dir):
        logger.debug("%s: path outside project_dir, skipped", log_label)
        return None
    return resolved


def _ingest_jsonl(
    *,
    jsonl_path: Path,
    session_id: str,
    agent_id: str,
    is_sidechain: bool,
    project_dir: Path,
    result: IngestResult,
    db_path: Path | None,
) -> None:
    """1 つの jsonl ファイルを ingest する（内部関数）。

    - 存在しない / symlink / project_dir 外のパスはスキップ
    - parse error なく読み切れた時のみ offset を更新（冪等性保証）
    """
    # symlink でない project_dir 配下の実在ファイルか検証し、resolved パスを得る。
    # resolve() の例外のみ errors に記録する（symlink/非ファイル/範囲外は静かにスキップ）。
    try:
        resolved_jsonl = _safe_resolved_file(
            jsonl_path, project_dir, log_label="_ingest_jsonl"
        )
    except Exception as exc:  # noqa: BLE001
        result.errors.append(type(exc).__name__)
        return
    if resolved_jsonl is None:
        return

    file_key = f"{session_id}:{agent_id}"
    start_offset = get_ingest_offset(file_key, db_path=db_path)

    # subagent の場合 meta.json から agentType / description を取得
    agent_type = "mainline" if not is_sidechain else "unknown"
    description: str | None = None
    attribution_skill: str | None = None

    if is_sidechain:
        meta_path = jsonl_path.with_suffix(".meta.json")
        try:
            meta_agent_type, meta_description = _read_agent_meta(
                meta_path, project_dir=project_dir
            )
            if meta_agent_type is not None:
                agent_type = meta_agent_type
            # description を長さ制限して格納（PII 永続化リスク低減 / SR-K-003）
            description = (meta_description or "")[:MAX_DESCRIPTION_LEN] or None
        except Exception as exc:  # noqa: BLE001
            result.errors.append(type(exc).__name__)
            logger.debug("_ingest_jsonl: meta read error: %s", type(exc).__name__)

    # jsonl を走査してモデル別トークン集計
    # key: model -> {"input_tokens": int, "output_tokens": int,
    #                "cache_read_tokens": int, "cache_create_tokens": int,
    #                "attribution_skill": str | None}
    model_accum: dict[str, dict] = {}
    parse_ok = False

    try:
        new_offset = _accumulate_usage(
            jsonl_path=resolved_jsonl,
            start_offset=start_offset,
            model_accum=model_accum,
        )
        parse_ok = True
    except Exception as exc:  # noqa: BLE001
        result.errors.append(type(exc).__name__)
        logger.debug("_ingest_jsonl: parse error: %s", type(exc).__name__)
        # offset 据え置き（冪等リトライ）
        new_offset = start_offset

    # モデル別に upsert
    for model, acc in model_accum.items():
        cost_usd, _known = compute_cost_usd(
            model=model,
            input_tokens=acc["input_tokens"],
            output_tokens=acc["output_tokens"],
            cache_read_tokens=acc["cache_read_tokens"],
            cache_create_tokens=acc["cache_create_tokens"],
        )
        # attribution_skill を長さ制限して格納（PII 永続化リスク低減 / SR-K-003）
        raw_skill = acc.get("attribution_skill") or attribution_skill
        trimmed_skill = raw_skill[:MAX_ATTRIBUTION_SKILL_LEN] if raw_skill is not None else None
        ok = insert_agent_cost_run(
            session_id=session_id,
            agent_id=agent_id,
            agent_type=agent_type,
            description=description,
            model=model,
            attribution_skill=trimmed_skill,
            input_tokens=acc["input_tokens"],
            output_tokens=acc["output_tokens"],
            cache_read_tokens=acc["cache_read_tokens"],
            cache_create_tokens=acc["cache_create_tokens"],
            total_cost_usd=cost_usd,
            db_path=db_path,
        )
        if ok:
            result.runs_upserted += 1
        else:
            result.errors.append("InsertAgentCostRunFailed")

    # parse 成功かつ新規行がある時のみ offset を更新（冪等性の核）
    # 空ファイル・全行処理済み時は据え置き（無駄なリスキャンは許容範囲、冪等性は upsert で担保）
    if parse_ok and new_offset > start_offset:
        set_ingest_offset(file_key, new_offset, db_path=db_path)

    if model_accum or parse_ok:
        result.files_processed += 1


def _accumulate_usage(
    jsonl_path: Path,
    start_offset: int,
    model_accum: dict,
) -> int:
    """jsonl_path の start_offset 行目以降を走査し model_accum に集計する。

    Args:
        jsonl_path: 走査対象の jsonl ファイルパス（resolve 済み）。
        start_offset: 処理開始行インデックス（0 始まり、既処理行数）。
        model_accum: モデル別集計 dict（呼び出し元が保持・更新される）。

    Returns:
        走査後の新しい offset（読み込んだ総行数）。

    Raises:
        任意の例外（json.JSONDecodeError 等）: parse 失敗時に呼び出し元で捕捉。
    """
    total_lines = 0
    with jsonl_path.open(encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f):
            total_lines = line_no + 1
            # start_offset 行目より前はスキップ
            if line_no < start_offset:
                continue

            # 1 行サイズ上限チェック（DoS 防止 / SR-V-001 対応）
            # 超過行は ValueError を raise → 呼び出し元で parse error 扱い → offset 据え置き
            if len(raw_line.encode("utf-8")) > MAX_LINE_BYTES:
                raise ValueError(
                    f"line {line_no} exceeds MAX_LINE_BYTES ({MAX_LINE_BYTES})"
                )

            stripped = raw_line.strip()
            if not stripped:
                continue

            # json.JSONDecodeError は呼び出し元で捕捉→ offset 据え置き
            record = json.loads(stripped)

            if record.get("type") != "assistant":
                continue

            msg = record.get("message", {})
            model = msg.get("model", "")
            if not model:
                continue

            usage = msg.get("usage", {})
            input_tok = int(usage.get("input_tokens", 0))
            output_tok = int(usage.get("output_tokens", 0))
            # キー対応: cache_read_input_tokens → cache_read_tokens
            #           cache_creation_input_tokens → cache_create_tokens
            cache_read_tok = int(usage.get("cache_read_input_tokens", 0))
            cache_create_tok = int(usage.get("cache_creation_input_tokens", 0))

            if model not in model_accum:
                model_accum[model] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_create_tokens": 0,
                    "attribution_skill": None,
                }

            model_accum[model]["input_tokens"] += input_tok
            model_accum[model]["output_tokens"] += output_tok
            model_accum[model]["cache_read_tokens"] += cache_read_tok
            model_accum[model]["cache_create_tokens"] += cache_create_tok

            # attributionSkill は最初に見つかったものを採用
            if model_accum[model]["attribution_skill"] is None:
                skill = record.get("attributionSkill")
                if skill:
                    model_accum[model]["attribution_skill"] = str(skill)

    return total_lines


def _read_agent_meta(
    meta_path: Path,
    *,
    project_dir: Path,
) -> tuple[str | None, str | None]:
    """agent-*.meta.json を読み (agentType, description) を返す。

    Args:
        meta_path: meta.json のパス（resolve 前でよい）。
        project_dir: プロジェクトルート（resolve 済み）。traversal チェックに使用。

    Returns:
        (agentType, description) のタプル。
        - meta 不在 / symlink / project_dir 外: (None, None)
        - parse エラー: (None, None)
        - agentType なし: (None, description_or_None)
    """
    # symlink でない project_dir 配下の実在ファイルか検証し、resolved パスを得る。
    # resolve() の例外は debug ログのみ（symlink/非ファイル/範囲外は静かにスキップ）。
    try:
        resolved_meta = _safe_resolved_file(
            meta_path, project_dir, log_label="_read_agent_meta"
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("_read_agent_meta: resolve error: %s", type(exc).__name__)
        return None, None
    if resolved_meta is None:
        return None, None

    try:
        data = json.loads(resolved_meta.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("_read_agent_meta: parse error: %s", type(exc).__name__)
        return None, None

    agent_type = data.get("agentType") or None
    description = data.get("description") or None
    return agent_type, description


def resolve_projects_root() -> Path | None:
    """Claude Code の ~/.claude/projects/ ルートを返す。

    Returns:
        ~/.claude/projects/ のパス（存在する場合）。
        存在しない場合は None。
    """
    candidate = Path.home() / ".claude" / "projects"
    if candidate.is_dir():
        return candidate
    return None
