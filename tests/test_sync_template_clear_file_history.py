"""Tests for sync between template and main clear_file_history.py."""

import os

WORKTREE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_FILE = os.path.join(WORKTREE_ROOT, '.claude', 'hooks', 'clear_file_history.py')
TEMPLATE_FILE = os.path.join(
    WORKTREE_ROOT, 'src', 'c3', '_template', '.claude', 'hooks', 'clear_file_history.py'
)


def _read_file(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def test_template_file_exists():
    """テンプレートファイルが存在すること。"""
    assert os.path.isfile(TEMPLATE_FILE), (
        f'Template file does not exist: {TEMPLATE_FILE}'
    )


def test_template_contains_islink_check():
    """テンプレートの内容に os.path.islink(full_path) が含まれること。"""
    content = _read_file(TEMPLATE_FILE)
    assert 'os.path.islink(full_path)' in content, (
        'os.path.islink(full_path) が見つかりません。'
    )


def test_template_contains_already_deleted_comment():
    """テンプレートの内容に # already deleted by another process コメントが含まれること。"""
    content = _read_file(TEMPLATE_FILE)
    assert '# already deleted by another process' in content, (
        '# already deleted by another process コメントが見つかりません。'
    )


def test_template_matches_main():
    """テンプレートの内容が本体の内容と完全一致すること。"""
    main_content = _read_file(MAIN_FILE)
    template_content = _read_file(TEMPLATE_FILE)
    assert template_content == main_content, (
        'テンプレートと本体の内容が一致しません。\n'
        f'本体: {MAIN_FILE}\n'
        f'テンプレート: {TEMPLATE_FILE}'
    )
