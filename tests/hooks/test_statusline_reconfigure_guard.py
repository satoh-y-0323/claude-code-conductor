"""[FR-3] .claude/hooks/statusline.py の reconfigure 保護に関する性質テスト。

測るべき性質:
    statusline.py はモジュールレベルで sys.stdout / sys.stderr / sys.stdin の
    ``reconfigure(encoding='utf-8')`` を呼ぶ。``reconfigure`` 属性を持たない
    ストリームオブジェクトが差し込まれている環境（pytest の ``DontReadFromInput``、
    パイプ・ラッパ経由で差し替えられた擬似ストリーム等）でも、
    **モジュールのロードが AttributeError でクラッシュしてはならない**。
    すなわち CLAUDE.md §9-3 の canonical idiom
    （``try: ... except AttributeError: pass``）による保護が入っていること。

Red の正体:
    無保護の現行実装（statusline.py:16-18）では、reconfigure を持たない
    ストリーム注入下で exec_module が ``AttributeError`` を送出して落ちる。
    保護が入ると緑に反転する。AttributeError は握り潰さず伝播させ、
    pytest の traceback に実際の例外型・メッセージが出るようにしている
    （Red が「正しい理由で赤い」ことを実出力で確認できるようにするため）。

意図的な設計上の制約（変更しないこと）:
    * モジュールのロードは ``importlib.util.spec_from_file_location`` +
      ``exec_module`` で **毎回再実行** する。``import statusline`` は
      tests/conftest.py:15 の sys.path 挿入で成立してしまうが、sys.modules
      キャッシュにより 2 回目以降モジュール本体が再実行されず「空の緑」になる。
      前例は tests/hooks/test_statusline.py:38-53 の ``load_statusline()``。
    * 注入するストリームは reconfigure 属性を持たない **実オブジェクト**
      （``io.StringIO``）とする。``MagicMock`` は未定義属性を自動生成するため
      「reconfigure を持つ」ことになり、無保護実装でも赤にならない。
    * 3 ストリームの差し替えは ``monkeypatch.setattr(sys, ...)`` でテスト内に閉じ、
      teardown で確実に復元する。ambient なストリームには依存しない
      （tests/test_statusline.py:18-21 は sys.stdin をグローバル置換したまま
      復元しないため、ambient に依存すると Red が Red にならない）。
    * **reconfigure 呼び出しの「粒度」は敢えて検証しない。** CLAUDE.md §9-3 の
      canonical idiom は 3 ストリームを 1 つの try ブロックで囲む形であり、
      「1 本壊れても残り 2 本は reconfigure されること」を要求すると
      canonical idiom を弾いてしまう。要求するのは
      「クラッシュしないこと」＋「対応ストリームには実際に適用されること」の 2 点のみ。
"""

import importlib.util
import io
import itertools
import sys
import types
from pathlib import Path

import pytest

STATUSLINE_PATH = (
    Path(__file__).parents[2]
    / ".claude"
    / "hooks"
    / "statusline.py"
)

#: statusline.py がモジュールレベルで reconfigure を呼ぶ 3 ストリーム
STREAM_NAMES = ("stdin", "stdout", "stderr")

#: exec_module ごとにユニークなモジュール名を振り、キャッシュ経由の再利用を避ける
_load_counter = itertools.count()


def _stream_without_reconfigure() -> io.StringIO:
    """``reconfigure`` を持たない実ストリームを返す。

    テスト自体の有効性（この注入が無保護実装を確実に赤にすること）を
    その場で表明しておく。将来 io.StringIO に reconfigure が生えたら、
    サイレントに空の緑になるのではなくここで落ちる。
    """
    stream = io.StringIO()
    assert not hasattr(stream, "reconfigure"), (
        "io.StringIO が reconfigure を持つ実装では、この注入は無保護実装を"
        "赤にできない（テストの検出力が失われている）"
    )
    return stream


def _stream_with_reconfigure() -> io.TextIOWrapper:
    """``reconfigure`` を持ち、適用されたかを ``encoding`` で観測できる実ストリーム。

    初期 encoding は utf-8 以外（latin-1）にしておく。こうすることで
    「utf-8 になっている」ことが reconfigure が実際に呼ばれた証拠になる。
    """
    stream = io.TextIOWrapper(io.BytesIO(), encoding="latin-1")
    assert hasattr(stream, "reconfigure")
    assert stream.encoding != "utf-8"
    return stream


def _exec_statusline() -> types.ModuleType:
    """statusline.py を毎回新規に exec してモジュールオブジェクトを返す。

    ``__name__`` にユニーク名を与えるため ``if __name__ == '__main__'`` 配下の
    ``main()`` は起動しない（ロード自体は副作用を持たない）。
    """
    name = f"_statusline_reconfigure_probe_{next(_load_counter)}"
    spec = importlib.util.spec_from_file_location(name, STATUSLINE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # module_from_spec は sys.modules に登録しない = 毎回本体が再実行される
    assert name not in sys.modules
    spec.loader.exec_module(mod)
    return mod


def _assert_module_fully_executed(mod: types.ModuleType) -> None:
    """モジュール本体が最後まで実行されたことを確認する。

    ``main`` は statusline.py:136 の最終 def。これが存在することで
    「reconfigure 行で中断せず本体を最後まで通った」ことを担保する
    （途中で例外を握り潰して打ち切る実装を空の緑にしないため）。
    """
    assert mod.MAX_INPUT == 64 * 1024
    assert callable(mod.main)
    assert callable(mod.render_output)
    assert callable(mod.pct_color)


# ---------------------------------------------------------------------------
# 本命の性質: reconfigure 非対応ストリーム下でもロードがクラッシュしない
# ---------------------------------------------------------------------------

def test_module_loads_when_all_three_streams_lack_reconfigure(monkeypatch):
    """3 ストリーム全てが reconfigure 非対応でも statusline のロードは成功する。

    無保護実装では exec_module が AttributeError で落ちる（Red）。
    try/except AttributeError 保護が入ると緑に反転する。
    """
    for name in STREAM_NAMES:
        monkeypatch.setattr(sys, name, _stream_without_reconfigure())

    mod = _exec_statusline()

    _assert_module_fully_executed(mod)


@pytest.mark.parametrize("broken", STREAM_NAMES)
def test_module_loads_when_single_stream_lacks_reconfigure(monkeypatch, broken):
    """3 ストリームのうち 1 本だけが reconfigure 非対応でもロードは成功する。

    「どのストリームが欠けても落ちない」ことを 1 本ずつ独立に固定する。
    残り 2 本は reconfigure 対応の実オブジェクトにしておくことで、
    落ちる原因が対象ストリーム 1 本に限定されることを保証する。
    """
    for name in STREAM_NAMES:
        if name == broken:
            monkeypatch.setattr(sys, name, _stream_without_reconfigure())
        else:
            monkeypatch.setattr(sys, name, _stream_with_reconfigure())

    mod = _exec_statusline()

    _assert_module_fully_executed(mod)


# ---------------------------------------------------------------------------
# 反・空実装ガード: 保護を「reconfigure 呼び出しの削除」で通してはならない
# ---------------------------------------------------------------------------

def test_reconfigure_is_still_applied_when_streams_support_it(monkeypatch):
    """reconfigure 対応ストリームには utf-8 が実際に適用される。

    本テストは保護導入前から緑であり、Red の一部ではない。
    「reconfigure 行を丸ごと削除する」という空実装で FR-3 を通す退行を防ぐ
    回帰ガードとして置く（CLAUDE.md §9-3 が reconfigure 自体を必須としており、
    削除は cp1252 環境での UnicodeEncodeError クラッシュを復活させる）。
    """
    streams = {}
    for name in STREAM_NAMES:
        stream = _stream_with_reconfigure()
        monkeypatch.setattr(sys, name, stream)
        streams[name] = stream

    mod = _exec_statusline()

    _assert_module_fully_executed(mod)
    for name, stream in streams.items():
        assert stream.encoding == "utf-8", (
            f"sys.{name} が utf-8 に reconfigure されていない "
            f"(encoding={stream.encoding!r})。reconfigure 呼び出しが"
            "削除された可能性がある"
        )


def test_broken_stream_injection_is_actually_detectable(monkeypatch):
    """テスト媒体の有効性: 注入したストリームが reconfigure を持たないこと。

    本テストが緑である限り、上の 2 テストの Red は
    「reconfigure 属性の不在」に由来すると言い切れる。
    MagicMock 注入に差し替えられた場合はここが落ちる。
    """
    for name in STREAM_NAMES:
        monkeypatch.setattr(sys, name, _stream_without_reconfigure())

    for name in STREAM_NAMES:
        stream = getattr(sys, name)
        assert isinstance(stream, io.StringIO)
        assert not hasattr(stream, "reconfigure")
