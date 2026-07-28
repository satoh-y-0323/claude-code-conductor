"""
INIT_ONLY_PATTERNS — `c3 init` は配置するが `c3 update` は上書きしないファイル.

## 背景

`_excludes.should_skip` は 3 つの用途を 1 つの真偽値で兼ねている:

  1. wheel（`_template/`）に収録するか
  2. `c3 init` が配置するか
  3. `c3 update` が上書きするか

このため「**初回は配置したいが、以後ユーザーが育てるので上書きしたくない**」という
ファイルを表現できなかった。`should_skip` に入れると wheel からも消えて init でも
配置されず、入れないと update で毎回上書きされる。

実害（2026-07-28 実測）:

- `rules/promoted/index.md`: config-policy.md §7 落とし穴 3 は「意図的に除外している」と
  書いているが `should_skip` は False を返しており、実際には `c3 update` の上書き対象。
  利用先で `/promote-pattern` が追記した目録行が空雛形で消える
- `.claude/.gitignore`: 利用先ユーザーが除外行を追記していると `c3 update` で消え、
  次の commit で意図しないファイルが tracked になりうる（security-review [SR-K-002]）

`INIT_ONLY_PATTERNS` は 2 番目と 3 番目の軸を分離する。
"""

from __future__ import annotations

import argparse
import filecmp
import os
from pathlib import Path

import pytest

from c3._excludes import INIT_ONLY_PATTERNS, is_init_only, should_skip


class TestInitOnlyPatterns:
    def test_promoted_index_is_init_only(self):
        assert is_init_only("rules/promoted/index.md") is True

    def test_distributed_gitignore_is_init_only(self):
        assert is_init_only(".gitignore") is True

    def test_ordinary_files_are_not_init_only(self):
        for rel in (
            "agents/developer.md",
            "skills/develop/SKILL.md",
            "hooks/stop.py",
            "CLAUDE.md",
            "settings.json",
            "rules/promoted/20260101-foo.md",
        ):
            assert is_init_only(rel) is False, f"{rel} は通常の更新対象であるべき"

    def test_init_only_files_are_still_distributed(self):
        """INIT_ONLY は「配布しない」ではない。wheel 収録と init 配置は続ける."""
        for rel in INIT_ONLY_PATTERNS:
            assert should_skip(rel) is False, (
                f"{rel} が should_skip に入ると wheel から消え c3 init でも配置されなくなる"
            )


class TestWalkDiffRespectsInitOnly:
    """`_walk_diff` が INIT_ONLY を「add はする・update はしない」で扱うこと."""

    @staticmethod
    def _make_tree(root: Path, files: dict[str, str]) -> Path:
        for rel, body in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        return root

    def test_init_only_file_is_added_when_absent(self, tmp_path):
        from c3.cli_update import _walk_diff

        template = self._make_tree(
            tmp_path / "template", {"rules/promoted/index.md": "# Promoted Rules\n"}
        )
        dest = tmp_path / "dest"
        dest.mkdir()

        actions = list(_walk_diff(template, dest))
        assert actions, "存在しない INIT_ONLY ファイルは add されるべき"
        assert all(a == "add" for a, _ in actions)

    def test_init_only_file_is_not_overwritten_when_modified(self, tmp_path):
        from c3.cli_update import _walk_diff

        template = self._make_tree(
            tmp_path / "template", {"rules/promoted/index.md": "# Promoted Rules\n"}
        )
        dest = self._make_tree(
            tmp_path / "dest",
            {"rules/promoted/index.md": "# Promoted Rules\n- **My rule** — 追記済み\n"},
        )

        actions = list(_walk_diff(template, dest))
        assert actions == [], (
            "利用先で育てられた INIT_ONLY ファイルは update 対象にしてはならない"
        )

    def test_gitignore_is_not_overwritten_when_modified(self, tmp_path):
        from c3.cli_update import _walk_diff

        template = self._make_tree(tmp_path / "template", {".gitignore": "state/*.flag\n"})
        dest = self._make_tree(
            tmp_path / "dest", {".gitignore": "state/*.flag\nmy-secret-dir/\n"}
        )

        actions = list(_walk_diff(template, dest))
        assert actions == [], "利用先が追記した除外行を update で消してはならない"

    def test_ordinary_file_is_still_updated(self, tmp_path):
        """INIT_ONLY 導入で通常ファイルの更新が壊れていないこと（回帰）."""
        from c3.cli_update import _walk_diff

        template = self._make_tree(tmp_path / "template", {"agents/developer.md": "new\n"})
        dest = self._make_tree(tmp_path / "dest", {"agents/developer.md": "old\n"})

        actions = list(_walk_diff(template, dest))
        assert [a for a, _ in actions] == ["update"]

    def test_identical_init_only_file_yields_nothing(self, tmp_path):
        from c3.cli_update import _walk_diff

        body = "# Promoted Rules\n"
        template = self._make_tree(tmp_path / "template", {"rules/promoted/index.md": body})
        dest = self._make_tree(tmp_path / "dest", {"rules/promoted/index.md": body})

        assert list(_walk_diff(template, dest)) == []


class TestDeletionsRespectInitOnly:
    """`deletions.txt` 経路が INIT_ONLY を削除しないこと（security-review [SR-NEW-1]）.

    `_walk_diff` は INIT_ONLY を上書きしないが、`c3 update` にはもう 1 本
    ファイルを消せる経路がある: `.claude/deletions.txt` に列挙されたパスを
    `_apply_deletions` が `unlink()` する経路。ここは `is_init_only()` を
    参照していないため、配布元で `deletions.txt` に `.gitignore` や
    `rules/promoted/index.md` を誤って追記すると、`_walk_diff` の保護を
    素通りして**利用先が育てた内容ごと完全削除**される。

    既存 13 段のセーフガード（絶対パス・`..`・symlink・`deletions.txt` 自己削除など）は
    いずれも「warning を積んでスキップ」で弾いている。INIT_ONLY もその仲間として扱う。
    """

    PROMOTED_REL = "rules/promoted/index.md"
    PROMOTED_BODY = "# Promoted Rules\n\n- **My rule** — 利用先が育てた目録行\n"
    GITIGNORE_REL = ".gitignore"
    GITIGNORE_BODY = "state/*.flag\nmy-secret-dir/\n"

    @staticmethod
    def _make_claude_root(tmp_path: Path, files: dict[str, str]) -> Path:
        root = tmp_path / ".claude"
        for rel, body in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        root.mkdir(parents=True, exist_ok=True)
        return root.resolve()

    def test_promoted_index_is_not_deleted(self, tmp_path):
        """deletions.txt に書かれていても promoted/index.md は削除しない."""
        from c3.cli_update import _apply_deletions

        claude_root = self._make_claude_root(
            tmp_path, {self.PROMOTED_REL: self.PROMOTED_BODY}
        )
        target = claude_root / self.PROMOTED_REL

        result = _apply_deletions(
            [self.PROMOTED_REL],
            claude_root=claude_root,
            dry_run=False,
            assume_yes=True,
        )

        assert target.exists(), (
            "INIT_ONLY のファイルは deletions.txt 経由でも削除してはならない"
        )
        assert target.read_text(encoding="utf-8") == self.PROMOTED_BODY, (
            "利用先が育てた目録行が失われている"
        )
        assert self.PROMOTED_REL not in result["deleted"]
        assert self.PROMOTED_REL not in result["to_delete"]
        assert self.PROMOTED_REL not in result["absent"], (
            "実在するファイルを absent 扱いにすると利用者が気づけない"
        )
        assert any(self.PROMOTED_REL in w for w in result["warnings"]), (
            "既存セーフガードに倣い、弾いた理由を warning として残すこと: "
            f"warnings={result['warnings']!r}"
        )

    def test_gitignore_is_not_deleted(self, tmp_path):
        """deletions.txt に書かれていても .claude/.gitignore は削除しない."""
        from c3.cli_update import _apply_deletions

        claude_root = self._make_claude_root(
            tmp_path, {self.GITIGNORE_REL: self.GITIGNORE_BODY}
        )
        target = claude_root / self.GITIGNORE_REL

        result = _apply_deletions(
            [self.GITIGNORE_REL],
            claude_root=claude_root,
            dry_run=False,
            assume_yes=True,
        )

        assert target.exists(), (
            ".gitignore を消すと次の commit で意図しないファイルが tracked になる"
        )
        assert target.read_text(encoding="utf-8") == self.GITIGNORE_BODY
        assert self.GITIGNORE_REL not in result["deleted"]
        assert self.GITIGNORE_REL not in result["to_delete"]
        assert any(self.GITIGNORE_REL in w for w in result["warnings"]), (
            f"warning が積まれていない: warnings={result['warnings']!r}"
        )

    def test_init_only_is_not_listed_as_candidate_in_dry_run(self, tmp_path):
        """--dry-run の削除予告一覧にも INIT_ONLY を載せない（予告と実挙動の一致）."""
        from c3.cli_update import _apply_deletions

        claude_root = self._make_claude_root(
            tmp_path,
            {
                self.PROMOTED_REL: self.PROMOTED_BODY,
                self.GITIGNORE_REL: self.GITIGNORE_BODY,
            },
        )

        result = _apply_deletions(
            [self.PROMOTED_REL, self.GITIGNORE_REL],
            claude_root=claude_root,
            dry_run=True,
            assume_yes=False,
        )

        assert result["to_delete"] == [], (
            "dry-run の予告に載ると「消える」と誤解させる: "
            f"to_delete={result['to_delete']!r}"
        )
        assert len(result["warnings"]) == 2, (
            f"2 件とも弾いた理由を出すこと: warnings={result['warnings']!r}"
        )

    def test_ordinary_file_is_still_deleted(self, tmp_path):
        """INIT_ONLY ガード追加で通常ファイルの削除が壊れていないこと（回帰）."""
        from c3.cli_update import _apply_deletions

        claude_root = self._make_claude_root(
            tmp_path, {"agents/tdd-develop.md": "廃止済みエージェント\n"}
        )

        result = _apply_deletions(
            ["agents/tdd-develop.md"],
            claude_root=claude_root,
            dry_run=False,
            assume_yes=True,
        )

        assert result["deleted"] == ["agents/tdd-develop.md"]
        assert not (claude_root / "agents" / "tdd-develop.md").exists()

    def test_handle_does_not_delete_init_only_files(
        self, tmp_path, monkeypatch, capsys
    ):
        """`c3 update` 入口（handle）を通しても INIT_ONLY が消えず、利用者が気づけること.

        通常ファイル 1 件の削除を混ぜているのは、`_format_deletion_report` が
        「削除候補ゼロ」のときサマリを `deletions: nothing to delete` に畳んで
        warning 本文を出さないため（現行仕様）。実運用に近い混在ケースで
        「弾いた事実が画面に出る」ことを確認する。
        """
        from c3.cli_update import handle

        claude_dir = tmp_path / ".claude"
        template_dir = tmp_path / "fake_template"
        for base in (claude_dir, template_dir):
            base.mkdir(parents=True, exist_ok=True)

        # 利用先に「育った」INIT_ONLY ファイルと、廃止された通常ファイルを置く
        (claude_dir / "rules" / "promoted").mkdir(parents=True)
        (claude_dir / self.PROMOTED_REL).write_text(self.PROMOTED_BODY, encoding="utf-8")
        (claude_dir / self.GITIGNORE_REL).write_text(self.GITIGNORE_BODY, encoding="utf-8")
        (claude_dir / "agents").mkdir()
        (claude_dir / "agents" / "tdd-develop.md").write_text("廃止\n", encoding="utf-8")

        # 配布元が誤って INIT_ONLY を deletions.txt に追記してしまった状況
        (template_dir / "deletions.txt").write_text(
            f"agents/tdd-develop.md\n{self.PROMOTED_REL}\n{self.GITIGNORE_REL}\n",
            encoding="utf-8",
        )

        monkeypatch.setattr("c3.cli_update.templates_dir", lambda: template_dir)
        monkeypatch.setattr("c3.__version__", "2.58.0")
        monkeypatch.setattr(
            "builtins.input",
            lambda _: (_ for _ in ()).throw(AssertionError("prompt must not fire with --yes")),
        )

        args = argparse.Namespace(
            target=tmp_path, dry_run=False, platform="claude", yes=True
        )
        assert handle(args) == 0

        assert (claude_dir / self.PROMOTED_REL).read_text(encoding="utf-8") == self.PROMOTED_BODY
        assert (claude_dir / self.GITIGNORE_REL).read_text(encoding="utf-8") == self.GITIGNORE_BODY
        # 通常ファイルは従来どおり削除される
        assert not (claude_dir / "agents" / "tdd-develop.md").exists()

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert self.PROMOTED_REL in combined, (
            "弾いたことが画面に出ないと、利用者は deletions.txt の誤記に気づけない: "
            f"out={captured.out!r} err={captured.err!r}"
        )
        assert self.GITIGNORE_REL in combined


class TestHandleUpdateIntegrationInitOnly:
    """`c3 update` 入口（handle）を通した e2e（code-review [CR-T-001]）.

    従来は `_walk_diff` を直接呼ぶ単体テストしかなく、`handle()` の配線
    （template 解決 → actions 反映 → shutil.copy2）が保護を素通しして
    いないことを確認できていなかった。

    core contract: **配置 → 利用先で改変 → `c3 update` 実行 → 改変が保持される**。
    組み立て方は `tests/test_cli_update_breaking_changes.py::TestHandleBreakingChangesIntegration`
    に倣う（tmp_path に `.claude/` と fake_template を作り `templates_dir` を差し替える）。
    """

    PROMOTED_REL = "rules/promoted/index.md"
    GITIGNORE_REL = ".gitignore"
    ORDINARY_REL = "agents/developer.md"

    TEMPLATE_FILES = {
        PROMOTED_REL: "# Promoted Rules\n",
        GITIGNORE_REL: "state/*.flag\nlogs/\n",
        ORDINARY_REL: "# developer (template v1)\n",
    }

    @staticmethod
    def _make_args(tmp_path: Path, dry_run: bool = False) -> argparse.Namespace:
        return argparse.Namespace(
            target=tmp_path, dry_run=dry_run, platform="claude", yes=True
        )

    def _setup(self, tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        template_dir = tmp_path / "fake_template"
        template_dir.mkdir()
        for rel, body in self.TEMPLATE_FILES.items():
            p = template_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")

        monkeypatch.setattr("c3.cli_update.templates_dir", lambda: template_dir)
        monkeypatch.setattr("c3.__version__", "2.58.0")
        monkeypatch.setattr(
            "builtins.input",
            lambda _: (_ for _ in ()).throw(AssertionError("prompt must not fire")),
        )
        return claude_dir, template_dir

    def test_first_update_places_init_only_files(self, tmp_path, monkeypatch, capsys):
        """不在の INIT_ONLY ファイルは handle 経由でも実際に配置される."""
        from c3.cli_update import handle

        claude_dir, _ = self._setup(tmp_path, monkeypatch)

        assert handle(self._make_args(tmp_path)) == 0
        capsys.readouterr()

        for rel, body in self.TEMPLATE_FILES.items():
            assert (claude_dir / rel).exists(), f"{rel} が配置されていない"
            assert (claude_dir / rel).read_text(encoding="utf-8") == body

    def test_user_edits_survive_update_while_ordinary_file_is_refreshed(
        self, tmp_path, monkeypatch, capsys
    ):
        """配置 → 利用先で改変 → c3 update → 改変が保持される（通常ファイルは更新される）."""
        from c3.cli_update import handle

        claude_dir, template_dir = self._setup(tmp_path, monkeypatch)

        # 1) 初回配置
        assert handle(self._make_args(tmp_path)) == 0
        capsys.readouterr()

        # 2) 利用先で改変（INIT_ONLY 2 件 + 通常ファイル 1 件）
        grown_promoted = "# Promoted Rules\n\n- **My rule** — /promote-pattern が追記\n"
        grown_gitignore = "state/*.flag\nlogs/\nmy-secret-dir/\n"
        (claude_dir / self.PROMOTED_REL).write_text(grown_promoted, encoding="utf-8")
        (claude_dir / self.GITIGNORE_REL).write_text(grown_gitignore, encoding="utf-8")
        (claude_dir / self.ORDINARY_REL).write_text("# 利用先が触った\n", encoding="utf-8")

        # 3) 新しい C3 が入った想定で template の通常ファイルを更新
        new_ordinary = "# developer (template v2)\n"
        (template_dir / self.ORDINARY_REL).write_text(new_ordinary, encoding="utf-8")

        # 4) c3 update
        assert handle(self._make_args(tmp_path)) == 0
        out = capsys.readouterr().out

        # 5) 改変が保持されている
        assert (claude_dir / self.PROMOTED_REL).read_text(encoding="utf-8") == grown_promoted, (
            "handle 経由で promoted/index.md が上書きされた"
        )
        assert (claude_dir / self.GITIGNORE_REL).read_text(encoding="utf-8") == grown_gitignore, (
            "handle 経由で .gitignore が上書きされた"
        )
        # 通常ファイルは template の最新に更新される
        assert (claude_dir / self.ORDINARY_REL).read_text(encoding="utf-8") == new_ordinary
        assert self.ORDINARY_REL in out.replace("\\", "/"), (
            f"通常ファイルの update が報告されていない: {out!r}"
        )

    def test_dry_run_does_not_report_init_only_as_changing(
        self, tmp_path, monkeypatch, capsys
    ):
        """--dry-run の予告にも改変済み INIT_ONLY を載せない（予告と実挙動の一致）."""
        from c3.cli_update import handle

        claude_dir, _ = self._setup(tmp_path, monkeypatch)

        assert handle(self._make_args(tmp_path)) == 0
        capsys.readouterr()

        (claude_dir / self.PROMOTED_REL).write_text("# 育った\n", encoding="utf-8")
        (claude_dir / self.GITIGNORE_REL).write_text("state/*.flag\nlogs/\nfoo/\n", encoding="utf-8")

        assert handle(self._make_args(tmp_path, dry_run=True)) == 0
        out = capsys.readouterr().out.replace("\\", "/")

        assert self.PROMOTED_REL not in out, f"dry-run が INIT_ONLY を変更予定と予告した: {out!r}"
        assert self.GITIGNORE_REL not in out, f"dry-run が INIT_ONLY を変更予定と予告した: {out!r}"


class TestPathNormalizationBypassesInitOnlyProtection:
    """Step 13 (init-only protection) が表記ゆれで迂回されないことの回帰テスト.

    是正前、`_validate_deletion_path` は step 13 で `is_init_only(rel)` に
    deletions.txt から読んだ**生文字列**を渡していた。二重スラッシュ・末尾スラッシュ・
    大小違いなどの表記ゆれがあると `is_init_only()` の fnmatch パターンと一致せず、
    実ファイルに解決されるにもかかわらず保護が素通りしていた（類型 3・入力 4 件）。

    是正（6bb1b6a）で `resolved.relative_to(claude_root).as_posix()` を渡す形になり、
    step 10/11/12 と同じ実体解決後の値で判定されるようになった。本クラスはその退行を防ぐ。

    検証層を 2 つに分けて要件化:
      - 戻り値層: _apply_deletions() の result["warnings"] に該当 rel が含まれること
      - 画面層: handle() 経由で stdout/stderr に警告が出ること

    Windows 大小無視テストは tmp_path 上で実際に大小無視を実測し、
    区別される環境では skip される（恒久的担保は OS 非依存 2 件のみ）。
    """

    PROMOTED_REL = "rules/promoted/index.md"
    PROMOTED_BODY = "# Promoted Rules\n\n- **My promoted rule** — 利用先で育てた目録行\n"
    GITIGNORE_REL = ".gitignore"
    GITIGNORE_BODY = "state/*.flag\nuser-secret-dir/\n"
    ORDINARY_REL = "agents/foo.md"
    ORDINARY_BODY = "廃止されたエージェント\n"

    @staticmethod
    def _make_claude_root(tmp_path: Path, files: dict[str, str]) -> Path:
        root = tmp_path / ".claude"
        for rel, body in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        root.mkdir(parents=True, exist_ok=True)
        return root.resolve()

    @staticmethod
    def _is_case_insensitive(tmp_path: Path) -> bool:
        """tmp_path 上で大小文字が区別されないか実測.

        小文字ファイルを作成し、大文字名で exists() を見る。
        True なら大小区別なし（Windows NTFS 等）。
        """
        test_file = tmp_path / "lowercase.txt"
        test_file.write_text("test")
        # 同じディレクトリで大文字でアクセスして True なら大小区別なし
        return (tmp_path / "LOWERCASE.TXT").exists()

    @pytest.mark.parametrize(
        "variant,target_rel",
        [
            # 類型 1: 二重スラッシュ（OS 非依存）
            ("rules//promoted/index.md", "rules/promoted/index.md"),
            # 類型 2: 末尾スラッシュ（OS 非依存）
            ("rules/promoted/index.md/", "rules/promoted/index.md"),
        ],
    )
    def test_apply_deletions_rejects_normalized_path_variants_os_independent(
        self, tmp_path: Path, variant: str, target_rel: str
    ):
        """戻り値層: OS 非依存の表記ゆれは warning で弾かれること.

        二重スラッシュ・末尾スラッシュは `Path.resolve()` で正規化される。是正前は
        step 13 の `is_init_only()` 呼び出しが生文字列 `rel` に対して行われていたため
        パターンと一致せず保護が素通りしていた。

        是正後は解決済み実体パスの相対 POSIX で判定されるため、これらの表記ゆれでも
        保護される（本テストはその退行を防ぐ）。
        """
        from c3.cli_update import _apply_deletions

        claude_root = self._make_claude_root(
            tmp_path, {target_rel: self.PROMOTED_BODY}
        )
        target = claude_root / target_rel

        result = _apply_deletions(
            [variant],  # ← 正規化されていない表記ゆれ版
            claude_root=claude_root,
            dry_run=False,
            assume_yes=True,
        )

        # 削除されてはいけない
        assert target.exists(), (
            f"表記ゆれ '{variant}' が保護を素通りして削除された。"
            f"target={target} exists={target.exists()}"
        )
        assert target.read_text(encoding="utf-8") == self.PROMOTED_BODY
        # warning が記録されていること（戻り値層）
        assert any(target_rel in w for w in result["warnings"]), (
            f"warning に保護対象の rel が含まれていない: "
            f"warnings={result['warnings']!r} target_rel={target_rel!r}"
        )
        # variant が to_delete に入ってはいけない
        assert variant not in result["to_delete"], (
            f"表記ゆれ '{variant}' が to_delete に載った"
        )

    @pytest.mark.parametrize(
        "variant,target_rel",
        [
            # 類型 3: 大文字違い — 大小区別なし FS（Windows NTFS）でのみテスト
            ("RULES/PROMOTED/INDEX.MD", "rules/promoted/index.md"),
            # 類型 4: .gitignore の大小違い
            (".GITIGNORE", ".gitignore"),
        ],
    )
    def test_apply_deletions_rejects_case_variants_windows_only(
        self, tmp_path: Path, variant: str, target_rel: str
    ):
        """戻り値層: Windows 大小無視 FS での表記ゆれは warning で弾かれること.

        Windows NTFS では大小無視のため、`RULES/PROMOTED/INDEX.MD` は
        `rules/promoted/index.md` に解決される。しかし step 13 で
        is_init_only(rel) が呼ばれる際 `rel` はまだ大文字のままで、
        パターン `rules/promoted/index.md` と不一致。
        """
        # 大小文字が区別されないファイルシステムでのみテスト
        if not self._is_case_insensitive(tmp_path):
            pytest.skip("test only on case-insensitive filesystems (Windows NTFS)")

        from c3.cli_update import _apply_deletions

        claude_root = self._make_claude_root(
            tmp_path, {target_rel: self.PROMOTED_BODY if "rules" in target_rel else self.GITIGNORE_BODY}
        )
        target = claude_root / target_rel

        result = _apply_deletions(
            [variant],  # ← 大文字版
            claude_root=claude_root,
            dry_run=False,
            assume_yes=True,
        )

        # 削除されてはいけない
        assert target.exists(), (
            f"大小違い '{variant}' が保護を素通りして削除された"
        )
        # warning に小文字版 (target_rel) が含まれていること
        assert any(target_rel.lower() in w.lower() for w in result["warnings"]), (
            f"warning に保護対象が含まれていない: warnings={result['warnings']!r}"
        )

    def test_handle_rejects_normalized_path_variants_and_shows_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """画面層: OS 非依存の表記ゆれは警告表示・ファイル保持.

        通常ファイル 1 件を削除予定に混ぜて _format_deletion_report が
        warning を出力することを確認。

        二重スラッシュ `rules//promoted/index.md` 版をテスト。
        """
        from c3.cli_update import handle

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        template_dir = tmp_path / "fake_template"
        template_dir.mkdir()

        # 利用先ファイル: promoted/index.md（小文字版・実在）
        (claude_dir / "rules" / "promoted").mkdir(parents=True)
        (claude_dir / self.PROMOTED_REL).write_text(self.PROMOTED_BODY, encoding="utf-8")
        # 廃止ファイル（通常）
        (claude_dir / "agents").mkdir()
        (claude_dir / self.ORDINARY_REL).write_text(self.ORDINARY_BODY, encoding="utf-8")

        # 配布元が二重スラッシュで誤入力
        (template_dir / "deletions.txt").write_text(
            f"agents/foo.md\nrules//promoted/index.md\n",  # ← 二重スラッシュ
            encoding="utf-8",
        )

        monkeypatch.setattr("c3.cli_update.templates_dir", lambda: template_dir)
        monkeypatch.setattr("c3.__version__", "2.58.0")
        monkeypatch.setattr(
            "builtins.input",
            lambda _: (_ for _ in ()).throw(AssertionError("prompt must not fire with --yes")),
        )

        args = argparse.Namespace(
            target=tmp_path, dry_run=False, platform="claude", yes=True
        )
        assert handle(args) == 0

        # promoted/index.md は保持
        assert (claude_dir / self.PROMOTED_REL).exists()
        assert (claude_dir / self.PROMOTED_REL).read_text(encoding="utf-8") == self.PROMOTED_BODY
        # 通常ファイルは削除
        assert not (claude_dir / self.ORDINARY_REL).exists()

        # 画面層で警告が出ていること
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        # 小文字版 rel がメッセージに含まれていること
        assert self.PROMOTED_REL in combined or "promoted" in combined.lower(), (
            f"警告が画面に出ていない: out={captured.out!r} err={captured.err!r}"
        )

    def test_handle_rejects_case_variants_and_shows_warning_windows_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """画面層: Windows 大小無視 FS での .gitignore 大小違い版.

        `.GITIGNORE` が `.gitignore` として保護されることを確認。
        """
        # 大小無視 FS でのみテスト
        if not self._is_case_insensitive(tmp_path):
            pytest.skip("test only on case-insensitive filesystems (Windows NTFS)")

        from c3.cli_update import handle

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        template_dir = tmp_path / "fake_template"
        template_dir.mkdir()

        # 利用先ファイル: .gitignore（小文字版・実在）
        (claude_dir / self.GITIGNORE_REL).write_text(self.GITIGNORE_BODY, encoding="utf-8")
        # 廃止ファイル（通常）
        (claude_dir / "agents").mkdir()
        (claude_dir / self.ORDINARY_REL).write_text(self.ORDINARY_BODY, encoding="utf-8")

        # 配布元が大文字で誤入力
        (template_dir / "deletions.txt").write_text(
            f"agents/foo.md\n.GITIGNORE\n",  # ← 大文字版
            encoding="utf-8",
        )

        monkeypatch.setattr("c3.cli_update.templates_dir", lambda: template_dir)
        monkeypatch.setattr("c3.__version__", "2.58.0")
        monkeypatch.setattr(
            "builtins.input",
            lambda _: (_ for _ in ()).throw(AssertionError("prompt must not fire with --yes")),
        )

        args = argparse.Namespace(
            target=tmp_path, dry_run=False, platform="claude", yes=True
        )
        assert handle(args) == 0

        # .gitignore は保持
        assert (claude_dir / self.GITIGNORE_REL).exists()
        assert (claude_dir / self.GITIGNORE_REL).read_text(encoding="utf-8") == self.GITIGNORE_BODY
        # 通常ファイルは削除
        assert not (claude_dir / self.ORDINARY_REL).exists()

        # 画面層で警告が出ていること
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert self.GITIGNORE_REL.lower() in combined.lower() or "gitignore" in combined.lower(), (
            f"警告が画面に出ていない: out={captured.out!r} err={captured.err!r}"
        )


class TestDocumentedBehaviourMatchesImplementation:
    """config-policy.md の記述と実装が一致していること（今回の食い違いの再発防止）.

    2026-07-28 に「文書が実装とずれている」defect が 2 件見つかっている
    （`promoted/index.md` の除外・config-policy 内の自己矛盾）ため、
    §2 書き込み権限マトリクスと §3 配布判断マトリクス（15 カテゴリ）の
    該当行を**セル単位でパース**して実装と突き合わせる。
    """

    CONFIG_POLICY = Path(__file__).parent.parent / ".claude" / "docs" / "config-policy.md"

    # 「c3 update が更新しない」を表す表記（列に × が入る）
    NOT_UPDATED = "×"

    @classmethod
    def _row_cells(cls, prefix: str) -> list[str]:
        """`prefix` で始まる Markdown 表の行をセル配列にして返す."""
        text = cls.CONFIG_POLICY.read_text(encoding="utf-8")
        matches = [ln for ln in text.splitlines() if ln.startswith(prefix)]
        assert len(matches) == 1, (
            f"{prefix!r} で始まる表の行が config-policy.md に "
            f"{len(matches)} 行ある（1 行であるべき）"
        )
        return [c.strip() for c in matches[0].split("|")]

    def test_promoted_index_documented_as_not_updated(self):
        text = self.CONFIG_POLICY.read_text(encoding="utf-8")
        # カテゴリ #5 の行に「c3 update が更新 ×」が書かれており、実装もそれに一致する
        assert "`.claude/rules/promoted/*`" in text
        assert is_init_only("rules/promoted/index.md") is True

    def test_category_5_promoted_row_matches_implementation(self):
        """§3 カテゴリ #5: 配布 ○ / c3 update が更新 ×（index.md は INIT_ONLY）."""
        cells = self._row_cells("| 5 |")
        assert "`.claude/rules/promoted/*`" in cells[2], cells
        assert cells[3] == "○", f"配布列が ○ でない: {cells[3]!r}"
        assert self.NOT_UPDATED in cells[4], f"更新列に × がない: {cells[4]!r}"
        assert "INIT_ONLY" in cells[4], f"INIT_ONLY の根拠が書かれていない: {cells[4]!r}"

        # 実装照合
        assert should_skip("rules/promoted/index.md") is False, "配布 ○ の記述と矛盾"
        assert is_init_only("rules/promoted/index.md") is True, "更新 × の記述と矛盾"

    def test_category_15_gitignore_row_matches_implementation(self):
        """§3 カテゴリ #15: `.claude/.gitignore` は配布 ○ / c3 update が更新 ×（INIT_ONLY）."""
        cells = self._row_cells("| 15 |")
        assert cells[2] == "`.claude/.gitignore`", f"カテゴリ #15 の対象が違う: {cells[2]!r}"
        assert cells[3] == "○", f"配布列が ○ でない: {cells[3]!r}"
        assert self.NOT_UPDATED in cells[4], f"更新列に × がない: {cells[4]!r}"
        assert "INIT_ONLY" in cells[4], f"INIT_ONLY の根拠が書かれていない: {cells[4]!r}"

        # 実装照合: 配布 ○ → should_skip False / 更新 × → is_init_only True
        assert should_skip(".gitignore") is False, (
            "配布 ○ と書かれているが should_skip が True（wheel から消え c3 init でも配置されない）"
        )
        assert is_init_only(".gitignore") is True, (
            "更新 ×（INIT_ONLY）と書かれているが is_init_only が False（c3 update が上書きする）"
        )

    def test_write_permission_matrix_gitignore_row_matches_implementation(self):
        """§2 書き込み権限マトリクス: `.claude/.gitignore` は init ○ / update ×（INIT_ONLY）."""
        cells = self._row_cells("| `.claude/.gitignore` |")
        assert cells[2] == "○", f"c3 init が初期配置の列が ○ でない: {cells[2]!r}"
        assert self.NOT_UPDATED in cells[3], f"c3 update が上書きの列に × がない: {cells[3]!r}"
        assert "INIT_ONLY" in cells[3], f"INIT_ONLY の根拠が書かれていない: {cells[3]!r}"

        assert should_skip(".gitignore") is False
        assert is_init_only(".gitignore") is True

    def test_no_conflicting_claim_that_update_overwrites_gitignore(self):
        """§2 と §3 が同じ結論であること（config-policy 内の自己矛盾の再発防止）."""
        section3 = self._row_cells("| 15 |")[4]
        section2 = self._row_cells("| `.claude/.gitignore` |")[3]
        for cell in (section2, section3):
            assert self.NOT_UPDATED in cell and "○" not in cell, (
                f"`.claude/.gitignore` の update 列が「更新する」と読める: {cell!r}"
            )
