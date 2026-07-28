"""
Tests for the distributed `.claude/.gitignore`.

C3 は「実行時生成領域は載せない理由があるもの以外は git 管理する」方針
（2026-07-28 裁定・`.claude/docs/config-policy.md` §1-2）。
利用先が recall インデックス（実測 63MB）を素で commit してしまうのを防ぎつつ、
引き継ぎ資産（agent-memory / reports / memory / state/c3.db）は tracked のままにする。

`.gitignore` のパターンは否定（`!`）・ディレクトリ指定・ワイルドカードの組み合わせで
挙動が直感に反しやすいため、**静的読解ではなく実際に git へ判定させる**
（`feedback_execute_to_verify_combinatorial_logic` の教訓）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parent.parent
CLAUDE_GITIGNORE = WORKTREE_ROOT / ".claude" / ".gitignore"

# 除外されるべきパス（.claude/ からの相対）
MUST_BE_IGNORED = [
    "state/recall.hnsw",
    "state/recall_meta.json",
    "state/recall_meta.json.bak",
    # SQLite WAL サイドカー（db.py が PRAGMA journal_mode=WAL で開く）。
    # プロセス異常終了時に残存すると、トランザクション中間データが
    # 「引き継ぎ資産」として tracked されてしまう
    "state/c3.db-wal",
    "state/c3.db-shm",
    "state/init_session.flag",
    "state/tier_selection.json",
    "state/tier_autoapply.jsonl.lock",
    "logs/session.log",
    "worktrees/task-1/README.md",
    "tmp/scratch.md",
    "settings.local.json",
]

# 追跡され続けるべきパス（引き継ぎ資産）
MUST_BE_TRACKED = [
    "state/c3.db",
    "state/setup_done.flag",
    "state/security_audit_exceptions.json",
    "state/tier_autoapply.jsonl",
    "state/.gitkeep",
    "tmp/.gitkeep",
    "agent-memory/code-reviewer/MEMORY.md",
    "reports/code-review-report-20260728-120000.md",
    "memory/sessions/20260728.tmp",
    "memory/patterns.json",
    "CLAUDE.md",
    "settings.json",
    "rules/coding-standards.md",
]

_GIT = shutil.which("git")
requires_git = pytest.mark.skipif(_GIT is None, reason="git が見つからない環境ではスキップ")


@pytest.fixture(scope="module")
def ignore_verdicts(tmp_path_factory):
    """一時 git リポジトリを作り、実際の git に ignore 判定をさせる.

    静的なパターン読解ではなく `git check-ignore` の実挙動を真とする。
    """
    if _GIT is None:
        pytest.skip("git が見つからない環境ではスキップ")

    repo = tmp_path_factory.mktemp("gitignore_probe")
    subprocess.run([_GIT, "init", "-q"], cwd=repo, check=True)

    claude = repo / ".claude"
    claude.mkdir()
    shutil.copyfile(CLAUDE_GITIGNORE, claude / ".gitignore")

    verdicts: dict[str, bool] = {}
    for rel in MUST_BE_IGNORED + MUST_BE_TRACKED:
        target = claude / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
        # check-ignore: exit 0 = ignored, exit 1 = not ignored
        proc = subprocess.run(
            [_GIT, "check-ignore", "-q", f".claude/{rel}"],
            cwd=repo,
        )
        verdicts[rel] = proc.returncode == 0
    return verdicts


class TestFileExists:
    def test_claude_gitignore_is_distributed(self):
        assert CLAUDE_GITIGNORE.is_file(), (
            ".claude/.gitignore が存在しない。_excludes.py の EXCLUDE_PATTERNS に "
            ".gitignore は含まれないため、作成すれば _template 経由で配布される"
        )

    def test_not_excluded_from_distribution(self):
        from c3._excludes import should_skip

        assert should_skip(".gitignore") is False


@requires_git
class TestIgnoredPaths:
    @pytest.mark.parametrize("rel", MUST_BE_IGNORED)
    def test_path_is_ignored(self, rel, ignore_verdicts):
        assert ignore_verdicts[rel] is True, f"{rel} は除外されるべき"


@requires_git
class TestTrackedPaths:
    @pytest.mark.parametrize("rel", MUST_BE_TRACKED)
    def test_path_is_not_ignored(self, rel, ignore_verdicts):
        assert ignore_verdicts[rel] is False, (
            f"{rel} は引き継ぎ資産なので tracked のままであるべき"
        )


@requires_git
class TestNegationPattern:
    """`state/*.flag` を除外しつつ `setup_done.flag` だけ戻す否定パターンの実挙動."""

    def test_session_flag_ignored_but_setup_flag_kept(self, ignore_verdicts):
        # 一括除外が粒度を隠さないこと: 一時 flag は除外・永続 flag は追跡
        assert ignore_verdicts["state/init_session.flag"] is True
        assert ignore_verdicts["state/setup_done.flag"] is False


@requires_git
class TestUpstreamRepoInteraction:
    """配布元リポジトリ自身のルート `.gitignore` との相互作用を検知する [CR-T-001].

    `.claude/.gitignore` はルート `.gitignore` よりネストが深いため、否定パターン
    （`!state/setup_done.flag`）がルートの `.claude/state/*` を打ち消す。
    この副作用は隔離された一時リポジトリのテストでは検知できず、実際
    「フルテスト全緑なのに `git status` に新規ファイルが浮上する」状態が起きた。

    将来 `!` 否定を追加したときに同じ見落としが再発しないよう、**配布元リポジトリの
    実ルート**で `git check-ignore` を回して浮上ファイルを固定する。
    """

    # 配布元で tracked 側に出てよい state ファイル（これ以外が出たら設計判断が必要）
    ALLOWED_SURFACED = {".gitkeep", "setup_done.flag"}

    def test_no_unexpected_state_file_surfaces_upstream(self):
        state_dir = WORKTREE_ROOT / ".claude" / "state"
        if not state_dir.is_dir():
            pytest.skip("配布元に .claude/state/ が無い環境ではスキップ")

        surfaced = set()
        for path in sorted(state_dir.iterdir()):
            if not path.is_file():
                continue
            proc = subprocess.run(
                [_GIT, "check-ignore", "-q", str(path)],
                cwd=WORKTREE_ROOT,
            )
            if proc.returncode != 0:  # exit 1 = not ignored
                surfaced.add(path.name)

        unexpected = surfaced - self.ALLOWED_SURFACED
        assert not unexpected, (
            f"配布元リポジトリで予期せず tracked 候補に浮上した state ファイル: {sorted(unexpected)}。"
            " `.claude/.gitignore` の否定パターンがルート `.gitignore` を打ち消していないか確認すること"
        )


class TestPolicyConsistency:
    """config-policy.md に書いた分類と .gitignore の中身がズレていないこと."""

    def test_documents_reference_each_other(self):
        text = CLAUDE_GITIGNORE.read_text(encoding="utf-8")
        assert "config-policy.md" in text, (
            ".gitignore は分類の根拠として config-policy.md を参照すべき"
        )

    def test_setup_done_flag_has_rationale_comment(self):
        text = CLAUDE_GITIGNORE.read_text(encoding="utf-8")
        assert "setup_done.flag" in text
        # なぜ除外しないのかがファイル内で読み取れること
        assert "setup" in text.lower()
