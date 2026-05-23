# parallel-agents 推奨個人設定

C3 の `parallel-agents` skill（`/develop` から間接起動）を快適に使うための、
**Claude Code バージョン要件と個人設定の推奨値**をまとめる。

> 本ドキュメントは **C3 利用者（人間）向け** のリファレンス。
> LLM 向けの skill 動作指針は `.claude/skills/parallel-agents/SKILL.md` を参照。

---

## 概要

`parallel-agents` skill は Claude Code 公式の `isolation: "worktree"` 機構を使い、
`.claude/worktrees/agent-<id>/` 配下に独立した git worktree を作成して並列実行する。
このとき以下 2 点で **利用者環境への期待** が発生する。

| 項目 | 推奨値 | 既定との差 |
|---|---|---|
| Claude Code バージョン | **2.1.150 以降** | 古いと auto-cleanup 不在 |
| `worktree.baseRef` | **`"head"`** | 既定は `"fresh"` (= `origin/<default>` から作成) |

設定しなくても skill 自体は動くが、以下の問題に遭遇しやすくなる:

- 古い CC: 各 wave 終了時に `git worktree remove` が走らず `.claude/worktrees/` に
  dead worktree が積もる
- `baseRef: fresh`: push 前のローカルコミットが worktree から見えず、agent が
  「ファイルが存在しない」と誤判定して fresh create に走る等の事故が起きる

---

## 推奨個人設定

`~/.claude/settings.json` (グローバル個人設定) に以下を追加する:

```json
{
  "worktree": {
    "baseRef": "head"
  }
}
```

既存の `~/.claude/settings.json` がある場合はマージする。例:

```json
{
  "permissions": {
    "defaultMode": "auto"
  },
  "model": "sonnet",
  "worktree": {
    "baseRef": "head"
  }
}
```

### なぜプロジェクト設定 (`.claude/settings.json`) ではなく個人設定か

`worktree.baseRef` は **Claude Code 公式機能の動作モード切替** であり、C3 固有の
設定ではない。C3 が利用者の Claude Code 設定全体を書き換える形になると
「C3 は `.claude/` 内に収まるフレームワーク」という設計思想と矛盾するため、
C3 配布物には含めず利用者個人の判断で適用する形にしている。

### worktree.baseRef: "head" の意味

| 値 | base 元 |
|---|---|
| `"fresh"` (既定) | `origin/<default-branch>` (例: `origin/main`) |
| `"head"` | 現在の local HEAD |

`"head"` にすると、push 前のローカルコミットが worktree にも含まれる。
parallel-agents は「直近の修正を反映した worktree で並列実装させる」ことが目的のため、
`"head"` の方が C3 のワークフローと整合する。

### 副作用

- 作業中のローカル HEAD がコミットされていない（dirty）状態だと、その dirty も
  worktree に持ち込まれる。これは通常のメリット（ローカル変更を反映）と表裏一体
- 壊れた HEAD から worktree を作ると、worktree も壊れる。コミット前にビルドが
  通っているかは利用者が確認する責任

---

## Claude Code バージョン要件

### 2.1.150 以降を推奨する理由

`isolation: "worktree"` 付き Agent が **完了時に worktree を auto-cleanup** する
挙動が安定したのが 2.1.150 系。これにより:

- `.claude/worktrees/` 配下に dead worktree が残らない
- `worktree-agent-<id>` ブランチも自動削除される
- 親 Claude が明示的に `git worktree remove` を呼ぶ必要がない

2.1.150 未満の場合、SKILL.md Step 2-F-3 のフォールバック手順
（`git worktree remove -f -f` + `git branch -D worktree-agent-<id>`）が
**毎回必要** になる。dead worktree が積み重なるとパフォーマンス・容量への
影響もあるため、可能ならアップグレードを推奨する。

### auto-cleanup の信頼度

検証実績 (2026-05-23 〜 2026-05-24):

- foreground / background / 並列 / 失敗 (exit 1) の全パターンで 10/10 cleanup 確認
- ごく稀に **外部要因による一時的ファイルロック** で cleanup が失敗するケースを
  1 件観測 (約 1/11 以下)。原因は未特定（アンチウィルス・バックアップソフト・
  その他常駐プロセスのいずれかの可能性）
- SKILL.md Step 2-F-3 の残留チェック + `git worktree prune` フォールバックで
  実害なく吸収される設計

---

## 設定後の動作確認

1. 設定追加後、Claude Code を再起動する（または `/config` で反映）
2. C3 で wt_tester を 1 件起動するだけの軽量タスクを実行（最も簡単な検証）:
   - `/start` → 「直接指示する」→ 「wt_tester を 1 個 isolation:worktree で pwd だけ実行して」
3. 完了通知後、`git worktree list --porcelain` と `ls .claude/worktrees/` で
   両方 main 以外が無い状態を確認

### baseRef 適用の確認

ローカルだけにある commit がある状態で wt_tester を起動し、worktree 内で
`git log -1` が **その commit を指していれば** baseRef: "head" が効いている。
`origin/main` の commit を指していれば既定（`fresh`）のまま。

---

## トラブルシューティング

### Q. worktree が cleanup されず残る

A. 以下の順に対処:

1. `git worktree list --porcelain` で main 以外の登録があるか確認
2. 登録があれば `git worktree remove -f -f <path>` で削除
3. 物理 dir だけ残っていれば `rm -rf .claude/worktrees/agent-*`
4. ブランチが残っていれば `git branch -d worktree-agent-<id>` (force 必要なら `-D`)

これらは parallel-agents SKILL.md Step 2-F-3 のフォールバック手順そのもの。
auto-cleanup が動いている環境ではほぼ no-op になる。

### Q. wt_developer / wt_tester が「ファイルが存在しない」と言う

A. `worktree.baseRef` が `"fresh"` (既定) のまま、かつローカルに push 前の
コミットでそのファイルを追加しているケースが典型。`worktree.baseRef: "head"` を
設定するか、対象 commit を push してから再実行する。

### Q. 設定変更しても効かない

A. Claude Code の設定読み込みはセッション開始時。`/config` で再読み込みするか、
Claude Code を再起動する。
