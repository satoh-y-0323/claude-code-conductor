#!/usr/bin/env python3
"""
restore_session.py: SessionStart(compact) hook.
コンテキスト圧縮後に現在のセッション状態を再注入する。
"""

import os
import re
import sys

try:
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8', newline='\n')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
SESSIONS_DIR = os.path.join(_CLAUDE_DIR, 'memory', 'sessions')

# 定数（architecture §3.5 / plan T4）
# GENBA = 「現在地」（現場）の音写命名。architecture §2.3 で確定した用語（CR M-01）。
# 他 Python consumer（init-session 等）が生まれたら session_utils へ移動すること（CR L-06）。
APPROACH_TAIL_LINES = 15
# GENBA_DONE: 現在地値が「完了」状態を表す定数（GENBA = 現在地 の音写）（CR M-01）。
# 他 Python consumer が生まれたら session_utils へ移動すること（CR L-06）。
GENBA_DONE = '完了'

# stdout の文字数上限（DC-AS-001）
MAX_OUTPUT_CHARS = 10000
OUTPUT_SAFETY_MARGIN = 200

# 残タスク切り詰め通知文言テンプレート（architecture §2-4）
TODO_TRUNCATION_NOTICE = (
    '⚠️ 残タスクは全 {total} 件のうち先頭 {shown} 件のみ表示している'
    '（C3 が stdout {limit} 文字上限に収めるため末尾側を切り詰めた）。'
    '表示されていない項目があるため、全文は `{path}` を Read して確認すること。'
)

# 現在地（genba）値の表示上限（characters）。
# ①はマーカーより前方に出力されるため、①の長さがマーカーの生存位置を直接決める。
# 現在地は運用規約上「フェーズD 実装中」等の短い定型値であり（dev-workflow SKILL.md）、
# 実値は 50 文字程度。200 は正常系に一切影響しない。値の根拠は plan §1-5。
MAX_GENBA_CHARS = 200

GENBA_TRUNCATION_SUFFIX = (
    '…[現在地は全 {total} 文字中 先頭 {shown} 文字のみ表示。全文は `{path}` を Read]'
)

# 現在地フィールドの読み取り用 regex（architecture §2.3）
_GENBA_RE = re.compile(r'^現在地:[ \t]*(.*)$', re.MULTILINE)

# date_str（ファイル名由来）の YYYYMMDD 形式検証用（SR L-3 / CR M-03）
# ファイル名経由で任意文字列が混入するのを防ぐ。他の regex 定数と同じモジュールレベルに配置。
_DATE_STR_RE = re.compile(r'^\d{8}$')


def _load_session_utils():
    """session_utils モジュールを動的にロードして返す（同階層）。"""
    import importlib.util

    util_path = os.path.join(_HOOKS_DIR, "session_utils.py")
    spec = importlib.util.spec_from_file_location("session_utils", util_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"session_utils が見つかりません: {util_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def find_latest_session() -> str | None:
    if not os.path.isdir(SESSIONS_DIR):
        return None
    files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith('.tmp')]
    if not files:
        return None
    return os.path.join(SESSIONS_DIR, max(files))


def extract_section(content: str, heading: str) -> str:
    """``session_utils.extract_section`` への薄いラッパー（後方互換用）。

    過去にこのモジュール直下にあった ``extract_section`` を呼び出すテスト・スクリプトとの
    互換維持のため、モジュールレベルで公開する。実体は :mod:`session_utils` 側にある。
    """
    return _load_session_utils().extract_section(content, heading)


def extract_genba(content: str) -> str:
    """セッション本文から「現在地:」行の値を抽出する（architecture §2.3）。

    行正規表現 ``^現在地:[ \\t]*(.*)$`` (MULTILINE) でマッチし、
    トリム済みの値を返す。行が存在しない場合は空文字列（後方互換）。

    Args:
        content: セッションファイル全体のテキスト。

    Returns:
        現在地の値（trim 済み）、または空文字列（行なし・値なし）。
    """
    m = _GENBA_RE.search(content)
    return m.group(1).strip() if m else ''


def _tail(text: str, n: int) -> str:
    """テキストの末尾 n 行を返す（architecture §3.5）。

    行数が n 以下の場合はテキストをそのまま返す（切り詰めない）。

    Args:
        text: 対象テキスト。
        n: 末尾から取得する行数。

    Returns:
        末尾 n 行（n 以下の場合は全体）。

    Note:
        n=0 のとき ``lines[-0:]`` は ``lines[0:]``（全体）と等価なため、
        全行を返す（切り詰めない）。Python の ``-0 == 0`` による反直感挙動（CR M-04）。
        呼び出し元は ``APPROACH_TAIL_LINES=15``（固定定数）のため実害はないが、
        仕様として明記・固定する。
    """
    lines = text.splitlines()
    return '\n'.join(lines[-n:]) if len(lines) > n else text  # nul-boundary: allow(末尾 N 行を人間可読テキストへ戻す。呼び出し元は表示のため再 splitlines するだけ)


def _sanitize_genba(value: str) -> str:
    """現在地の値を出力に埋め込む前にサニタイズする（architecture §10 / SR 観点）。

    ``session_utils.sanitize_value`` への薄いラッパー。
    サニタイズ範囲を ``stop.py::_INHERIT_SANITIZE_RE`` と同等以上に保つため、
    共通関数に委譲する（SR M-1 / CR M-02）:
    - 改行文字（\\n / \\r）を除去する
    - C0 制御文字・DEL（\\x7f）・C1 制御文字（\\x80-\\x9f）・U+2028/U+2029 を除去する
    - ``-->`` を ``-- >`` に置換して HTML コメントブロックの破壊を防ぐ
    - タブ（\\t）は保持する（SR L-1・session_utils.sanitize_value 参照）
    """
    return _load_session_utils().sanitize_value(value)


def _cap_genba(value: str, path: str, limit: int = MAX_GENBA_CHARS) -> str:
    """現在地の値を limit 文字で切り詰め、切り詰めた場合のみ fail-loud 表示を付ける。

    ①ワークフロー復帰指示は fail-loud マーカーより前方に出力されるため、
    現在地の値が肥大するとマーカーが上限境界の外へ押し出されて静かに消える。
    それを防ぐためのキャップ（SR-NEW）。

    判定は ``len(value) <= limit`` で「ちょうどは切り詰めない」側に寄せる
    （``_fit_items`` と同じ規約）。切り詰めは末尾を落として先頭を残す
    （現在地の値は先頭にフェーズ名が来るため）。

    Note:
        ``value[:limit]`` は末尾を削るだけなので、サニタイズ済み文字列に存在しなかった
        ``-->`` が新たに出現することはない（削除操作は新しい部分列を作らない）。
        grapheme cluster（絵文字の ZWJ 連結など）の途中で切れる可能性はあるが、
        表示の乱れのみで安全性には影響しないため許容する。
    """
    if len(value) <= limit:
        return value
    return value[:limit] + GENBA_TRUNCATION_SUFFIX.format(
        total=len(value), shown=limit, path=path
    )


def _fit_items(items: list[str], budget: int) -> list[str]:
    """予算に収まる項目を先頭から順に返す（architecture §2-2）。

    項目の途中では絶対に切らない（意味の破壊とサニタイズ済みトークン `-- >` の分断を防ぐ）。
    `budget <= 0` のとき必ず空リストを返す。
    `_tail` の `n=0` が全体を返す反直感挙動とは意味が逆である点に注意。

    Args:
        items: サニタイズ済みの項目文字列リスト。
        budget: 総文字数の上限。

    Returns:
        予算に収まるまでの先頭連続部分。budget <= 0 のときは空リスト。
    """
    if budget <= 0:
        return []

    kept = []
    used = 0
    for item in items:
        # 2 件目以降は '\n' の 1 文字が挿入されるコスト
        cost = len(item) + (1 if kept else 0)
        if used + cost > budget:
            break  # continue ではない（前置の連続性を保つ）
        kept.append(item)
        used += cost

    return kept


def _truncation_notice(total: int, shown: int, path: str) -> str:
    """残タスク切り詰め通知文言を生成する（architecture §2-4）。

    Args:
        total: 全体の項目数。
        shown: 実際に表示される項目数。
        path: セッションファイルの絶対パス。

    Returns:
        フォーマットされた通知文字列。
    """
    return TODO_TRUNCATION_NOTICE.format(
        total=total,
        shown=shown,
        limit=MAX_OUTPUT_CHARS,
        path=path
    )


def main():
    path = find_latest_session()
    if not path or not os.path.exists(path):
        sys.exit(0)

    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    session_utils = _load_session_utils()
    _extract_section = session_utils.extract_section
    _sanitize = session_utils.sanitize_value

    date_str = os.path.basename(path).replace('.tmp', '')

    # date_str の YYYYMMDD 形式検証（SR L-3）:
    # ファイル名経由で任意文字列が date_str に混入するのを防ぐ。
    # 8桁数字以外のファイル名はヘッダへの注入を避けるため exit 0 でスキップする。
    if not _DATE_STR_RE.match(date_str):
        sys.exit(0)

    # 現在地フィールドを読み取る（architecture §3.2 step2）
    genba = extract_genba(content)
    genba_in_progress = genba != '' and genba != GENBA_DONE

    todos = _extract_section(content, '残タスク')
    successes = _extract_section(content, 'うまくいったアプローチ')
    failures = _extract_section(content, '試みたが失敗したアプローチ')

    # - [ ] 行のみにフィルタ（architecture §3.4）
    # フィルタ判定は元行の lstrip() で行い（- [ ] プレフィックス保持）、
    # 出力時に sanitize_value でサニタイズする（SR M-2）。
    pending_todos = [
        _sanitize(line) for line in todos.splitlines()
        if line.lstrip().startswith('- [ ]')
    ]

    # early-exit 判定（architecture §3.2 step4）
    if not genba_in_progress and not pending_todos and not successes and not failures:
        sys.exit(0)

    # ========== 2 段構成・段階 1: head_parts と tail_parts を事前組立 ==========

    head_parts = []

    # safe_path: セッションファイルの絶対パスをサニタイズして通知に使う
    # （①の切り詰め表示と残タスク切り詰めマーカーの両方で使うため冒頭で 1 回だけ算出する）
    safe_path = _sanitize(path)

    # ①ワークフロー復帰指示（現在地が進行中のときのみ・出力冒頭）
    if genba_in_progress:
        # キャップは必ず _sanitize_genba の後に掛ける:
        # sanitize_value は '-->' → '-- >' で 1 出現あたり 1 文字増えるため、
        # サニタイズ前に長さを測ると過小評価になる。
        safe_genba = _cap_genba(_sanitize_genba(genba), safe_path)
        head_parts.append(
            f'⚠️ dev-workflow 進行中（現在地: {safe_genba}）。\n'
            f'残作業に直接着手せず、対応 skill（develop / review-phase / start）経由で再開し、\n'
            f'各エージェント出力後の Approval Flow を守ること。'
        )

    # ②ヘッダ
    head_parts.append(f'[C3 セッション復元: {date_str} / 圧縮後リマインダー]')

    # tail_parts: ④⑤を先に組み立てて fixed_len 計算に使う
    tail_parts = []

    # ④うまくいったアプローチ（末尾 N 行に切り詰め・行単位でサニタイズ）（SR M-2）
    if successes:
        tail_text = _tail(successes, APPROACH_TAIL_LINES)
        n = len(successes.splitlines())
        # 切り詰めが起きたときだけサフィックスを付ける（DC-GP-005）
        if n > APPROACH_TAIL_LINES:
            heading = f'\n## うまくいったアプローチ（末尾 {APPROACH_TAIL_LINES} 行のみ・全 {n} 行）'
        else:
            heading = '\n## うまくいったアプローチ'
        tail_parts.append(heading)
        sanitized_lines = [_sanitize(line) for line in tail_text.splitlines()]
        tail_parts.append('\n'.join(sanitized_lines))  # nul-boundary: allow(stdout へ出す復元メッセージの成功アプローチ段落。表示専用)

    # ⑤試みたが失敗したアプローチ（末尾 N 行に切り詰め・行単位でサニタイズ）（SR M-2）
    if failures:
        tail_text = _tail(failures, APPROACH_TAIL_LINES)
        n = len(failures.splitlines())
        # 切り詰めが起きたときだけサフィックスを付ける（DC-GP-005）
        if n > APPROACH_TAIL_LINES:
            heading = f'\n## 試みたが失敗したアプローチ（末尾 {APPROACH_TAIL_LINES} 行のみ・全 {n} 行）'
        else:
            heading = '\n## 試みたが失敗したアプローチ'
        tail_parts.append(heading)
        sanitized_lines = [_sanitize(line) for line in tail_text.splitlines()]
        tail_parts.append('\n'.join(sanitized_lines))  # nul-boundary: allow(stdout へ出す復元メッセージの失敗アプローチ段落。表示専用)

    # ========== 段階 2: 固定部長から残タスク予算を逆算し、切り詰め通知を判定 ==========

    # fixed_len は head_parts + tail_parts + print() の末尾改行による実効文字数
    fixed_len = len('\n'.join(head_parts + tail_parts))  # nul-boundary: allow(固定部の長さを測るための一時的な連結で、結果の文字列は len() 計測後に捨てられるため、機械的な分割消費者がない)

    parts = []
    if pending_todos:
        heading = '\n## 残タスク'
        # -3 の内訳: heading 挿入で増える '\n' 1 + 本文挿入で増える '\n' 1 + print() の末尾改行 1
        budget_body = MAX_OUTPUT_CHARS - OUTPUT_SAFETY_MARGIN - fixed_len - len(heading) - 3
        body = '\n'.join(pending_todos)  # nul-boundary: allow(stdout へ出力する残タスク本文の構成で、読み手は表示または全文 Read のみであり、機械的な分割消費者がない)

        if len(body) <= budget_body:
            # パス 1: 全件がマーカーなしで収まる
            parts = [heading, body]
        else:
            # パス 2: 先頭部分に切り詰め・マーカーを付ける
            total = len(pending_todos)
            # reserve: 最悪ケース（全 total 件を表示する場合のマーカー長）から上界を得る
            reserve = _truncation_notice(total, total, safe_path)
            budget2 = budget_body - len(reserve) - 1  # -1 はマーカー挿入で増える '\n'
            kept = _fit_items(pending_todos, budget2)
            notice = _truncation_notice(total, len(kept), safe_path)
            # kept が空のときだけ本文パートを足さない（マーカーは必ず出す・fail-loud）
            parts = [heading, notice] + (['\n'.join(kept)] if kept else [])  # nul-boundary: allow(stdout へ出力するための出力部分リストの構成で、読み手は表示または全文 Read のみであり、機械的な分割消費者がない)

    # 先頭優先を採る理由は 2 点（DC-AS-002）:
    # (1) ハーネスの truncate preview は先頭側を残すため、C3 側の予算計算にズレがあっても
    #     優先度の高い内容が生き残る（二重防御）
    # (2) 落とした項目は fail-loud マーカーと全文ポインタで必ず通知される

    # 上限保証コメント（DC-AS-003）:
    # 上限 `MAX_OUTPUT_CHARS` を満たす保証条件は **`budget2 >= 0`** であること。
    # `kept` が空のときの総長は `fixed_len + len(heading) + len(notice) + 3` で、
    # `len(notice) <= len(reserve)` かつ `budget2 >= 0` ⟹ `len(reserve) <= budget_body - 1`
    # より上限内に収まる。
    # 固定部①②④⑤の性質はマーカーとの位置関係で 3 つに分かれる:
    # - ①現在地（マーカーより前）: `MAX_GENBA_CHARS` でキャップ済み。
    #   したがって①が `budget2 < 0` の原因になることはなく、マーカーは常に
    #   `MAX_OUTPUT_CHARS` 境界の内側に留まる。
    # - ②ヘッダ（マーカーより前）: 固定長（`date_str` は `^\d{8}$` 検証済み）で変動しない。
    # - ④⑤アプローチ（マーカーより後）: `_tail` の行数上限のみで文字数上限がないため、
    #   総長が上限を超え得る（`budget2 < 0` になり得るのはこの肥大時のみ）。
    #   ただしマーカーは④⑤より前方にあるため、マーカーの生存は④⑤の大きさに依存しない。
    # よってマーカー生存を脅かすのは①の肥大だけであり、それは `MAX_GENBA_CHARS` で
    # キャップ済みである。④⑤による総長超過は requirements §6-4 が明示的に
    # スコープ外宣言した領域であること（クラッシュはせず、マーカーは生存するが
    # 上限は保証されない）。

    lines = head_parts + parts + tail_parts
    print('\n'.join(lines))  # nul-boundary: allow(stdout へ出力する復元メッセージ全体。読み手は Claude Code の表示でリポジトリ内に split 側がない)


if __name__ == '__main__':
    main()
