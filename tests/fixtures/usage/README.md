# tests/fixtures/usage — フィクスチャ匿名化ポリシー

本ディレクトリのフィクスチャは **匿名化済み** データのみを含む。

## 禁止事項

以下の情報を本ディレクトリのファイルに含めないこと（SR-K-003 対応）:

- 実プロジェクト名・実ユーザー名
- 認証情報（APIキー・トークン・パスワード等）
- 実際のセッション ID（架空 UUID を使用すること）
- 会話本文・プロンプト内容など実作業に由来する文字列

## 現在のフィクスチャ

- `mainline.jsonl` — 架空 UUID・架空モデル ID・架空トークン数のみ
- `subagents/agent-deadbeef.jsonl` — 同上
- `subagents/agent-deadbeef.meta.json` — agentType / description は汎用的な文字列のみ
