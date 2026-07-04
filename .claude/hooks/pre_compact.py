#!/usr/bin/env python3
"""PreCompact hook: append checkpoint marker (デバウンス付き)。

直近 DEBOUNCE_WINDOW_SECONDS 秒以内に PreCompact checkpoint が既に存在する場合は、
checkpoint の追記をスキップする（PreCompact の連続起動による重複追記を防ぐ
デバウンス機構）。
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

from session_utils import SESSION_JSON_MARKER, append_checkpoint, is_worktree, SESSIONS_DIR


# デバウンス窓幅（秒）。直近の PreCompact checkpoint からこの秒数以内の再実行は
# 追記をスキップする（4連続起動の重複防止）。
DEBOUNCE_WINDOW_SECONDS = 10

# checkpoint 行抽出のため splitlines() に渡す前に除去する行区切り文字パターン。
# stop.py::_INHERIT_SANITIZE_RE / session_utils._VALUE_SANITIZE_RE と同一範囲（対称）。
# str.splitlines() は \n 以外に \v / \f / \x1c-\x1e / \x85 / U+2028 / U+2029 でも
# 行分割する。append_checkpoint が書き込む summary body（未サニタイズの trigger 等）に
# これらの文字＋checkpoint 行に酷似する文字列が混入すると、splitlines() が偽の行を
# 生成し _PRECOMPACT_CHECKPOINT_RE が誤マッチしてデバウンスの偽陽性を起こし得る。
# stop.py の _inherit_pending_tasks と同趣旨で、分割前に事前除去する。
# raw string は \uXXXX を解釈しないため U+2028 / U+2029 は chr() で連結する。
_LINE_SEPARATOR_SANITIZE_RE = re.compile(
    r'[\x00-\x08\x0b-\x1f\x7f-\x9f' + chr(0x2028) + chr(0x2029) + r']'
)

# checkpoint 行の固定プレフィックス。PreCompact ラベルへのアンカーとして使う
# （他ラベル、例えば Wave checkpoint を除外する）。
_PRECOMPACT_CHECKPOINT_PREFIX = '## [Checkpoint: PreCompact:'

# checkpoint 行として許容する最大長（fix-cycle-2, security-review-report-20260704-075817.md
# [SR-NEW]）。正規の checkpoint ヘッダ行は数十〜数百文字で十分なため、これを超える行は
# パース対象から除外する（regex 撤去後の belt-and-suspenders ガード）。
MAX_CHECKPOINT_LINE_LEN = 512

# parse 成功した timestamp が `now` をこの秒数を超えて上回る場合、構造的異常値として
# 棄却する（fail-open）。checkpoint の timestamp は append_checkpoint が同一ホストの
# UTC 実時刻で書くため、書き込み時刻は必ず読み取り時刻以前になるはず。
#
# 60 秒は独立したセキュリティ判断としての保守値である。正当な checkpoint 行の
# timestamp が読み取り時刻をわずかに上回り得る唯一の現実的経路は、NTP による
# クライアント時計の後方ステップ補正（書き込み時点で実時刻より進んでいた時計が、
# 読み取り時点までに巻き戻される）であり、この巻き戻し幅を吸収するために許容する。
# 通常の NTP ステップ補正は秒〜十数秒規模のため 60 秒あれば十分に吸収できる。
#
# この値を大きく取り過ぎると、許容スキュー以内の未来日時 checkpoint を注入された
# 場合に、実時刻がその timestamp に追いつくまでデバウンスが効き続け、checkpoint 追記
# が最大で許容秒数ぶん停止する（[SR-V-001]）。60 秒に
# 抑えることで、悪意ある注入・データ破損いずれの場合も最大デバウンス停止時間を
# 60 秒以内に限定する。
#
# 未来スキューを跨いだ許容/棄却境界はテスト側で module.FUTURE_SKEW_TOLERANCE_SECONDS
# を参照した相対時刻テストとして固定しており、テストは now= を明示指定して壁時計に
# 依存しない（値をテストフィクスチャ都合で膨らませる必要はない）。
FUTURE_SKEW_TOLERANCE_SECONDS = 60

# parse 失敗時の stderr 診断ログに出す捕捉テキストの最大長
# （fix-cycle-2, security-review-report-20260704-075817.md [SR-R-001]）。
DIAGNOSTIC_MAX_LEN = 64


def _last_precompact_checkpoint_dt(session_file, now=None):
    """session_file 内の最後の PreCompact checkpoint 行から aware datetime を返す。

    ファイル無し・PreCompact 行無し・parse 不能・naive datetime・未来日時（許容
    スキュー超）など、あらゆる異常系で None を返す（fail-open の中核）。

    `now` は未来日時ガードの基準時刻（省略時は呼び出し時点の UTC 実時刻）。
    呼び出し元（main）と判定基準を一致させたい場合は明示的に渡すこと。
    """
    if now is None:
        now = datetime.now(timezone.utc)

    try:
        with open(session_file, encoding='utf-8') as f:
            content = f.read()
    except (OSError, ValueError, TypeError):
        # 「ファイル無し」は初回起動時の通常ケース。ほぼ毎日 1 回 stderr へ
        # 出すとスパムになるため、ここでは無音のまま fail-open する（CR-L-003）。
        return None

    # splitlines() が特殊行区切り文字（\x85 / U+2028 / U+2029 等）で偽の checkpoint
    # 行を生成しないよう、分割前に除去する（stop.py と対称なガード / CR-NEW Medium）。
    lines = _LINE_SEPARATOR_SANITIZE_RE.sub('', content).splitlines()

    for line in reversed(lines):
        # regex（`.* - (.+)`）の二重ワイルドカードによる多項式時間バックトラッキング
        # （ReDoS）を根絶するため、手続き的パースに置き換える
        # （fix-cycle-2, security-review-report-20260704-075817.md [SR-NEW]）。
        stripped = line.rstrip()
        if len(stripped) > MAX_CHECKPOINT_LINE_LEN:
            # 行長ガード。異常に長い行は正規の checkpoint ではあり得ないため
            # 以降のパース処理（rfind 等）に進む前にスキップする。
            continue
        if not stripped.startswith(_PRECOMPACT_CHECKPOINT_PREFIX):
            continue
        if not stripped.endswith(']'):
            continue
        # プレフィックスと末尾 `]` を除いた内側の文字列から、最後の ` - ` を
        # timestamp との区切りとみなす（旧 greedy regex `.* - (.+)]` の「最後の
        # ` - ` フィールドを timestamp とする」挙動と等価。label 内の偶発的な
        # ` - ` があっても安全側に倒れる）。
        # 旧 regex は `(.+)` が 1 文字以上を要求するため、timestamp 欄が完全空文字の
        # 行（例: `## [Checkpoint: PreCompact: manual - ]`）を非マッチとして扱い、
        # より古い有効な checkpoint 行を探し続けていた。空 ts_text を continue で
        # スキップすることで、この空文字境界でも旧 regex と等価に振る舞う。
        inner = stripped[len(_PRECOMPACT_CHECKPOINT_PREFIX):-1]
        sep_idx = inner.rfind(' - ')
        if sep_idx == -1:
            continue
        ts_text = inner[sep_idx + len(' - '):].strip()
        if not ts_text:
            # 空文字 timestamp は旧 greedy regex `(.+)` の非マッチに相当する。
            # 診断ログは出さず（破損ではなく単なる欠落のため）、より古い有効行の
            # 探索を継続する。
            continue
        try:
            dt = datetime.fromisoformat(ts_text)
        except (ValueError, TypeError):
            # 「timestamp が壊れている」のは本来発生しない想定外の破損ケース。
            # ファイル無しの通常ケースと異なり、将来のトラブルシュートのため
            # このケースに限り stderr へ診断ログを 1 行出す（CR-L-003・非対称仕様）。
            # 捕捉テキストは固定長に切り詰めて出力する（SR-R-001）。
            # 戻り値は引き続き None（fail-open で追記継続）。
            print(
                '[PreCompact] 診断: checkpoint 行の timestamp を parse できませんでした'
                f'（fail-open で追記を継続します）: {ts_text[:DIAGNOSTIC_MAX_LEN]!r}',
                file=sys.stderr,
            )
            return None
        if dt.tzinfo is None:
            return None
        if dt > now + timedelta(seconds=FUTURE_SKEW_TOLERANCE_SECONDS):
            # 未来日時（許容スキュー超）は構造的異常値として棄却する（fail-open）。
            # 悪意ある注入・データ破損いずれの場合も、デバウンスの恒久停止を
            # 招かないよう None を返して追記を継続する
            # （fix-cycle-2, security-review-report-20260704-075817.md [SR-V-001]）。
            return None
        return dt

    return None


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        payload = {}

    cwd = os.getcwd()
    if is_worktree(cwd):
        sys.exit(0)

    trigger = payload.get('trigger', 'unknown')
    context_items_before = payload.get('context_items_before')
    context_items_before_str = 'N/A' if context_items_before is None else str(context_items_before)

    now = datetime.now(timezone.utc)
    # 既知の構造的限界（CR-NEW Low・修正しない）: session_file は now の UTC 日付から
    # 算出する。PreCompact の連続起動（実測 約1.2 秒スパン）が UTC 00:00:00 ちょうどを
    # またぐ極稀ケースでは、前後の呼び出しが別日の session_file を参照するため
    # デバウンス判定が効かず checkpoint が二重追記され得る。発生確率は極小のため
    # コードでは対処せず既知の限界として許容する。
    date_str = now.strftime('%Y%m%d')
    session_file = os.path.join(SESSIONS_DIR, f'{date_str}.tmp')

    last = _last_precompact_checkpoint_dt(session_file, now=now)
    if last is not None and (now - last) < timedelta(seconds=DEBOUNCE_WINDOW_SECONDS):
        print('[PreCompact] debounce: 直近の checkpoint を検出したため追記をスキップしました', file=sys.stderr)
        return

    # セキュリティ非対称性の記録（fix-cycle-2, security-review-report-20260704-121824.md
    # [SR-V-001]・情報提供・現状は対応不要）: 以下の summary（body）は
    # session_utils.append_checkpoint の設計上 sanitize_value() 非適用で書き込まれる
    # （複数行 Markdown を保持するための承認済み設計判断・session_utils.py L147 参照）。
    # そのため body に埋め込む trigger（L164）は未サニタイズのまま checkpoint に書かれる。
    # 一方 label（下の f'PreCompact: {trigger}'）は append_checkpoint 内で
    # sanitize_value() を通るため、body と label で sanitize 適用範囲に非対称性がある。
    # 現状 trigger は Claude Code ハーネスが設定する列挙的な値（manual/auto 等）で、
    # 外部入力・LLM 自由記述からの直接汚染経路が無いため実害の確信度は低い。
    # 【将来の拡張時の注意】trigger の由来をハーネス以外（プラグイン・カスタムフック
    # 連携等）に拡張する場合は、偽 checkpoint 注入経路化を防ぐため body 側にも
    # sanitize_value() 適用を検討すること。
    summary = (
        f"- trigger: {trigger}\n"
        f"- context_items_before: {context_items_before_str}\n"
        f"- このポイント以前の詳細な文脈は圧縮により失われます。"
    )
    append_checkpoint(session_file, f'PreCompact: {trigger}', summary)

    print(f'[PreCompact] セッション状態を {session_file} に保存しました', file=sys.stderr)


if __name__ == '__main__':
    main()
