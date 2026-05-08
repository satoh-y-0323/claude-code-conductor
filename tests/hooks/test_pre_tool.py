"""Tests for .claude/hooks/pre_tool.py

PreToolUse hook の挙動を検証する。

テストケース:
 既存挙動（リグレッション防止）:
  1. rm -rf 系 → exit 2 でブロック（既存）
  2. 通常の Bash → exit 0
  3. Bash 以外の tool_name → exit 0
  4. 不正な JSON → exit 0

 F-006 秘密情報検出:
  5. password=xxx を含む → exit 2、stderr に警告（パターン名: password）
  6. api_key=xxx → exit 2
  7. Bearer xxxxxxxx → exit 2
  8. -----BEGIN ... PRIVATE KEY----- → exit 2
  9. token=xxx → exit 2
 10. aws_secret_access_key=xxx → exit 2
 11. シェルコメント '# password reset' → exit 0（偽陽性回避）
 12. C3_SKIP_SECRET_CHECK=1 環境変数 → exit 0
 13. 警告メッセージに検出値そのものが含まれない（二次漏洩防止）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "pre_tool.py"


def _run_hook(
    payload: dict,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """pre_tool.py を subprocess で実行し、CompletedProcess を返す。"""
    env = dict(os.environ)
    # 既存環境の C3_SKIP_SECRET_CHECK が混入しないようクリア
    env.pop("C3_SKIP_SECRET_CHECK", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _bash_payload(command: str) -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


# ---------------------------------------------------------------------------
# 既存挙動（リグレッション防止）
# ---------------------------------------------------------------------------


class TestExistingBehavior:
    """F-006 追加後も既存ブロック・通過判定が変わらないことを確認する。"""

    def test_rm_rf_is_blocked(self) -> None:
        """rm -rf 系コマンドは引き続き exit 2 でブロックされる。"""
        result = _run_hook(_bash_payload("rm -rf /tmp/somedir"))
        assert result.returncode == 2
        assert "[PreToolUse BLOCK]" in result.stderr

    def test_normal_bash_passes(self) -> None:
        """通常の Bash コマンドは exit 0 で通過する。"""
        result = _run_hook(_bash_payload("ls -la /tmp"))
        assert result.returncode == 0

    def test_non_bash_tool_passes(self) -> None:
        """Bash 以外の tool_name は判定対象外（exit 0）。"""
        result = _run_hook({
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/foo.txt"},
        })
        assert result.returncode == 0

    def test_invalid_json_passes(self) -> None:
        """不正な JSON でも crash せず exit 0 で通過する。"""
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input="this is not json",
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# F-006: 秘密情報検出
# ---------------------------------------------------------------------------


class TestF006SecretDetection:
    """F-006: 秘密情報パターンの検出と bypass 機構。"""

    @pytest.mark.parametrize("command,expected_pattern", [
        ("echo password=hunter2", "password"),
        ("export API_KEY=sk-abc123", "api_key"),
        ("curl -H 'Authorization: Bearer eyJhbGc.something.xx' https://api.example.com", "bearer"),
        ("export token=ghp_abc123", "token"),
        ("export aws_secret_access_key=AKIAxxxxxxx", "aws_secret"),
    ])
    def test_secret_patterns_are_blocked(
        self, command: str, expected_pattern: str
    ) -> None:
        """各種秘密情報パターンが検出されてブロックされる。"""
        result = _run_hook(_bash_payload(command))
        assert result.returncode == 2, (
            f"command={command} expected exit 2 got {result.returncode}\n"
            f"stderr={result.stderr}"
        )
        assert "秘密情報の代入を検出" in result.stderr
        assert f"パターン: {expected_pattern}" in result.stderr

    def test_private_key_block_is_blocked(self) -> None:
        """PEM 形式の秘密鍵ブロックも検出される。"""
        cmd = (
            "echo '-----BEGIN RSA PRIVATE KEY-----' "
            "&& echo 'MIIEowIBAAKCAQEA...' "
            "&& echo '-----END RSA PRIVATE KEY-----'"
        )
        result = _run_hook(_bash_payload(cmd))
        assert result.returncode == 2
        assert "パターン: private_key" in result.stderr

    def test_password_in_comment_is_not_blocked(self) -> None:
        """シェルコメント '# password reset' は誤爆しない（=値 が無いため）。"""
        result = _run_hook(_bash_payload("git commit -m 'password reset feature'"))
        assert result.returncode == 0, (
            f"shell-comment-style mention should not be blocked\n"
            f"stderr={result.stderr}"
        )

    def test_bypass_env_var_skips_detection(self) -> None:
        """C3_SKIP_SECRET_CHECK=1 で検出がスキップされる。"""
        result = _run_hook(
            _bash_payload("echo password=hunter2"),
            extra_env={"C3_SKIP_SECRET_CHECK": "1"},
        )
        assert result.returncode == 0, (
            f"bypass should allow command\n"
            f"stderr={result.stderr}"
        )

    def test_warning_does_not_leak_secret_value(self) -> None:
        """警告メッセージに検出値そのものが含まれない（二次漏洩防止）。"""
        result = _run_hook(_bash_payload("echo password=highly-sensitive-value-12345"))
        assert result.returncode == 2
        # パターン名は出る
        assert "password" in result.stderr
        # 検出値そのものは含まれない
        assert "highly-sensitive-value-12345" not in result.stderr

    def test_non_bash_tool_is_not_scanned(self) -> None:
        """Bash 以外の tool_name は秘密情報検査対象外（exit 0）。"""
        result = _run_hook({
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/tmp/notes.txt",
                "content": "password=hunter2",
            },
        })
        assert result.returncode == 0
