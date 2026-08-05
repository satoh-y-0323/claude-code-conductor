# 参照抽出器（refgraph）の契約

- **確定日**: 2026-08-05
- **対象**: `src/c3/refgraph.py`（配布物・wheel 収録）
- **位置づけ**: 配布元専用の内部資料（`docs/` は wheel にも sdist にも収録されない）
- **旧文書**: `.dev/refgraph-benchmark-20260805.md` は**本契約が置き換える**。旧文書の受け入れ条件（到達可能性の期待値）は §9 の理由で失効した

---

## 1. この道具は何か

**C3 のファイル間・ファイルとシンボル間に実在する参照関係を、種類と出所つきで漏れなく抽出し、ファイルへ出力する。**

判定はしない。削除してよいか・生きているか・呼ばれているかは、**出力を読む側**（人間または LLM、あるいは別のクエリ）が決める。

### 1-1. 何のためか

C3 の負債棚卸しにおける **削除判定の補助**。削除判定そのものではない。

「この関係が取れているから削除できない」という含意は**持たせない**。`CHANGELOG.md` がある agent を言及していることは真の関係であり、それをもって削除不可とは決まらない。決めるのは読む側。

### 1-2. 非目的（やらないこと）

| やらないこと | 理由 |
|---|---|
| 到達可能性の真偽判定を抽出器が行う | 判定をルート集合の選び方に依存させると、抽出器の中に判断が焼き付く（§9 の失敗） |
| 「起動」と「言及」のどちらかを抽出段階で捨てる | 両方とも実在する関係。**種類として区別して両方記録する** |
| 出所（`CHANGELOG` / `_template/` / `.dev/` / `tmp/` 等）によるフィルタ | フィルタは読む側の仕事。抽出段階で落とすと、落としたことに気づけない |
| 削除可否・死活の結論を出す | 上記のとおり |

---

## 2. 収集方針（3 原則）

### 原則 1: 落とさない

抽出段階でファイル・ディレクトリ・出所によるフィルタを**しない**。
`CHANGELOG.md` / `src/c3/_template/` / `.dev/` / `.claude/tmp/` / `.claude/reports/` すべて走査対象。

> **理由（非対称性）**: 採りすぎは読む側で絞れる。採り漏らしは**漏れていることに気づけない**。
> 「このパターンが漏れていた」「この条件で対象外にしていた」と後から判明する手戻りのほうが、
> ノイズを読み飛ばすコストより高い。

### 原則 2: 出所を残す

すべての関係に `source` / `source_line` / `context`（該当行の抜粋）を付ける。
**出所があるから読む側が絞れる。** 出所の無い関係は、絞れないので価値が半分になる。

### 原則 3: 拾えなかったものを残す

読めない・解決できない・曖昧だったものを出力に含める。**沈黙しない。**

- 読めなかったファイル → `skipped` に記録
- 参照先が実在しない → 辺は**出す**うえで `target_exists: false` を立てる
  （例: `.claude/tmp/` の古いマニフェストが削除済み `tdd-develop.md` を指す。
  これは「消したはずのものを参照している残骸がある」という有用な信号であり、捨てない）

---

## 3. 出力（ファイルであること）

`networkx` の node-link 形式に倣う。graphify の `graph.json` と同じ型（`.dev/pivot-20260805.md` §9-1 に構造を実測記録済み）。

```json
{
  "schema_version": 1,
  "root": "<抽出したリポジトリルートの絶対パス>",
  "generated_from": {"file_count": 1234},
  "nodes": [
    {"id": ".claude/hooks/stop.py", "kind": "file", "exists": true}
  ],
  "links": [
    {
      "relation": "py_importlib",
      "source": ".claude/hooks/session_stop.py",
      "source_line": 80,
      "context": "        stop_module = _load_module(\"stop\")",
      "target": ".claude/hooks/stop.py",
      "target_exists": true,
      "resolution": "exact"
    }
  ],
  "skipped": [
    {"path": ".claude/skills/x/SKILL.md", "reason": "UnicodeDecodeError"}
  ]
}
```

### ノード ID

- ファイル: リポジトリルート相対の **POSIX** パス（`.claude/hooks/stop.py`）
- DB テーブル: `sqltable:<name>`

`skipped` のパスも**同じ規約**に従う（区切りが混ざると読む側で突合できない）。

### `resolution` の値

| 値 | 意味 |
|---|---|
| `exact` | パスが一意に解決した |
| `basename` | **名前で解決した**（パスの厳密一致ではない）。次の 2 形を含む: ①ファイル名のみの参照（例: SKILL.md 内の `record_agent_outcome.py`）②**パス末尾の部分一致**（例: `dev-workflow/SKILL.md` → `.claude/skills/dev-workflow/SKILL.md`）。どちらも候補が一意のときだけこの値になる |
| `ambiguous` | 候補が複数あった。**候補ごとに 1 本ずつ辺を出す**（どれかを選ばない） |
| `missing` | 参照先が実在しない（辺は出す・`target_exists: false`） |

### 解決の順序（2026-08-05 追記）

パス形の参照は次の順で解決する。**先に一致した時点で確定する。**

1. ルート相対で実在 → `exact`
2. source 相対で実在 → `exact`
3. **リポジトリ内の実在ファイルのうち、パス末尾が `/<参照>` に一致するもの**
   → 一意なら `basename` / 複数なら `ambiguous`（候補ごとに 1 本）
4. どれにも当たらない → `missing`

> **3 を欠くと実在するものを `missing` と報告する。** C3 の文書は
> `dev-workflow/SKILL.md` `rules/promoted/index.md` のような**部分パス**で書く癖があり、
> 3 が無い実装では上位 20 件だけで 331 件が誤って `missing` になった（2026-08-05 実測）。
> 読む側が「残骸だ」と誤読する経路になる。

**末尾一致は `/` 区切りの成分境界で行う。** `skills/dev-workflow/SKILL.md` のような
2 段以上の部分パスは一致するが、`workflow/SKILL.md`（成分の途中から始まる形）は一致させない。

> **読む側への注意**: 3 は `ambiguous` を増やす（本リポジトリでは 22,845 → 23,538・2026-08-05 実測）。
> `src/c3/_template/` が `.claude/` の複製なので、部分パスはほぼ 2 候補になるためである。
> **`ambiguous` の複数辺は「どれか 1 つが実際の参照先」であって「全部を参照している」ではない。**
> 抽出器はどれかを選ばない（選べる情報が無い）ので、選ぶのは読む側の仕事。

### トークン境界（2026-08-05 追記）

**参照トークンは境界から始まったものだけを採る。** 許容集合外の文字（日本語・`$(`・`<>` 等）で
トークンが途切れたとき、その**続きを独立した参照として採ってはならない**。

| 元テキスト | 誤（断片） | 期待 |
|---|---|---|
| `` `.claude/reports/doc-{名前}-{timestamp}.md` `` | `-{timestamp}.md` | 断片を出さない |
| `e0-targets-$(date +%s)-$$-$RANDOM.txt` | `-$$-$RANDOM.txt` | 同上 |
| `` `.claude/skills/<skill>/templates/<name>-template.md` `` | `-template.md` | 同上 |

> これは「採りすぎ」ではなく**実在しない参照先の捏造**であり、原則 1 の射程外。
> 全体の 6%（255/4017 の missing 辺）で発生していた（2026-08-05 実測）。
> トークン全体を `missing` として出すか、何も出さないかは実装の裁量とする。

#### 境界として**許す側**の列挙が本体（逆方向の落とし穴）

上表は「区切ってはいけない文字」の例だが、**実装で効くのは区切りとして許す側の集合**である。
ここを ASCII だけで組むと、**正当な参照を落とす**逆方向の事故が起きる。

- **非 ASCII の句読点・空白は区切りとして扱う**（`。` `、` `（` `・` 等）。
  ASCII だけの allowlist で組んだ実装は、`…という表示になる。SKILL.md の引用では…` の
  `SKILL.md` を拾えず、**正当な参照 39 件を落とした**（2026-08-05 実測）。
  `unicodedata.category` の先頭が `P`（句読点）/ `Z`（空白）なら区切り、
  漢字・かなは区切りでない、という判定で解消した
- **`*` は二役**。Markdown の強調（`**stop.py**`）では区切りだが、
  glob（`reports/*-{timestamp}.md`）ではパスの途中。**直前 1 文字で役割を決める**
  （直前がパス構成文字ならグロブ＝非境界）。実測: 一律で非境界にすると強調記法の参照を
  **273 件**失い、一律で境界にするとグロブ断片が **10 件**残る。条件付きで両立する

> **検出手段**: この 39 件はテストでは出ず、**A/B 比較**（修正前にあって修正後に無い、かつ
> 解決済みだった辺を数える）で初めて出た。境界規則を触ったら A/B を取ること。

> **自己言及について**: 本節の表 2 列目にある `` `-$$-$RANDOM.txt` `` は
> コードスパン内の正当なトークンなので、本文書自身が `missing` ノードを 1 件生む。
> 回避不能であり、実装の欠陥ではない。

### `context`

該当行の抜粋。**制御文字・改行を除去し、長さ上限で切る**（外部由来文字列をそのまま埋め込まない）。

---

## 4. 関係の種類（relation）

**すべて記録する。落とすものは無い。** 種類が違うだけ。

| relation | 抽出元 | 解決先 |
|---|---|---|
| `settings_hook` | `settings*.json` の `hooks` セクションの command / args | `.py` |
| `settings_statusline` | `settings*.json` の `statusLine` | `.py` |
| `settings_permission` | `settings*.json` の `permissions`（allow/deny 等） | `.py` ほか |
| `md_code_span_path` | md のコードスパン内のパス | ファイル |
| `md_link` | md のマークダウンリンク `[x](path)` | ファイル |
| `md_c3_run` | md の `c3 run <path>` | `.py` |
| `md_agent_variant_map` | md の表の行 `\| x \| wt_x \|` | `.claude/agents/wt_x.md` |
| `md_subagent_type` | md の `subagent_type: <name>` / `agent: <name>` | `.claude/agents/<name>.md` |
| `md_bare_agent_name` | md 本文中の agent 名（パスでない素の名前） | `.claude/agents/<name>.md` |
| `md_bare_skill_name` | md 本文中の skill 名 / `/<skill>` | `.claude/skills/<name>/SKILL.md` |
| `py_import` | py の import 文（**AST で解析**） | モジュール実体 |
| `py_importlib` | py の動的ロード（`_load_module("name")` 等） | 同ディレクトリの `name.py` |
| `py_subprocess_path` | py の subprocess で組み立てるスクリプトパス | `.py` |
| `py_sql_table` | py / sql の SQL 文字列中のテーブル名 | `sqltable:<name>` |

### 設計上の要点

- **`settings_permission` を独立の種類として記録する。** 以前の契約はこれを「起動ではない」として
  辺にしなかったが、`"Bash(c3 run .claude/hooks/stop.py*)"` は実在する関係である。
  種類で区別できるので、読む側が「起動だけ見たい」なら `settings_hook` に絞ればよい
- **`md_bare_agent_name` を新設する。** C3 の agent は親 Claude が**散文の指示**を読んで起動するため、
  パス形式の参照が存在しない。実測では `security-reviewer.md` への辺が **0 本**だった。
  「`security-reviewer` という語が SKILL.md の N 行目にある」は実在する関係であり、記録する
- **`py_import` は AST で解析する。** 行単位の正規表現では
  `from c3 import (\n    cli_ask,\n    ...)` のような複数行括弧付き import を取りこぼす
  （`src/c3/cli.py:20` が実例）。標準ライブラリの `ast` を使う。
  外部依存（graphify 等）は配布 wheel の実行時依存になるため入れない

---

## 5. API

```python
build_graph(root) -> Graph          # 抽出。ルート集合を持たない（§6 条件 5）
Graph.nodes  -> tuple[Node, ...]
Graph.links  -> tuple[Link, ...]
Graph.skipped -> tuple[Skipped, ...]
Graph.to_dict() -> dict             # §3 のスキーマ
write_graph(graph, path) -> None    # JSON をファイルへ書く
read_graph(path) -> Graph           # 書いたものを読み戻せる
```

**判定はここに置かない。** 到達可能性のような問いは、出力を読む**クエリ側**の責務とし、
ルート集合はクエリの引数として外から与える。

---

## 6. 完成条件

1. **形式カバレッジ**: §4 の各 relation について、実リポジトリで **1 本以上**の辺が出ること。
   0 本の relation があれば、その形式を取りこぼしている（または C3 に存在しない）ため、
   **どちらかを出力または文書で明示する**
2. **既知の関係が出ること**: 実リポジトリで下記が出力に含まれること
   （`.dev/pivot-20260805.md` §9 の実測を「到達可能か」でなく「**関係が出るか**」に読み替えたもの。
   **実測値は再測定しない**）

   | 関係 | 期待 |
   |---|---|
   | `settings*.json` の hooks 登録 → hook `.py` | `settings_hook` が出る |
   | `session_stop.py` の importlib → `stop.py` / `consolidate_memory.py` / `session_utils.py` / `tier_gap_check.py` | `py_importlib` が出る |
   | `permission_handler.py` の subprocess → `permission_handler_toast.py` | `py_subprocess_path` が出る |
   | `parallel-agents/SKILL.md` の写像表 → `wt_systematic-debugger.md` | `md_agent_variant_map` が出る |
   | SKILL.md の `c3 run` → skill scripts 7 本 | `md_c3_run` が出る |
   | `cli.py` の複数行 import → `cli_init.py` ほか | `py_import` が出る |
   | SKILL.md の散文 → `security-reviewer.md` | `md_bare_agent_name` が出る |
   | `settings.json` の `permissions.allow` → `stop.py` | `settings_permission` が出る（`settings_hook` **ではない**） |

3. **取りこぼしの可視化**: デコード不能なファイルが `skipped` に出ること。
   実在しない参照先が `target_exists: false` の辺として出ること
4. **出力がファイルであること**: `write_graph` → `read_graph` で往復し、同じ内容が復元できること
5. **判定を含まないこと（機械強制）**: 抽出器がルート集合・エントリポイント定義を持たないことを
   静的検査で縛る。判定が抽出器に再混入するのを防ぐ
6. **フルスイート緑**
7. **do-nothing スタブ検査**: 何も抽出しない空実装を当てて、緑になってよいのは API の型検査のみ。
   採用条件・カバレッジ検査が 1 件でも緑になったら、そのテストは空回りしている
   （**旧契約から引き継ぐ唯一の受け入れ条件**。初版では 26 件中 9 件が緑だった）

---

## 7. テストの書き方（旧契約から引き継ぐ規律）

1. **不在は `assert xs == []` で直接 assert する。** 空になりうるループの中で assert しない
2. **合成入力では参照先のファイルを実際に作る。** 作らないと「存在しない」ので必ず空になる
3. **ノード ID / skipped のパスはルート相対 POSIX**
4. **「無いこと」の検査は「在ること」の対照と同じ fixture に置く**。
   辺を 1 本も作らない実装が必ず赤になるようにする
5. **assert のないテストを書かない。** 期待が立てられないならテストを書かず、
   レポートに「言えないこと」として書く
6. **機構を足したら、同じ周回でその機構を検査するテストを足す**
   （`skipped` 機構を追加したのに検査せず、8 箇所のクラッシュを 2800 件の緑が見逃した実例がある）

---

## 8. 非スコープ / 拡張余地

| 項目 | 扱い |
|---|---|
| `c3 graph` CLI サブコマンド | **未決**。公開 CLI の拡大は外部契約の追加になるため別途裁定 |
| ノードへの配布可否（`_excludes.should_skip`）の付与 | 削除判定に効く事実だが v1 のスキーマには入れない。出力がファイルなので後から足せる |
| 関数レベルの死にコード検出 | 本契約の対象外。`src/c3/` 内部の呼び出しグラフは graphify が正確（pivot §8 実測）で、dev ツールとして外から使えば足りる |
| `confidence` スコア | v1 では `resolution` の 4 値で代替する |

---

## 9. なぜ契約を書き直したか（再発防止の記録）

初版（`.dev/refgraph-benchmark-20260805.md`）は **`is_reachable()` という真偽値 API を仕様に置き、
ルート集合を抽出器の中に固定した**。これにより判断が抽出器に焼き付き、次の 3 つが起きた。

1. **真の関係を「偽陽性」と誤って評価した。** `code-reviewer.md` が `CHANGELOG.md:3403` から
   参照されているのは実在する関係だが、固定ルートからの真偽に畳んだ結果
   「文書連鎖でしか到達しない＝偽陽性」と読んでしまった
2. **実在する関係を「汚染源」と呼んで捨てようとした。** `CHANGELOG` 585 / `_template/` 367 /
   `.dev/` 178 / `.claude/tmp/` 30 の計 1160 本（全体の 45%）。
   とくに `.claude/tmp/` → 削除済み `tdd-develop.md` は**有用な信号**だった
3. **負の対照 N-2 で「散文の言及は辺にしない」と明文化した。** これが
   `md_bare_agent_name` の欠落を招き、`security-reviewer.md` への辺が 0 本になった

**共通の誤り**: 抽出と判定を混ぜた。**抽出は網羅、判定は後段**が正しい分離であり、
それを保証するのが §6 条件 5（判定を含まないことの機械強制）である。
