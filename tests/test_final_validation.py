"""Tests for final_validation action — RFC file coverage parser + integration."""

from __future__ import annotations

from pathlib import Path

from code_loops.stages.final_validation import (
    _build_coverage_md,
    _parse_rfc_files,
    _read_diff_files,
)

# ---- _parse_rfc_files ----


def test_parse_rfc_files_basic(tmp_path: Path):
    rfc = tmp_path / "rfc.md"
    rfc.write_text(
        """\
# RFC

## Context
blah

## File-level changes
- `app/foo.py` — add X
- `app/bar.py` (new) — create
- `tests/test_foo.py` — add tests

## Risks
- something
"""
    )
    files = _parse_rfc_files(rfc)
    assert files == {"app/foo.py", "app/bar.py", "tests/test_foo.py"}


def test_parse_rfc_files_no_section_returns_empty(tmp_path: Path):
    rfc = tmp_path / "rfc.md"
    rfc.write_text("# RFC\n\n## Context\nno file-level section")
    assert _parse_rfc_files(rfc) == set()


def test_parse_rfc_files_handles_dash_and_emdash(tmp_path: Path):
    rfc = tmp_path / "rfc.md"
    rfc.write_text(
        """\
## File-level changes
- `app/a.py` — emdash separator
- `app/b.py` - hyphen separator
"""
    )
    files = _parse_rfc_files(rfc)
    assert files == {"app/a.py", "app/b.py"}


def test_parse_rfc_files_missing_file_returns_empty(tmp_path: Path):
    assert _parse_rfc_files(tmp_path / "nope.md") == set()


# ---- _read_diff_files ----


def test_read_diff_files_strips_blank_lines(tmp_path: Path):
    f = tmp_path / "files.txt"
    f.write_text("app/a.py\n\napp/b.py\n  \napp/c.py\n")
    assert _read_diff_files(f) == {"app/a.py", "app/b.py", "app/c.py"}


def test_read_diff_files_missing_returns_empty(tmp_path: Path):
    assert _read_diff_files(tmp_path / "nope.txt") == set()


# ---- _build_coverage_md ----


def test_coverage_md_complete_match():
    md = _build_coverage_md({"a.py", "b.py"}, {"a.py", "b.py"}, [], [])
    assert "Diff exactly matches" in md
    # Section headers absent (counts may still mention these words)
    assert "## Missing files" not in md
    assert "## Unexpected files" not in md


def test_coverage_md_lists_missing_and_unexpected():
    md = _build_coverage_md(
        {"a.py", "b.py", "c.py"},
        {"a.py", "extra.py"},
        ["b.py", "c.py"],
        ["extra.py"],
    )
    assert "BLOCKING" in md
    assert "`b.py`" in md
    assert "`c.py`" in md
    assert "Unexpected files" in md
    assert "`extra.py`" in md
