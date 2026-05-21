# Setup Skill 参照表

`project-setup` agent が `coding-standards-template.md` のプレースホルダを埋めるときに使う参照データ。

---

## 言語 → ファイル拡張子 glob マッピング

`coding-standards.md` の `paths` フロントマターに使う glob パターンの参照表。
複数言語の場合は全行を結合する。

| 言語 | `paths` glob 行 |
|---|---|
| Python | `  - "**/*.py"` |
| TypeScript | `  - "**/*.ts"`<br>`  - "**/*.tsx"` |
| JavaScript | `  - "**/*.js"`<br>`  - "**/*.jsx"`<br>`  - "**/*.mjs"`<br>`  - "**/*.cjs"` |
| TypeScript + JavaScript | 上記すべて |
| Go | `  - "**/*.go"` |
| Java | `  - "**/*.java"` |
| Kotlin | `  - "**/*.kt"`<br>`  - "**/*.kts"` |
| C# | `  - "**/*.cs"` |
| Rust | `  - "**/*.rs"` |
| Ruby | `  - "**/*.rb"` |
| PHP | `  - "**/*.php"` |
| Swift | `  - "**/*.swift"` |

> **使い方**: `{LANG_PATHS}` プレースホルダに、選択した言語の行を YAML リスト形式で埋め込む。
> 例: Python + TypeScript の場合は以下を `{LANG_PATHS}` の位置に挿入する:
> ```yaml
>   - "**/*.py"
>   - "**/*.ts"
>   - "**/*.tsx"
> ```

---

## 公式スタイルガイド参照先（WebSearch / WebFetch のヒント）

| 言語 / フレームワーク | 主要参照先 |
|---|---|
| Python | PEP 8 / Google Python Style Guide |
| TypeScript | TypeScript ESLint / Google TypeScript Style |
| JavaScript | StandardJS / Airbnb JavaScript Style Guide |
| Go | Effective Go / Uber Go Style Guide |
| Java | Google Java Style Guide / Oracle Code Conventions |
| Rust | Rust API Guidelines / rustfmt default |
| Ruby | Ruby Style Guide (rubocop) |
| React | React docs / Airbnb React Style |
| Vue | Vue.js Style Guide |
| Django | Django Coding Style |
| Rails | Rails Style Guide |

セキュリティガイドラインは OWASP Top 10 / CWE Top 25 を共通参照とする。

テストフレームワークのベストプラクティス:

- pytest（Python）, jest / vitest（JS/TS）, JUnit / TestNG（Java）, RSpec（Ruby）, Go testing パッケージ
