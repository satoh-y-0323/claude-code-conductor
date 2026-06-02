# コントリビューションガイド

Claude Code Conductor (C3) への貢献に興味を持っていただきありがとうございます。
バグ報告・機能提案・Pull Request、いずれも歓迎します。

> Issue / PR は **日本語・英語どちらでも構いません**（Feel free to write issues and PRs in English or Japanese）。

---

## 開発環境のセットアップ

- Python **3.10 以上**
- リポジトリを clone し、editable install:

```bash
git clone https://github.com/satoh-y-0323/claude-code-conductor.git
cd claude-code-conductor
pip install -e .
```

- テストの実行:

```bash
python -m pytest
```

新しい機能や修正には、原則として**テストを添えて**ください（`tests/` 配下）。

---

## リポジトリ構造で特に注意してほしいこと

C3 は「`.claude/` を正本（canonical source）とし、そこから配布物・各プラットフォーム向け
adapter を生成する」という構造になっています。以下は貢献時にハマりやすいポイントです。

### 1. `src/c3/_template/` は直接編集しないでください

`src/c3/_template/` は `hatch_build.py` がビルド時に **`.claude/` から自動再生成**する
配布物の実体です。直接編集してもビルド時に上書きされて消えます。
**テンプレートの内容（skills / agents / hooks など）を変えたい場合は `.claude/` 側を編集**してください。

### 2. adapter は派生生成物です

`AGENTS.md` / `.codex/` / `.cursor/` / `.opencode/` などの adapter 生成物は
`src/c3/adapters.py` が `.claude/` から生成します。これらを手で編集するのではなく、
生成ロジック（`adapters.py`）か元の `.claude/` を変更してください。

### 3. 除外パターンの 3 ファイル同期

配布対象の除外/保持ルールを変更する場合、以下の 3 つを必ず揃えてください
（ビルドフックは package import 前に走るため定義が重複しています）:

- `.gitignore`
- `src/c3/_excludes.py`（`EXCLUDE_PATTERNS` / `KEEP_PATTERNS`）
- `hatch_build.py`（同上）

---

## Pull Request の流れ

1. Issue で先に相談いただけると、方針のすり合わせがスムーズです（小さな修正は不要）。
2. ブランチを切って変更し、`python -m pytest` が**全て green** であることを確認してください。
3. PR テンプレートのチェックリストに沿って説明を記入してください。
4. コミットメッセージは Conventional Commits 風（`feat:` / `fix:` / `docs:` / `refactor:` / `test:` など）を推奨します。

### リリース・バージョン・CHANGELOG について

リリース作業（バージョン更新・`CHANGELOG.md` の記載・タグ付け・PyPI 公開）は
**メンテナが行います**。貢献者の PR では原則として `CHANGELOG.md` や
`src/c3/__init__.py` のバージョンを変更する必要はありません（メンテナがリリース時にまとめます）。

---

## 行動規範

このプロジェクトには [行動規範](CODE_OF_CONDUCT.md) があります。参加にあたってご一読ください。

## セキュリティ

脆弱性を見つけた場合は、公開 Issue ではなく [セキュリティポリシー](SECURITY.md) の手順で報告してください。
