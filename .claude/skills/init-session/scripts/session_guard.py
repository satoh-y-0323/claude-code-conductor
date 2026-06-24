#!/usr/bin/env python3
"""Session-start guard helper for /init-session and /start.

3 つのセッション開始 Bash ブロック（mkdir+printf / setup 判定 / init 判定）を
1 つの Python エントリに集約し、settings.json の allow を 1 行プレフィックスで
登録できるようにする（複合 Bash は allow プレフィックス一致が効かないため）。

挙動は旧 Bash と等価に保つ:
  - mark      : .claude/state を作り init_session.flag に session id を書き、
                coding-standards.md / setup_done.flag の有無で SETUP_DONE/SETUP_NEEDED を print
  - check     : init_session.flag を読み strip して CLAUDE_CODE_SESSION_ID と比較し
                INIT_DONE/INIT_NEEDED を print
  - setup-mark: setup_done.flag を書く（/setup Phase 4 用・判定 print なし）

Exit code:
  0: 正常（mark/check の判定結果は stdout で返す。判定自体は exit code で表さない）
  2: 未知 / 欠落サブコマンド（usage を stderr に出す）

セキュリティ境界:
  init_session.flag に書く CLAUDE_CODE_SESSION_ID は認証トークンではなくセッション識別子であり、
  ループ回避判定にのみ使用する。.claude/state/ は gitignore＋配布除外済みで git/wheel に含まれない
  （信頼境界の確認）。

allow のスコープ:
  settings.json の allow エントリは末尾 * のプレフィックス一致。
  追加引数は main() で無視される（多層防御: pre_tool hook も Bash を事前検査）。
"""
from __future__ import annotations

import os
import pathlib
import sys

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------------------
# パス定数
# ---------------------------------------------------------------------------

FLAG_REL = (".claude", "state", "init_session.flag")
STATE_DIR_REL = (".claude", "state")
# setup_done.flag のパスは cmd_mark の SETUP 判定（SETUP_MARKERS_REL 経由）と
# cmd_setup_mark の書き込み（SETUP_DONE_FLAG_REL）で共通参照する単一真実源（CR-M-001）。
SETUP_DONE_FLAG_REL = (".claude", "state", "setup_done.flag")
SETUP_MARKERS_REL = (
    (".claude", "rules", "coding-standards.md"),
    SETUP_DONE_FLAG_REL,  # SETUP_DONE_FLAG_REL を参照して DRY を維持（同一オブジェクト）
)

USAGE = "usage: session_guard.py {mark|check|setup-mark}"


# ---------------------------------------------------------------------------
# パス解決
# ---------------------------------------------------------------------------

def _project_root() -> str:
    """CLAUDE_PROJECT_DIR があればそれ、無ければ cwd。resolve() で `..` を展開し絶対パスを保証する（SR M-1・recall_inject.py と整合）。

    CLAUDE_PROJECT_DIR はハーネス信頼値・パス構成要素はハードコードのため実害は薄いが、
    整合性・防御として resolve() を適用する。
    """
    root = os.environ.get("CLAUDE_PROJECT_DIR")
    if root:
        return str(pathlib.Path(root).resolve())
    return str(pathlib.Path.cwd().resolve())


# ---------------------------------------------------------------------------
# mark サブコマンド
# ---------------------------------------------------------------------------

def cmd_mark() -> int:
    root = _project_root()
    # 1. state ディレクトリ作成（存在しても失敗しない）
    state_dir = os.path.join(root, *STATE_DIR_REL)
    os.makedirs(state_dir, exist_ok=True)
    # 2. flag 書き込み（session id が空でもそのまま空文字を書く＝旧 printf '%s' 等価）
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    flag_path = os.path.join(root, *FLAG_REL)
    with open(flag_path, "w", encoding="utf-8", newline="") as f:
        f.write(sid)
    # 3. SETUP 判定 print
    if any(os.path.isfile(os.path.join(root, *m)) for m in SETUP_MARKERS_REL):
        print("SETUP_DONE")
    else:
        print("SETUP_NEEDED")
    return 0


# ---------------------------------------------------------------------------
# check サブコマンド
# ---------------------------------------------------------------------------

def _read_flag(root: str) -> str:
    """flag を読み、CR/LF と前後空白を除去して返す。読めなければ空文字。"""
    flag_path = os.path.join(root, *FLAG_REL)
    try:
        with open(flag_path, "r", encoding="utf-8") as f:
            raw = f.read()
    except (OSError, ValueError):
        # OSError: ファイル不在・権限エラー
        # ValueError: UnicodeDecodeError（ValueError サブクラス）を含む。flag が UTF-8 非互換でも INIT_NEEDED 安全側に倒す
        return ""
    # 旧 bash: tr -d '\r\n'（全 CR/LF を除去）+ strip
    # replace 順は CRLF/CR/LF を確実に除去する（\r 先行で CRLF の \r を確実に消す）。逆順でも結果は同じだが意図を明示
    return raw.replace("\r", "").replace("\n", "").strip()


def cmd_check() -> int:
    root = _project_root()
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    saved = _read_flag(root)
    if sid and saved == sid:  # 旧 bash: [ -n "$SID" ] && [ "$saved" = "$SID" ]（sid 空なら比較せず INIT_NEEDED に倒す）
        print("INIT_DONE")
    else:
        print("INIT_NEEDED")
    return 0


# ---------------------------------------------------------------------------
# setup-mark サブコマンド
# ---------------------------------------------------------------------------

def cmd_setup_mark() -> int:
    """/setup Phase 4 用: setup_done.flag を書く（存在自体がマーカー・中身不要）。
    判定 print はしない（mark/check と異なり stdout で分岐させない）。
    書き込み例外は握り潰さず上げる（ADR-4 と整合）。
    """
    root = _project_root()
    state_dir = os.path.join(root, *STATE_DIR_REL)
    os.makedirs(state_dir, exist_ok=True)
    flag_path = os.path.join(root, *SETUP_DONE_FLAG_REL)
    with open(flag_path, "w", encoding="utf-8") as f:
        pass  # 空ファイルで可（存在自体がマーカー）
    return 0


# ---------------------------------------------------------------------------
# ディスパッチ（main）
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    cmd = args[0]
    if cmd == "mark":
        return cmd_mark()
    if cmd == "check":
        return cmd_check()
    if cmd == "setup-mark":
        return cmd_setup_mark()
    print(f"{USAGE}\nunknown subcommand: {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
