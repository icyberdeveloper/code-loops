"""Tests для acceptance criteria checker — mechanical post-condition verification."""

from __future__ import annotations

import json

from code_loops.acceptance import (
    AcceptanceViolation,
    check_acceptance,
    format_violations,
)

# ---- file_contains_pattern ----


def test_file_contains_pattern_match(tmp_path):
    (tmp_path / "x.py").write_text("@pytest.mark.xfail\ndef test_foo(): ...\n")
    crit = [{"type": "file_contains_pattern", "file": "x.py", "pattern": r"@pytest\.mark\.xfail"}]
    assert check_acceptance(crit, tmp_path) == []


def test_file_contains_pattern_missing(tmp_path):
    (tmp_path / "x.py").write_text("def test_foo(): ...\n")
    crit = [{"type": "file_contains_pattern", "file": "x.py", "pattern": "xfail"}]
    violations = check_acceptance(crit, tmp_path)
    assert len(violations) == 1
    assert "not found" in violations[0].reason


def test_file_contains_pattern_file_absent(tmp_path):
    crit = [{"type": "file_contains_pattern", "file": "missing.py", "pattern": "x"}]
    violations = check_acceptance(crit, tmp_path)
    assert len(violations) == 1
    assert "does not exist" in violations[0].reason


# ---- file_not_contains_pattern ----


def test_file_not_contains_clean(tmp_path):
    (tmp_path / "x.py").write_text("def foo(): pass\n")
    crit = [{"type": "file_not_contains_pattern", "file": "x.py", "pattern": "TODO"}]
    assert check_acceptance(crit, tmp_path) == []


def test_file_not_contains_violated(tmp_path):
    (tmp_path / "x.py").write_text("# TODO: fix\n")
    crit = [{"type": "file_not_contains_pattern", "file": "x.py", "pattern": "TODO"}]
    violations = check_acceptance(crit, tmp_path)
    assert len(violations) == 1
    assert "forbidden pattern" in violations[0].reason


# ---- file_size_min ----


def test_file_size_min_satisfied(tmp_path):
    (tmp_path / "x.json").write_text("x" * 200)
    crit = [{"type": "file_size_min", "file": "x.json", "bytes": 100}]
    assert check_acceptance(crit, tmp_path) == []


def test_file_size_min_violated(tmp_path):
    (tmp_path / "x.json").write_text("x" * 50)
    crit = [{"type": "file_size_min", "file": "x.json", "bytes": 100}]
    violations = check_acceptance(crit, tmp_path)
    assert len(violations) == 1
    assert "size 50B < required 100B" in violations[0].reason


# ---- json_path_exists ----


def test_json_path_exists_found(tmp_path):
    (tmp_path / "data.json").write_text(json.dumps({"known": [{"name": "x"}]}))
    crit = [{"type": "json_path_exists", "file": "data.json", "path": ["known", 0, "name"]}]
    assert check_acceptance(crit, tmp_path) == []


def test_json_path_exists_missing_key(tmp_path):
    (tmp_path / "data.json").write_text(json.dumps({"other": "value"}))
    crit = [{"type": "json_path_exists", "file": "data.json", "path": ["known"]}]
    violations = check_acceptance(crit, tmp_path)
    assert len(violations) == 1
    assert "not found" in violations[0].reason


def test_json_path_exists_malformed_json(tmp_path):
    (tmp_path / "data.json").write_text("{not valid")
    crit = [{"type": "json_path_exists", "file": "data.json", "path": ["x"]}]
    violations = check_acceptance(crit, tmp_path)
    assert len(violations) == 1
    assert "parse failed" in violations[0].reason


# ---- unknown type ----


def test_unknown_type_yields_violation(tmp_path):
    crit = [{"type": "made_up", "file": "x.py"}]
    violations = check_acceptance(crit, tmp_path)
    assert len(violations) == 1
    assert "unknown check type" in violations[0].reason


# ---- format_violations ----


def test_format_violations_empty():
    assert format_violations([]) == ""


def test_format_violations_human():
    v = AcceptanceViolation({"type": "file_contains_pattern", "file": "x.py"}, "missing")
    text = format_violations([v])
    assert "ACCEPTANCE VIOLATIONS" in text
    assert "x.py" in text
    assert "missing" in text
