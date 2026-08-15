"""3 ファイル同期（``.gitignore`` / ``_excludes.py`` / ``pyproject.toml``）の検出器.

``tests/test_three_file_sync.py`` の被テスト実装。pytest には収集されない
（ファイル名が ``test_`` で始まらない）。先例: ``tests/_pre_compact_helpers.py``。

設計の正本: architecture-report-20260815-164137.md（改訂 5・最終）
           + requirements-report-20260815-164117.md（改訂 3）。
戻り値・型・カテゴリ語彙の凍結は ``tests/test_three_file_sync.py`` 冒頭の
設計判断メモ（tester が正本）に従う。

本モジュールの関数はすべて**引数注入の純粋関数**である: ファイル I/O をせず、
``git`` を呼ばず、``c3._excludes`` を import しない。実物（root ``.gitignore``・
``pyproject.toml``・``c3._excludes``・``git check-ignore`` の実挙動）の供給は
呼び出し側（テストモジュール）の責務であり、判定関数（``check_ignore_fn`` /
``should_skip_fn``）と導出関数（``derive_probe`` / ``normalize``）は引数として
注入される。これにより負の対照（同期漏れ・do-nothing スタブ）を実ファイルを
変異させずに実行できる（ADR-4）。

戻り値は**全判定レコード列**（``Verdict``）であり、違反専用の戻り値ではない。
関数名は ``find_*_violations`` のままだが、**違反の取得は結果フィルタ経由に限る**
（``assert not find_...(...)`` の形は誤り・ADR-9 再改訂 / DC-AM-004）。
許容リストに当たった対象も ``allowlisted=True`` のレコードとして**残す**
（残さない実装は契約違反）。導出関数が ``None`` を返した対象にはレコードを作らない。

--------------------------------------------------------------------------
docstring 記載義務 ①: 辺 1 の測定射程
--------------------------------------------------------------------------
辺 1 が測るのは「root ``.gitignore`` の意図と ``_excludes.py`` の意図の一致」であり、
配布元リポジトリでの git の最終判定ではない（実挙動は root と ``.claude/.gitignore``
〔配布物・ネストが深い側が優先〕の合成で決まる）。合成状態での判定を課すのは
**KEEP の not-ignored だけ**であり、EXCLUDE 側には合成判定を課さない
（deeper 側が広く除外していても root の意図一致とは独立のため）。
``.claude/.gitignore`` は**検証対象ではなく合成判定の入力**である
（requirements 改訂 2 §4 但し書き）。
判定は**ユーザー全体設定（``core.excludesFile``）と init テンプレート由来
（``.git/info/exclude``）の両方を排して隔離**した一時リポジトリで行う
（呼び出し側が ``git -c core.excludesFile= check-ignore`` の形＋``git init`` 直後の
``.git/info/exclude`` 空上書きで隔離する・DC-AS-002）。

--------------------------------------------------------------------------
docstring 記載義務 ⑤: 件数運用
--------------------------------------------------------------------------
件数の正本は ``tests/test_three_file_sync.py`` の件数定数群。増減いずれの場合も
理由文字列とセットでレビューする（DC-AM-003 / ADR-10）。実効検査件数は
「当該由来カテゴリかつ許容リスト適用 False のレコード件数」、許容リスト件数は
「適用 True のレコード件数」で数える。更新せずに実データだけ変えると赤になるのは
仕様（黙って通さないための番兵）である。

--------------------------------------------------------------------------
docstring 記載義務 ⑦: docs/spec 凍結乖離の記録
--------------------------------------------------------------------------
``docs/spec/06-distribution.md:67, 332-348``・``00-index.md:125`` は本検査の導入で
失効するが 2026-08-05 凍結裁定により意図的に未更新（本文を編集しないのが正・ADR-11）。
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Container, Iterable
from typing import NamedTuple

# プローブ名（literal 確定・DC-GP-005）。グロブ ``*`` とディレクトリ形の 1 段補いに使う。
PROBE_NAME = "__sync_probe__"
CLAUDE_PREFIX = ".claude/"

# 由来カテゴリの語彙 8 種（architecture 改訂 5 §3）
CATEGORY_EXCLUDE = "EXCLUDE"
CATEGORY_KEEP = "KEEP"
CATEGORY_KEEP_COMPOSITE = "合成KEEP"
CATEGORY_CLAUDE_LINE = "claude行"
CATEGORY_UNIVERSAL = "全体パターン"
CATEGORY_EDGE2_RESCUE = "辺2救済"
CATEGORY_EDGE2_CLASSIFY = "辺2分類"
CATEGORY_EDGE3 = "辺3"


class Verdict(NamedTuple):
    """判定レコード（必須 5 フィールド・ADR-9 再改訂）.

    - ``subject``: 対象（プローブ相対パスまたは force-include キー）
    - ``expected``: 課した期待
    - ``actual``: 判定結果
    - ``allowlisted``: 許容リスト適用の有無
    - ``category``: 由来カテゴリ（本モジュールの ``CATEGORY_*`` 8 種のいずれか）

    違反は ``actual != expected and not allowlisted``。この判定は呼び出し側の
    結果フィルタで行う（本モジュールは違反専用の戻り値を持たない）。
    """

    subject: str
    expected: bool
    actual: bool
    allowlisted: bool
    category: str


# ---------------------------------------------------------------------------
# プローブ導出（P1）
# ---------------------------------------------------------------------------


def _replace_glob_with_probe(s: str) -> str:
    """グロブ ``*`` をプローブ名 ``__sync_probe__`` へ置換する（導出 3 関数の共通部分）.

    切り出しの射程は**グロブ置換のみ**であり、``.claude/`` プレフィックスの付与・剥離は
    各導出関数に残す（方向〔付与 / 剥離〕も戻り値型も関数ごとに異なるため・CR-M-001）。
    公開 API・挙動は不変。
    """
    return s.replace("*", PROBE_NAME)


def derive_probe_from_pattern(pattern: str) -> str | None:
    """``EXCLUDE_PATTERNS`` / ``KEEP_PATTERNS`` のパターン → プローブパス.

    グロブ ``*`` はプローブ名 ``__sync_probe__`` へ置換し、リテラルはそのまま、
    先頭に ``.claude/`` を付す（例: ``reports/*`` →
    ``.claude/reports/__sync_probe__``）。プローブの実体は作らない
    （``git check-ignore`` はパス実在を要求しない・ADR-6。D-1 で実測固定済み）。
    """
    return CLAUDE_PREFIX + _replace_glob_with_probe(pattern)


def derive_probe_from_gitignore_line(line: str) -> tuple[str, bool] | None:
    """root ``.gitignore`` の 1 行 → ``(プローブ相対パス, 否定行か)``.

    戻り値は ``tuple[str, bool] | None``。**コメント / 空行 / ``.claude/`` 外の行は
    ``None``**。否定行（``!.claude/...``）は ``(プローブ, True)`` を返す
    （改訂 1 §2-1 手順 1 の「対象外」列挙に含まれる否定行は**抽出段階**の話であり、
    本関数の戻り値契約ではない・DC-AM-002）。

    プローブは ``.claude/`` プレフィックスを剥がし、ディレクトリ形（末尾 ``/``）は
    プローブ名を 1 段補い、``dir/*`` 形はグロブをプローブ名へ置換し、リテラルは
    そのままとする（例: ``.claude/agent-memory/`` → ``agent-memory/__sync_probe__``・
    ``.claude/memory/patterns.json`` → ``memory/patterns.json``）。
    """
    entry = line.strip()
    if not entry or entry.startswith("#"):
        return None
    negated = entry.startswith("!")
    if negated:
        entry = entry[1:]
    if not entry.startswith(CLAUDE_PREFIX):
        return None
    rel = entry[len(CLAUDE_PREFIX) :]
    if not rel:
        return None
    if rel.endswith("/"):
        rel += PROBE_NAME
    else:
        rel = _replace_glob_with_probe(rel)
    return rel, negated


def _derive_probe_from_sdist_entry(entry: str) -> str | None:
    """正規化済み sdist exclude エントリ → プローブ相対パス（辺 3 の内部既定）.

    ``.claude/`` 外のエントリは ``None``（辺 3 の射程は ``.claude/`` エントリのみ）。
    正規化でディレクトリ形は ``dir/*`` になっているため、ここではグロブ置換のみで
    方向 B と同じ「1 段補い」の結果になる。
    """
    if not entry.startswith(CLAUDE_PREFIX):
        return None
    rel = entry[len(CLAUDE_PREFIX) :]
    if not rel:
        return None
    return _replace_glob_with_probe(rel)


# ---------------------------------------------------------------------------
# 正規化（ADR-8 改訂）
# ---------------------------------------------------------------------------


def normalize_sdist_exclude_entry(entry: str) -> str:
    """sdist exclude エントリを判定形式へ正規化する（辺 2・辺 3 共通）.

    受理して正規化する形式は 3 つ:

    - リテラル（グロブなし）… そのまま
    - ``dir/*`` 形（末尾セグメントが ``*`` のみ）… そのまま
    - ディレクトリ形 ``dir/`` … ``dir/*`` 相当へ正規化する

    docstring 記載義務 ⑥（正規化関数と辺 3 導出の整合）: **ディレクトリ形は辺 2 の
    正規化と辺 3 のプローブ導出（方向 B と同じ規則）で同じ 1 段補いになる**
    （``dir/`` → ``dir/*`` → ``dir/__sync_probe__``）。

    上記 3 形に該当しないもの（``**`` を含む・先頭 ``/``・``!`` 否定・空文字・
    その他）は**正規化規則を持たない形式**として fail-loud で ``ValueError`` を
    送出する（サイレント近似を許さない＝検査の想定更新を要求する）。
    """
    if not isinstance(entry, str) or not entry:
        raise ValueError(f"sdist exclude エントリが空または文字列でない: {entry!r}")
    if "**" in entry:
        raise ValueError(f"正規化規則を持たない形式（`**` を含む）: {entry!r}")
    if entry.startswith("/"):
        raise ValueError(f"正規化規則を持たない形式（先頭 `/`）: {entry!r}")
    if entry.startswith("!"):
        raise ValueError(f"正規化規則を持たない形式（`!` 否定）: {entry!r}")
    if entry.endswith("/"):
        if "*" in entry:
            raise ValueError(f"正規化規則を持たない形式（ディレクトリ形にグロブ）: {entry!r}")
        return entry + "*"
    if "*" not in entry:
        return entry
    if entry.endswith("/*") and "*" not in entry[:-1]:
        return entry
    raise ValueError(f"正規化規則を持たない形式（想定外のグロブ）: {entry!r}")


# ---------------------------------------------------------------------------
# 辺 1 方向 A（+ P2b 合成 KEEP / P2c 全体パターン git 側の再利用）
# ---------------------------------------------------------------------------


def find_gitignore_intent_violations(
    exclude_patterns: Iterable[str],
    keep_patterns: Iterable[str],
    check_ignore_fn: Callable[[str], bool],
    allowlist_a: Container[str],
    *,
    derive_probe: Callable[[str], str | None] = derive_probe_from_pattern,
    category_exclude: str = CATEGORY_EXCLUDE,
    category_keep: str = CATEGORY_KEEP,
) -> list[Verdict]:
    """辺 1 方向 A（``_excludes.py`` の意図 → git が同じ扱いをするか）.

    EXCLUDE パターンのプローブは ignored 期待（``expected=True``）、KEEP パターンの
    プローブは not-ignored 期待（``expected=False``）。``allowlist_a`` に載っている
    **パターン**から導いたレコードは ``allowlisted=True`` になり、期待に反していても
    違反にならない（レコード自体は残す）。

    ``check_ignore_fn`` の判定契約（ADR-6）: 呼び出し側は ``git check-ignore`` の
    returncode 0 を ignored（True）・1 を not ignored（False）とし、それ以外
    （128 等）は検証不能として fail-loud（例外送出）にする。本関数はその真偽値を
    そのままレコードの ``actual`` にする。

    **測定射程（記載義務 ①）**: 測るのは root ``.gitignore`` の意図と
    ``_excludes.py`` の意図の一致であり、配布元での git の最終判定ではない。
    ``.claude/.gitignore`` は検証対象ではなく、KEEP 合成判定（P2b）の**入力**として
    のみ用いる（requirements 改訂 2 §4 但し書き）。合成判定を課すのは KEEP の
    not-ignored だけで EXCLUDE には課さない。判定はユーザー全体設定
    （``core.excludesFile``）と init テンプレート由来（``.git/info/exclude``）の
    両方を排して隔離した一時リポジトリで行う。

    **KEEP の VCS ignore 救済を認めない禁止規範（記載義務 ②・ADR-7 再改訂）**:
    KEEP は VCS ignore で落ちてはならない。``force-include`` による KEEP の救済は
    認めない（git 履歴から消える脆い救済になるため）。KEEP を ignore している
    ``.gitignore`` は root と ``.claude/.gitignore`` のどちらでもありうる（git は
    よりネストの深い側を優先し root から取り消せない）ため、**是正は ignore の
    出所側の否定行で戻す**。出所が ``.claude/.gitignore``（配布物）の場合、その編集は
    本スライスのスコープ外＝debug-needed で申告し別スライスで扱う。
    なお辺 2 逆方向の分類名「KEEP 救済」は **sdist exclude 経路の救済**を指す語であり、
    ここで禁止している「VCS ignore で落ちた KEEP の force-include 救済」とは別物である
    （2 検査の判定が割れないための語彙の書き分け）。

    P2b（KEEP 合成判定）と P2c（全体パターン git 側）は、``exclude_patterns`` /
    ``keep_patterns`` / ``category_exclude`` / ``category_keep`` / ``check_ignore_fn``
    を差し替えた**別呼び出し**として本関数を再利用する（新関数を作らない）。
    """
    records: list[Verdict] = []
    for patterns, expected, category in (
        (exclude_patterns, True, category_exclude),
        (keep_patterns, False, category_keep),
    ):
        for pattern in patterns:
            probe = derive_probe(pattern)
            if probe is None:
                continue
            records.append(
                Verdict(
                    subject=probe,
                    expected=expected,
                    actual=bool(check_ignore_fn(probe)),
                    allowlisted=pattern in allowlist_a,
                    category=category,
                )
            )
    return records


# ---------------------------------------------------------------------------
# 辺 1 方向 B
# ---------------------------------------------------------------------------


def find_gitignore_line_violations(
    gitignore_lines: Iterable[str],
    should_skip_fn: Callable[[str], bool],
    allowlist_b: Container[str],
    *,
    derive_probe: Callable[[str], tuple[str, bool] | None] = derive_probe_from_gitignore_line,
    category: str = CATEGORY_CLAUDE_LINE,
) -> list[Verdict]:
    """辺 1 方向 B（root ``.gitignore`` の ``.claude/`` 行 → ``should_skip`` の一致）.

    非否定行のプローブは ``should_skip`` True 期待（git が個人状態として捨てるものは
    wheel からも落ちる）、否定行のプローブは False 期待（KEEP 対応の確認）。
    ``allowlist_b`` に載っている**プローブ**のレコードは ``allowlisted=True`` になり、
    期待に反していても違反にならない（レコード自体は残す）。

    **限界（記載義務 ④）**: 本検査が測るのは ``.claude/`` 配下の行のみである。
    リポジトリ全体パターンについては、``should_skip`` が特別分岐で扱う 3 種
    （``__pycache__`` / ``*.pyc`` / ``*.pyo``）に対応するプローブを ``.claude/`` 配下の
    行として与えた場合の対応だけを測る。**それ以外の全体パターン**
    （``.pytest_cache/`` / ``build/`` / ``*.egg-info/`` 等）**とアンカー形は射程外**で
    あり、``should_skip`` に対応分岐が無いことと併せてここに限界として記す。
    """
    records: list[Verdict] = []
    for line in gitignore_lines:
        derived = derive_probe(line)
        if derived is None:
            continue
        probe, negated = derived
        records.append(
            Verdict(
                subject=probe,
                expected=not negated,
                actual=bool(should_skip_fn(probe)),
                allowlisted=probe in allowlist_b,
                category=category,
            )
        )
    return records


# ---------------------------------------------------------------------------
# 辺 2（KEEP ↔ sdist force-include・双方向）
# ---------------------------------------------------------------------------


def find_force_include_violations(
    keep_patterns: Iterable[str],
    sdist_exclude: Iterable[str],
    force_include_keys: Iterable[str],
    injection_controls: Container[str],
    *,
    normalize: Callable[[str], str] = normalize_sdist_exclude_entry,
) -> list[Verdict]:
    """辺 2 双方向（``KEEP_PATTERNS`` ↔ ``pyproject.toml`` の sdist force-include）.

    - 救済要求（``辺2救済``）: ``.claude/`` を付した KEEP パスが正規化後の sdist
      ``exclude`` のいずれかに一致する場合、その ``.claude/`` 付きリテラルパスが
      ``force-include`` のキーに**存在すること**を要求する（sdist 経由の wheel から
      KEEP が欠落する過去 defect の型）。sdist exclude 配下でない KEEP は対象外＝
      レコードを作らない。
    - 分類要求（``辺2分類``）: ``force-include`` の各キーは「KEEP 救済（KEEP パターンに
      一致）」または「注入対照」に分類できること（用途不明キーの増殖防止）。
      ``injection_controls`` に載っているキーは ``allowlisted=True`` となり、KEEP に
      一致しなくても違反にならない。

    ここでの「KEEP 救済」は **sdist exclude 経路の救済**を指す（VCS ignore で落ちた
    KEEP を force-include で救うことは辺 1 の禁止規範で認めていない・記載義務 ②）。

    本検出器は判定関数を注入されない（キー存在の照合のみ）。計数の seam は戻り値が
    レコード列であることにより成立するため、P7(e)/(f) のスタブ反転の対象外である。
    """
    normalized_exclude = [normalize(entry) for entry in sdist_exclude]
    keep_paths = [CLAUDE_PREFIX + pattern for pattern in keep_patterns]
    keys = list(force_include_keys)

    records: list[Verdict] = []
    for keep_path in keep_paths:
        if not any(fnmatch.fnmatchcase(keep_path, pat) for pat in normalized_exclude):
            continue
        records.append(
            Verdict(
                subject=keep_path,
                expected=True,
                actual=keep_path in keys,
                allowlisted=False,
                category=CATEGORY_EDGE2_RESCUE,
            )
        )
    for key in keys:
        records.append(
            Verdict(
                subject=key,
                expected=True,
                actual=any(fnmatch.fnmatchcase(key, pat) for pat in keep_paths),
                allowlisted=key in injection_controls,
                category=CATEGORY_EDGE2_CLASSIFY,
            )
        )
    return records


# ---------------------------------------------------------------------------
# 辺 3（sdist exclude → EXCLUDE の意図・片方向）
# ---------------------------------------------------------------------------


def find_sdist_exclude_violations(
    sdist_exclude: Iterable[str],
    should_skip_fn: Callable[[str], bool],
    *,
    normalize: Callable[[str], str] = normalize_sdist_exclude_entry,
    derive_probe: Callable[[str], str | None] = _derive_probe_from_sdist_entry,
) -> list[Verdict]:
    """辺 3 片方向（sdist ``exclude`` の ``.claude/`` エントリ → ``should_skip`` True 期待）.

    sdist が意図的に落とすものは ``EXCLUDE_PATTERNS`` の意図と整合していること
    （``should_skip`` が True であること）を要求する。

    **逆方向は要求しない（非対称の根拠・記載義務 ③・改訂 3 §2-3 の確定文面）**:
    sdist exclude はローカル作業ファイル対策の部分列であり網羅を要求しない。この非網羅は他のどの検査でも代替されない（`scripts/verify_wheel.py` の sdist 検査は sdist に実体として入った EXCLUDE 対象の混入のみを見る。tracked / 非 ignore の未追跡作業ファイルの双方が対象で、clean checkout での実効候補は注入対照 1 件）
    """
    records: list[Verdict] = []
    for entry in sdist_exclude:
        probe = derive_probe(normalize(entry))
        if probe is None:
            continue
        records.append(
            Verdict(
                subject=probe,
                expected=True,
                actual=bool(should_skip_fn(probe)),
                allowlisted=False,
                category=CATEGORY_EDGE3,
            )
        )
    return records
