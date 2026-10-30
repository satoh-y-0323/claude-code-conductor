"""
dev-workflow の references/record-protocol.md の実在を検査する導線テスト。

純テキスト走査（pathlib のみ）。文言・節構成の固定は行わない。
"""

from __future__ import annotations

import sys

from tests.skills._skill_helpers import SKILLS_DIR

RECORD_PROTOCOL_PATH = SKILLS_DIR / "dev-workflow" / "references" / "record-protocol.md"


class TestReferenceFilesExist:
    """Red - 分離先 references の実在。"""

    def test_red_record_protocol_file_exists(self):
        assert RECORD_PROTOCOL_PATH.is_file(), (
            f"RED FAIL: {RECORD_PROTOCOL_PATH} が存在しない"
        )


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
