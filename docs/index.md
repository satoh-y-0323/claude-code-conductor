# Claude Code Conductor (C3)

[![PyPI version](https://img.shields.io/pypi/v/claude-code-conductor.svg)](https://pypi.org/project/claude-code-conductor/)
[![Python versions](https://img.shields.io/pypi/pyversions/claude-code-conductor.svg)](https://pypi.org/project/claude-code-conductor/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/satoh-y-0323/claude-code-conductor/blob/main/LICENSE)
[![Publish](https://github.com/satoh-y-0323/claude-code-conductor/actions/workflows/publish.yml/badge.svg)](https://github.com/satoh-y-0323/claude-code-conductor/actions/workflows/publish.yml)

**複数エージェントのオーケストレーションを中心に据えた Claude Code フレームワーク。**

---

## C3 とは

Claude Code Conductor (C3) は、Claude Code を業務開発で使うときに必要な「役割分担・ワークフロー・知識蓄積」を 1 つのフレームワークとして提供します。

```
ユーザー
    ↓ /start, /develop, /review-phase, /doc, /mcp-config, /extract-lib ...
親 Claude（オーケストレーター）
    ├─ interviewer         ← ヒアリング
    ├─ architect           ← 設計
    ├─ planner             ← タスク計画
    ├─ design-critic       ← 設計・計画の監査
    ├─ developer           ← 実装
    ├─ tester              ← テスト
    ├─ code-reviewer       ← コードレビュー
    ├─ security-reviewer   ← セキュリティレビュー
    ├─ systematic-debugger ← デバッグ調査
    └─ doc-writer          ← ドキュメント生成
```

各エージェントは明確なスコープを持ち、担当外の作業は行いません。フェーズ間の遷移・承認フロー・知識の蓄積はすべてフレームワークが管理します。

## なぜ CLAUDE.md だけでは足りないのか

Claude Code には標準で `CLAUDE.md` にプロジェクト指示を書く仕組みがあります。小規模・単発の作業ならそれで十分ですが、業務開発では以下の問題が発生します。

| 問題 | 何が起きるか |
|---|---|
| 指示が 1 ファイルに集中する | 長くなるほど Claude が全体を把握できなくなる |
| 「誰が何をするか」が分離されていない | ヒアリング・設計・実装・レビューを 1 つの Claude が兼任し、コンテキスト汚染で品質が下がる |
| ワークフローが定義されていない | 承認なしに実装が始まる |
| セッションをまたいだ記憶がない | 前回の知見が毎回リセットされる |

C3 は **役割を分離**し、**フェーズに沿った承認フロー**を提供し、**パターンをセッション間で蓄積**することでこの問題を解決します。

## 主要機能

- **13 のスキル**（`/init-session` / `/setup` / `/start` / `/develop` / `/review-phase` / `/promote-pattern` / `/pattern-status` / `/doc` / `/mcp-config` / `/extract-lib` / `/recall` / `/brainstorm` / `/codex-review`）
- **5 フェーズの開発ワークフロー**（ヒアリング → 設計 → 計画 → TDD → レビュー。計画承認後に design-critic による任意監査あり）
- **14 専門エージェント**（interviewer / architect / planner / design-critic / developer / tester / code-reviewer / security-reviewer / doc-writer / systematic-debugger / project-setup + 並列 worktree 専用の wt_developer / wt_tester / wt_systematic-debugger）
- **並列実行 (parallel-agents skill)**: plan-report を親 Claude の Agent ツール並列起動 + 公式 `isolation: "worktree"` で並列実行
- **パターン昇格システム**: 観測されたパターンを信用度スコアで管理し、`rules/promoted/` または `skills/promoted-*/` に自動昇格
- **メモリ集約 (memory-consolidation)**: 直近 7 日のドメイン知見を自動的に次セッションのコンテキストに注入
- **Tier 自動ルーティング (tier-routing)**: タスク複雑度に応じた Haiku/Sonnet/Opus の動的選択（Thompson Sampling）
- **意味検索 recall (v2.10.0+)**: `.claude/memory/sessions/`・`.claude/agent-memory/`・`.claude/reports/archive/`・`.claude/memory/patterns.json` から類似情報を numpy ベクトル検索 + 多言語 embedding (`paraphrase-multilingual-MiniLM-L12-v2`) で意味検索。`UserPromptSubmit` hook で親 Claude のコンテキストに「現タスクと無関係なら無視」前置き付きで自動注入

## 次に読むページ

- [はじめに](getting-started.md) — インストールから最初のセッションまで
- [スキル一覧](skills.md) — 全スキルの概要
- [CLI リファレンス](cli-reference.md) — `c3` コマンドの一覧

## リンク

- **GitHub**: [satoh-y-0323/claude-code-conductor](https://github.com/satoh-y-0323/claude-code-conductor)
- **PyPI**: [claude-code-conductor](https://pypi.org/project/claude-code-conductor/)
- **リリースノート**: [GitHub Releases](https://github.com/satoh-y-0323/claude-code-conductor/releases)
- **CHANGELOG**: [CHANGELOG.md](https://github.com/satoh-y-0323/claude-code-conductor/blob/main/CHANGELOG.md)
