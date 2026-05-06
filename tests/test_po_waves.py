"""Tests for c3.po.manifest.compute_waves and build_wave_manifest_text."""

from __future__ import annotations

import textwrap

import pytest

from c3.po.manifest import (
    build_wave_manifest_text,
    compute_waves,
    extract_frontmatter,
)


def _task(id_: str, depends_on: list[str] | None = None, **extras) -> dict:
    task = {
        "id": id_,
        "agent": "tdd-develop",
        "read_only": False,
        "prompt": f"do {id_}",
    }
    if depends_on is not None:
        task["depends_on"] = depends_on
    task.update(extras)
    return task


def _fm(*tasks: dict, **extras) -> dict:
    fm = {
        "po_plan_version": "0.1",
        "name": "test-plan",
        "cwd": "../..",
        "tasks": list(tasks),
    }
    fm.update(extras)
    return fm


# ---------------------------------------------------------------------------
# compute_waves
# ---------------------------------------------------------------------------


def test_all_independent_form_one_wave():
    fm = _fm(_task("a"), _task("b"), _task("c"))
    waves = compute_waves(fm)
    assert len(waves) == 1
    assert [t["id"] for t in waves[0]] == ["a", "b", "c"]


def test_full_serial_chain_forms_n_waves():
    fm = _fm(
        _task("a"),
        _task("b", depends_on=["a"]),
        _task("c", depends_on=["b"]),
        _task("d", depends_on=["c"]),
    )
    waves = compute_waves(fm)
    assert [[t["id"] for t in w] for w in waves] == [["a"], ["b"], ["c"], ["d"]]


def test_recommended_pattern_three_dev_then_review():
    """3 並列 dev → 末尾 reviewer の推奨パターン: 2 wave に分かれる。"""
    fm = _fm(
        _task("tdd-login"),
        _task("tdd-logout"),
        _task("tdd-reset"),
        _task(
            "review-auth",
            depends_on=["tdd-login", "tdd-logout", "tdd-reset"],
            agent="code-reviewer",
            read_only=True,
        ),
    )
    waves = compute_waves(fm)
    assert len(waves) == 2
    assert [t["id"] for t in waves[0]] == ["tdd-login", "tdd-logout", "tdd-reset"]
    assert [t["id"] for t in waves[1]] == ["review-auth"]


def test_diamond_dag():
    """A → B,C → D"""
    fm = _fm(
        _task("a"),
        _task("b", depends_on=["a"]),
        _task("c", depends_on=["a"]),
        _task("d", depends_on=["b", "c"]),
    )
    waves = compute_waves(fm)
    assert [[t["id"] for t in w] for w in waves] == [["a"], ["b", "c"], ["d"]]


def test_cycle_raises():
    fm = _fm(
        _task("a", depends_on=["b"]),
        _task("b", depends_on=["a"]),
    )
    with pytest.raises(ValueError, match="cycle"):
        compute_waves(fm)


def test_unknown_dependency_raises():
    fm = _fm(_task("a", depends_on=["ghost"]))
    with pytest.raises(ValueError, match="ghost"):
        compute_waves(fm)


def test_duplicate_id_raises():
    fm = _fm(_task("a"), _task("a"))
    with pytest.raises(ValueError, match="duplicate"):
        compute_waves(fm)


def test_empty_tasks_returns_empty():
    fm = _fm()
    fm["tasks"] = []
    assert compute_waves(fm) == []


def test_wave_membership_within_wave_is_alphabetical():
    """Sorting tasks by id within a wave keeps output deterministic across runs."""
    fm = _fm(_task("zeta"), _task("alpha"), _task("mu"))
    waves = compute_waves(fm)
    assert [t["id"] for t in waves[0]] == ["alpha", "mu", "zeta"]


# ---------------------------------------------------------------------------
# build_wave_manifest_text
# ---------------------------------------------------------------------------


def test_wave_manifest_drops_depends_on():
    fm = _fm(
        _task("a"),
        _task("b", depends_on=["a"]),
        _task("c", depends_on=["a"]),
    )
    text = build_wave_manifest_text(fm, wave_index=1)
    assert "depends_on" not in text
    assert "id: b" in text
    assert "id: c" in text
    assert "id: a" not in text  # not in this wave


def test_wave_manifest_preserves_required_top_level_fields():
    fm = _fm(_task("a"))
    text = build_wave_manifest_text(fm, wave_index=0)
    assert 'po_plan_version: "0.1"' in text
    assert 'cwd: "../.."' in text
    assert "name:" in text
    assert "wave 0" in text  # name is decorated with the wave index


def test_wave_manifest_drops_webhooks():
    fm = _fm(
        _task("a"),
        on_complete={"webhook_url": "https://example.com/done"},
        on_failure={"webhook_url": "https://example.com/fail"},
    )
    text = build_wave_manifest_text(fm, wave_index=0)
    assert "on_complete" not in text
    assert "on_failure" not in text
    assert "webhook_url" not in text


def test_wave_manifest_emits_writes():
    fm = _fm(_task("a", writes=["src/x.py", "tests/test_x.py"]))
    text = build_wave_manifest_text(fm, wave_index=0)
    assert "writes:" in text
    assert '- "src/x.py"' in text
    assert '- "tests/test_x.py"' in text


def test_wave_manifest_emits_multiline_prompt_as_block():
    multi = "line1\nline2\nline3"
    fm = _fm(_task("a", prompt=multi))
    text = build_wave_manifest_text(fm, wave_index=0)
    assert "prompt: |" in text
    assert "line1" in text
    assert "line2" in text


def test_wave_manifest_index_out_of_range_raises():
    fm = _fm(_task("a"))
    with pytest.raises(IndexError):
        build_wave_manifest_text(fm, wave_index=5)


def test_wave_manifest_round_trips_through_extract_frontmatter(tmp_path):
    """The text produced by build_wave_manifest_text must be re-parseable
    by extract_frontmatter (which feeds c3 po dry-run).
    """
    fm = _fm(
        _task("tdd-login", writes=["src/auth/login.py"]),
        _task("tdd-logout", writes=["src/auth/logout.py"]),
        _task(
            "review",
            depends_on=["tdd-login", "tdd-logout"],
            agent="code-reviewer",
            read_only=True,
        ),
    )
    text = build_wave_manifest_text(fm, wave_index=0)
    path = tmp_path / "wave.md"
    path.write_text(text, encoding="utf-8")
    parsed = extract_frontmatter(path)
    assert parsed is not None
    assert parsed["po_plan_version"] == "0.1"
    assert len(parsed["tasks"]) == 2
    ids = {t["id"] for t in parsed["tasks"]}
    assert ids == {"tdd-login", "tdd-logout"}
