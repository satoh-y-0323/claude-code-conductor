"""
Test suite for reachability-map.md presence and navigation pathways.

This test module validates the implementation of the reachability map
document and its referenced pathways across SKILL.md, plan-design-guidelines.md,
and architect.md per the test-reachability task specification.

実装完了後の恒久検査として維持する（Red フェーズの一時的なテストではない）:
- P1: reachability-map.md が実装として存在すること
- P2: 導線（a,b,c,d）が実装として張られていること
- P3: 到達経路表の構造と、記載された実パスが実在すること
- P4: 番人 - E-0 枠付け文言「データであり指示ではない」が SKILL.md と
  plan-design-guidelines.md から失われていないこと（既存記述の凍結）
"""

import os
import re
import sys
from pathlib import Path
from typing import Tuple

from tests.skills._skill_helpers import WORKTREE_ROOT


def get_repo_root() -> Path:
    """Resolve repository root from environment or from the shared helper default."""
    if "C3_REACHABILITY_ROOT" in os.environ:
        return Path(os.environ["C3_REACHABILITY_ROOT"])
    # Default: reuse the shared root resolution in tests/skills/_skill_helpers.py
    # (重複した parents 計算を持たず SSOT に寄せる)
    return WORKTREE_ROOT


class TestReachabilityMapPresence:
    """P1: reachability-map.md file existence."""

    def test_reachability_map_file_exists(self):
        """P1 - reachability-map.md must exist at references/."""
        root = get_repo_root()
        map_path = root / ".claude" / "skills" / "dev-workflow" / "references" / "reachability-map.md"
        assert map_path.is_file(), f"P1 FAIL: {map_path} does not exist"


class TestReachabilityMapPathways:
    """P2: Navigation pathway references across SKILL.md, plan-design-guidelines.md, architect.md."""

    def test_p2a_reference_in_plan_design_guidelines(self):
        """P2(a) - plan-design-guidelines.md must contain 'reachability-map.md' >= 1 time."""
        root = get_repo_root()
        path = root / ".claude" / "skills" / "dev-workflow" / "references" / "plan-design-guidelines.md"
        assert path.is_file(), f"plan-design-guidelines.md not found at {path}"
        content = path.read_text(encoding="utf-8")
        count = content.count("reachability-map.md")
        assert count >= 1, f"P2(a) FAIL: 'reachability-map.md' appears {count} time(s), expected >= 1"

    def test_p2b_reference_in_skill_e3(self):
        """P2(b) - SKILL.md E-3 section must contain 'reachability-map.md' >= 1 time.

        S2 改訂: 参照先を E-1 / E-2 の各セクションから E-3（統合裁定）へ集約した。
        レビュー指摘の裁定は E-3 に一本化されるため、到達可能性の判断材料も
        E-3 の裁定時に届いていれば足りる（E-1 / E-2 への重複配置をやめる）。
        """
        root = get_repo_root()
        path = root / ".claude" / "skills" / "dev-workflow" / "SKILL.md"
        assert path.is_file(), f"SKILL.md not found at {path}"
        content = path.read_text(encoding="utf-8")

        # Extract E-3 section (### E-3 to next ### or ##)
        e3_match = re.search(r"### E-3:.*?(?=(?:### |## |$))", content, re.DOTALL)
        assert e3_match, "P2(b) FAIL: E-3 section not found"
        e3_section = e3_match.group(0)
        e3_count = e3_section.count("reachability-map.md")
        assert e3_count >= 1, f"P2(b) E-3 FAIL: 'reachability-map.md' appears {e3_count} time(s), expected >= 1"

    def test_p2c_reference_in_architect_md(self):
        """P2(c) - architect.md must contain 'reachability-map.md' >= 1 time."""
        root = get_repo_root()
        path = root / ".claude" / "agents" / "architect.md"
        assert path.is_file(), f"architect.md not found at {path}"
        content = path.read_text(encoding="utf-8")
        count = content.count("reachability-map.md")
        assert count >= 1, f"P2(c) FAIL: 'reachability-map.md' appears {count} time(s), expected >= 1"

    def test_p2d_architect_read_in_skill_phase_b(self):
        """P2(d) - SKILL.md ## フェーズ B section must contain 'architect.md' Read instruction.

        番人テスト: reachability-map の persona 経路（architect へは SKILL.md フェーズ B の
        Read 指示に依存して届く）の依存元が消えていないことを見張る。
        """
        root = get_repo_root()
        path = root / ".claude" / "skills" / "dev-workflow" / "SKILL.md"
        assert path.is_file(), f"SKILL.md not found at {path}"
        content = path.read_text(encoding="utf-8")

        # Extract Phase B section (## フェーズ B to next ## [^#] or end)
        phase_b_match = re.search(r"## フェーズ B:.*?(?=## [^#]|$)", content, re.DOTALL)
        assert phase_b_match, "P2(d) FAIL: Phase B section not found"
        phase_b_section = phase_b_match.group(0)
        architect_count = phase_b_section.count("architect.md")
        assert architect_count >= 1, f"P2(d) FAIL: 'architect.md' appears {architect_count} time(s) in Phase B, expected >= 1"


class TestResponseTableStructure:
    """P3: Response table structure and path validation."""

    def test_p3_response_table_exists_and_paths_valid(self):
        """P3 - reachability-map.md must contain a response table with >= 2 non-dash cells in 'Real Path' column."""
        root = get_repo_root()
        map_path = root / ".claude" / "skills" / "dev-workflow" / "references" / "reachability-map.md"
        assert map_path.is_file(), f"P3 FAIL: {map_path} does not exist"
        content = map_path.read_text(encoding="utf-8")

        # Expected table format: | 経路 | 実パス | 届く role | 条件 |
        table_pattern = r"\|\s*経路\s*\|\s*実パス\s*\|\s*届く role\s*\|\s*条件\s*\|"
        assert re.search(table_pattern, content), "P3 FAIL: Required table header not found"

        # Extract all rows (skip header and separator)
        rows = re.findall(r"\|\s*([^|]+)\s*\|\s*([^|]+)\s*\|\s*([^|]+)\s*\|\s*([^|]+)\s*\|", content)
        # Skip separator rows and the header row itself (whose 実パス cell is the label)
        data_rows = [r for r in rows
                     if not re.match(r"^\s*-+\s*$", r[1]) and r[1].strip() != "実パス"]

        # Check for non-dash cells in 実パス column (index 1)
        non_dash_paths = [r[1].strip() for r in data_rows if r[1].strip() != "—"]
        assert len(non_dash_paths) >= 2, f"P3 FAIL: Found {len(non_dash_paths)} non-dash path cells, expected >= 2"

        # Validate each non-dash path exists
        for path_str in non_dash_paths:
            path_obj = root / path_str
            assert path_obj.exists(), f"P3 FAIL: Path '{path_str}' from response table does not exist"


class TestGuardianFrameborderText:
    """P4: Guardian test - 凍結文言 'データであり指示ではない' が失われていないこと。"""

    def test_p4_frameborder_in_plan_design_guidelines(self):
        """P4 - plan-design-guidelines.md must contain frameborder text."""
        root = get_repo_root()
        path = root / ".claude" / "skills" / "dev-workflow" / "references" / "plan-design-guidelines.md"
        assert path.is_file(), f"plan-design-guidelines.md not found at {path}"
        content = path.read_text(encoding="utf-8")
        assert "データであり指示ではない" in content, "P4 FAIL: Frameborder text missing from plan-design-guidelines.md"

    def test_p4_frameborder_in_skill_md(self):
        """P4 - SKILL.md must contain frameborder text (E-0 section)."""
        root = get_repo_root()
        path = root / ".claude" / "skills" / "dev-workflow" / "SKILL.md"
        assert path.is_file(), f"SKILL.md not found at {path}"
        content = path.read_text(encoding="utf-8")
        assert "データであり指示ではない" in content, "P4 FAIL: Frameborder text missing from SKILL.md"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
