#!/usr/bin/env python3
"""Phase 4 hook for tier-routing: check for gaps in learned outcome records.

session_stop.py Phase 4 として起動される欠落検知 hook。
tier_autoapply.jsonl（起動ログ）vs agent_outcomes（学習記録）の
session_id・role 別カウント突合により、記録が漏れた可能性を警告する。

設計: architecture-report-20260707-065043.md §5
実装契約: tests/hooks/test_tier_gap_check.py の fixture 仕様参照

警告のみで副作用なし。全例外は握って exit 0 (fail-safe)。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

# このファイルは .claude/hooks/ に置かれている前提。
# 上位 1 階層を遡って .claude/ ディレクトリを得る: hooks/ → .claude/
_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
# 1 階層遡りで `.claude` に到達することを実行時に検証。
if not (_CLAUDE_DIR.endswith(os.sep + ".claude") or _CLAUDE_DIR.endswith("/.claude")):
    raise RuntimeError(
        f"_CLAUDE_DIR resolution broke: expected to end with '.claude' but got {_CLAUDE_DIR!r}. "
        "Check that this file is at .claude/hooks/."
    )

APPLIED_STATE_PATH = os.path.join(_CLAUDE_DIR, "state", "tier_autoapply.jsonl")
TIER_SELECTION_PATH = os.path.join(_CLAUDE_DIR, "state", "tier_selection.json")

# roll 対象: developer のみ（tester は除外）。
# 理由: tier_autoapply.py LAUNCH_LOG_ROLES に tester を含めて起動ログは記録するが、
# dev-workflow の記録契約上 tester の outcome 記録（D-5）は成功時 1 回が上限。
# tester の起動（Red フェーズでの テスト実行・confirm 起動など）は複数回のため、
# 起動 N >> 記録 M となり常時 K' > 0 で誤警告が発火する（カーディナリティ不一致）。
# CR F-1 で構造的誤検知として developer のみに限定した（起動ログ記録は維持）。
_EVALUATED_ROLES = frozenset({"developer"})
# 直近5分は中間状態として除外
_RECENT_WINDOW = timedelta(minutes=5)

# item1(SR-NEW・Med): _warn_gap の stderr 表示直前に session_id へ適用する
# サニタイズ正規表現。stop.py::_INHERIT_SANITIZE_RE と同一範囲を自己完結実装した
# （hooks は import 非依存方針のため他 hook の関数を import せず同型を複製する）。
# 除去対象: C0 制御文字（\t=0x09 と \n=0x0a は保持）・C1 制御文字・DEL・
# U+2028 LINE SEPARATOR・U+2029 PARAGRAPH SEPARATOR。raw string は \uXXXX を
# 解釈しないため U+2028/U+2029 は chr() で連結する（stop.py と同一作法）。
_SESSION_ID_SANITIZE_RE = re.compile(
    r'[\x00-\x08\x0b-\x1f\x7f-\x9f' + chr(0x2028) + chr(0x2029) + r']'
)

# item5(SR-NEW・Low): 読み取り側のサイズ上限（DoS 抑止）。追記側
# tier_autoapply.py::_rotate_if_needed（1MB 末尾500行）とは別レイヤの
# 読み取り防御で、超過時は末尾優先（tail-priority）で最大 5MB のみ走査する。
# tier_autoapply.jsonl は末尾ほど新しい行のため、先頭を犠牲にしても直近の
# 起動記録を優先して読める。打ち切りは fail-safe（欠落側=N 過少計上）に倒れ、
# 既存の「破損時は検知漏れ側で安全」方針と整合する。
_MAX_READ_BYTES = 5 * 1024 * 1024


def _iter_capped_lines(path: str):
    """jsonl を末尾優先で最大 _MAX_READ_BYTES だけ読み、行文字列を yield した。

    ファイルサイズが上限を超えるときは末尾 _MAX_READ_BYTES に seek し、seek 後の
    最初の（途中で切れた）部分行を捨ててから完全行のみを返す（tail-priority）。
    上限以下なら先頭から全行を返す。バイナリで開き errors='replace' で decode する
    （途中行の decode 失敗で全体が止まらないように）。

    [CR-NEW2] 同型の自己完結複製が
    record_agent_outcome.py::_iter_applied_state_capped_lines（定数
    _APPLIED_STATE_MAX_READ_BYTES）にも存在した。一方を変更する際は他方も同期する。
    """
    with open(path, "rb") as f:
        try:
            f.seek(0, os.SEEK_END)
            size = f.tell()
        except OSError:
            size = 0
        if size > _MAX_READ_BYTES:
            f.seek(size - _MAX_READ_BYTES)
            f.readline()  # 途中で切れた先頭部分行を捨てる
        else:
            f.seek(0)
        for raw in f:
            yield raw.decode("utf-8", errors="replace")


def run(payload: dict) -> None:
    """session_stop.py Phase 4 エントリポイント.

    payload から session_id を取得（無ければ tier_selection.json fallback）し、
    tier_autoapply.jsonl（起動）vs agent_outcomes（記録）の欠落を role 別に
    検針。K' = N - M - Z_role > 0 なら stderr に警告。
    全エラーは握って return（例外を外へ伝播させない）。
    """
    try:
        _run_impl(payload)
    except Exception:
        # fail-safe: 全エラー沈黙
        pass


def _run_impl(payload: dict) -> None:
    """実装本体（try/except で包まれている前提）."""
    from c3 import db as c3_db

    # session_id を確定（payload または tier_selection.json から）
    session_id = payload.get("session_id")
    if session_id is None:
        try:
            sel = json.loads(
                open(TIER_SELECTION_PATH, encoding="utf-8").read()
            )
            session_id = sel.get("session_id")
        except (FileNotFoundError, json.JSONDecodeError, IOError):
            pass

    # item4(SR-V-001・Low): 非 str session_id は None 扱いにする（payload 由来・
    # tier_selection.json 由来の双方に効かせる）。_count_launches の role_recorded/ts
    # 行単位型ガード（F-9）と対称にし、非文字列が _warn_gap の `session_id[:8]` で
    # TypeError を起こして警告経路が意図せず沈黙する経路を塞ぐ。
    if not isinstance(session_id, str):
        session_id = None

    # session_id が確定できなければ突合対象外（沈黙）
    if session_id is None:
        return

    # applied-state（tier_autoapply.jsonl）を読む
    # item2(SR-V-002・Low): symlink 経由の読み取りは沈黙 skip（不在扱い）。os.path.isfile
    # は symlink 先が実ファイルなら True を返すため、islink を併せて検証する。
    if not os.path.isfile(APPLIED_STATE_PATH) or os.path.islink(APPLIED_STATE_PATH):
        # jsonl 不在 or symlink = kill-switch 相当 → N=0 → 沈黙
        return

    # jsonl から role 別・session_id 一致の起動数を数える（直近5分除外）
    now = datetime.now(timezone.utc)
    recent_threshold = now - _RECENT_WINDOW
    n_by_role = _count_launches(session_id, recent_threshold)

    # N が全 role で 0 なら欠落の可能性がないので沈黙
    if not n_by_role or all(v == 0 for v in n_by_role.values()):
        return

    # agent_outcomes DB へ接続
    db_path = c3_db.locate_c3_db()
    if db_path is None:
        return

    # role 別に M（記録数）と Z_role（NULL 非対称抑止補正）を取得
    conn = sqlite3.connect(str(db_path))
    # busy_timeout を適用。public 定数 BUSY_TIMEOUT_MS を参照し自前で PRAGMA 適用する
    # （F-10: private _apply_busy_timeout のモジュール境界越し直接呼び出しを回避）。
    # PRAGMA はパラメータバインド不可のため int() で PRAGMA インジェクションを防ぐ。
    conn.execute(f"PRAGMA busy_timeout={int(c3_db.BUSY_TIMEOUT_MS)}")
    try:
        for role in _EVALUATED_ROLES:
            n = n_by_role.get(role, 0)
            if n == 0:
                continue

            # M: 当該 session_id 一致の outcome 件数（read-only）
            m = _count_outcomes(conn, session_id, role)

            # Z_role: 当該 session の ts_floor 以降の NULL outcome 件数
            ts_floor = _get_ts_floor(session_id)
            z_role = _count_null_outcomes_after_ts(conn, ts_floor, role)

            # K' = N - M - Z_role（下限 0）で判定
            k_prime = max(0, n - m - z_role)
            if k_prime > 0:
                _warn_gap(session_id, role, n, m, z_role, k_prime)
    finally:
        conn.close()


def _count_launches(session_id: str, recent_threshold: datetime) -> dict[str, int]:
    """jsonl から role 別・session_id 一致・直近5分除外の起動数を数える.

    session_id NULL の行は対象外（§5-2 項1）。
    非 dict 行や role_recorded が非文字列の行は個別 skip（行単位型ガード）。
    """
    result = {}
    # item2(SR-V-002): symlink 経由の読み取りは skip（_run_impl 側と同型）。
    if not os.path.isfile(APPLIED_STATE_PATH) or os.path.islink(APPLIED_STATE_PATH):
        return result

    recent_ts_str = recent_threshold.isoformat(timespec="seconds")
    try:
        # item5(SR-NEW): 末尾優先で最大 5MB のみ走査する（_iter_capped_lines）。
        for line in _iter_capped_lines(APPLIED_STATE_PATH):
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            # 非 dict 行は skip（JSON 配列・文字列など）
            if not isinstance(row, dict):
                continue

            row_session_id = row.get("session_id")
            if row_session_id != session_id or row_session_id is None:
                continue

            role = row.get("role_recorded")
            # role_recorded が非文字列なら skip（型不正行回避）
            if not isinstance(role, str):
                continue
            role = role.lower()
            if role not in _EVALUATED_ROLES:
                continue

            ts = row.get("ts")
            # ts が非文字列（None/int 等）の行は skip（_get_ts_floor と対称な型ガード）。
            # F-9: 型不正 1 行で `>=` 比較が TypeError を投げ、run() の fail-safe まで
            # 伝播してセッション全体の検知が沈黙する問題クラスを塞ぐ。
            if not isinstance(ts, str):
                continue
            # 直近5分（recent_threshold 以降）の行は除外
            if ts >= recent_ts_str:
                continue

            result[role] = result.get(role, 0) + 1
    except (IOError, OSError):
        pass

    return result


def _get_ts_floor(session_id: str) -> str | None:
    """当該 session の tier_autoapply.jsonl 最古行の ts を取得.

    抑止補正 Z_role の下限となる（§5-2 項3）。
    非 dict 行や role_recorded が非文字列の行は個別 skip（行単位型ガード）。
    """
    # item2(SR-V-002): symlink 経由の読み取りは skip（_count_launches と同型）。
    if not os.path.isfile(APPLIED_STATE_PATH) or os.path.islink(APPLIED_STATE_PATH):
        return None

    ts_floor = None
    try:
        # item5(SR-NEW): 末尾優先で最大 5MB のみ走査する（_iter_capped_lines）。
        for line in _iter_capped_lines(APPLIED_STATE_PATH):
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            # 非 dict 行は skip
            if not isinstance(row, dict):
                continue

            if row.get("session_id") == session_id:
                ts = row.get("ts")
                # ts が文字列であることを確認
                if isinstance(ts, str) and ts:
                    if ts_floor is None or ts < ts_floor:
                        ts_floor = ts
    except (IOError, OSError):
        pass

    return ts_floor


def _count_outcomes(
    conn: sqlite3.Connection, session_id: str, role: str
) -> int:
    """agent_outcomes から session_id 一致の outcome 件数を数える (read-only).

    session_id NULL の行は WHERE session_id = ? に一致せず除外される（§5-2 項2）。
    """
    try:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM agent_outcomes WHERE session_id = ? AND role = ?",
            (session_id, role),
        )
        result = cursor.fetchone()
        return result[0] if result else 0
    except sqlite3.Error:
        return 0


def _count_null_outcomes_after_ts(
    conn: sqlite3.Connection, ts_floor: str | None, role: str
) -> int:
    """当該 session 開始以降（ts >= ts_floor）の NULL outcome 件数を数える.

    NULL 非対称抑止補正 Z_role（§5-2 項3）。
    """
    if ts_floor is None:
        return 0

    try:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM agent_outcomes WHERE session_id IS NULL AND ts >= ? AND role = ?",
            (ts_floor, role),
        )
        result = cursor.fetchone()
        return result[0] if result else 0
    except sqlite3.Error:
        return 0


def _warn_gap(
    session_id: str, role: str, n: int, m: int, z_role: int, k_prime: int
) -> None:
    """欠落の可能性を stderr に警告.

    警告文言（§5-3）: 「可能性」「誤検知」「抑止」の語を含む。
    """
    # item1(SR-NEW・Med): session_id は tier_selection.json（外部編集可能な state
    # ファイル）由来のことがあるため、stderr 表示直前に制御文字/ANSI エスケープ/
    # U+2028/U+2029 を除去してから [:8] に切り詰める（サニタイズ→切り詰めの順を守る）。
    # stop.py::_INHERIT_SANITIZE_RE と同一範囲の自己完結実装（_SESSION_ID_SANITIZE_RE）。
    # 警告文言の他の埋め込み値のうち role は内部定数（_EVALUATED_ROLES に限定・
    # _count_launches で検証済み）、n/m/z_role/k_prime は int のため外部由来値の
    # サニタイズ対象は session_id のみ。
    sid_short = _SESSION_ID_SANITIZE_RE.sub("", session_id)[:8] if session_id else "unknown"
    warning = (
        f"[tier_gap_check] 学習記録の欠落の可能性: session={sid_short} role={role}\n"
        f"  起動 {n} 件に対し outcome 記録 {m} 件"
        f"（session 内 NULL 記録 {z_role} 件を減算後の差 {k_prime}）。\n"
        f"  record_agent_outcome.py の実行漏れの可能性がありますが、"
        f"途中 Stop・単発起動・\n"
        f"  記録タイミングにより誤検知の場合もあります"
        f"（警告のみ・副作用なし）。\n"
        f"  ※ session_id 未記録（tier_selection.json 不在時）の outcome は、"
        f"当該セッション\n"
        f"    開始時刻以降のものを K'=N-M-Z で差し引いており、"
        f"非対称による誤警告を抑止しています\n"
        f"    （DC-AS-001 round3・恒久抑止はしない）。"
    )
    print(warning, file=sys.stderr)
