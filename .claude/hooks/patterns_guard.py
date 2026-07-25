#!/usr/bin/env python3
"""PreToolUse hook: patterns.json への直接編集を事故防止ガード（P1・配布対象）。

Write / Edit ツールで `.claude/memory/patterns.json` への書き込みを検出し、
許可フラグがなければブロックする（TTL 型・600 秒）。正規経路は session.tmp 経由の
promote-pattern skill。意図的な編集は `.claude/state/patterns_guard_allow.flag` で
一時許可できる。

## 射程の明記（設計の限界）

- 対象: Write / Edit ツール呼び出しのみ
- 対象外: Bash（python -c・sed 等）/ NotebookEdit 経由の書き込み
- worktree 実行時: 基準が main/worktree のどちらになるかは環境解決に依存
  （本 hook の基準は main の patterns.json 本体の事故防止。main 側保護が
  保護されない場合は検知が働かない限界あり）
- resolve() による大小文字の正準化は実在ファイルのみ（Windows nt._getfinalpathname）。
  patterns.json が未作成の初期環境（c3 init 直後）ではケース違い指定が素通りする
- Windows の末尾ドット・スペース正規化も実在パス限定。非実在パスでは本 hook が検知できない限界
"""

import json
import os
import sys
import time
from pathlib import Path

# 共通ヘルパー (_hook_utils.sanitize_for_terminal) を hooks/ 経由で import するため、
# このスクリプトのディレクトリを PYTHONPATH に追加する。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hook_utils import sanitize_for_terminal  # noqa: E402

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass

# 許可フラグの有効期間（秒）。判定式はこの定数を参照する。
TTL_SECONDS = 600


def main():
    # bypass: 恒久 disable 環境変数の確認
    if os.environ.get('C3_PATTERNS_GUARD_DISABLE') == '1':
        sys.exit(0)

    # JSON payload の読み込み（fail-open）
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    # tool_name の確認（Write/Edit 以外は対象外）
    tool_name = payload.get('tool_name', '')
    if tool_name not in ('Write', 'Edit'):
        sys.exit(0)

    # file_path の抽出（キー欠落は fail-open）
    file_path = payload.get('tool_input', {}).get('file_path', '')
    if not file_path:
        sys.exit(0)

    # プロジェクトルートと保護対象パスの導出（Path(__file__) ベース）
    hook_dir = Path(__file__).resolve().parent
    project_root = hook_dir.parent.parent
    flag_path = project_root / ".claude" / "state" / "patterns_guard_allow.flag"

    # file_path と保護対象を同じ正規化（realpath）にそろえて比較する。
    # NUL byte 混入など不正なパス文字列は OSError / ValueError になるため fail-open。
    try:
        protected_path = (
            project_root / ".claude" / "memory" / "patterns.json"
        ).resolve()
        if os.path.isabs(file_path):
            resolved = Path(file_path).resolve()
        else:
            # 相対パスは CWD を基準に解決（worktree 実行時は worktree CWD）
            resolved = (Path.cwd() / file_path).resolve()
    except (OSError, ValueError):
        sys.exit(0)

    # realpath 一致チェック
    if resolved != protected_path:
        sys.exit(0)

    # patterns.json への書き込みが検出された → フラグチェック
    # フラグが存在し TTL 内なら allow（削除しない＝複数操作可）
    #
    # TOCTOU 免責: exists() / getmtime() / unlink() の間にフラグが差し替えられる
    # 競合は原理的に残る。本 hook は「事故防止ガード」であり悪意ある同時操作に対する
    # セキュリティ境界ではないため、この競合は許容する。
    if flag_path.exists():
        mtime = os.path.getmtime(flag_path)
        age = time.time() - mtime
        if age <= TTL_SECONDS:
            # TTL 内: 許可
            sys.exit(0)
        else:
            # TTL 超過: フラグを削除して block
            try:
                flag_path.unlink()
            except OSError:
                pass

    # ブロック: stderr と exit 2
    flag_create_instruction = (
        f".claude/state/patterns_guard_allow.flag を作成してください（空ファイルで可）。\n"
        f"  例 Bash: touch {sanitize_for_terminal(str(flag_path))}\n"
        f"  例 PowerShell: New-Item -ItemType File -Force "
        f"{sanitize_for_terminal(str(flag_path))}"
    )

    message = (
        f"[PatternGuard BLOCK] patterns.json への直接編集がブロックされました。\n"
        f"正規経路: session.tmp の「##残タスク」へ記載後、promote-pattern skill を実行してください\n"
        f"          または LLM が patterns.json を直接編集する場合は以下を実行:\n"
        f"{flag_create_instruction}\n"
        f"恒久 bypass: C3_PATTERNS_GUARD_DISABLE=1 環境変数を設定してください"
    )

    print(message, file=sys.stderr)
    sys.exit(2)


if __name__ == '__main__':
    main()
