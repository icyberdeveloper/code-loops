"""Tests for project_loader — resolution order + parse + accessors."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from code_loops import project_loader
from code_loops.project_loader import (
    PROJECTS_DIR,
    _resolve_path,
    get_base_repo,
    get_brief_path,
    get_test_infrastructure,
    inject_project_brief,
    list_projects,
    load_project_config,
    read_brief,
)

# ---- _resolve_path ----


def test_resolve_path_explicit_arg_wins(tmp_path):
    explicit = tmp_path / "custom.yaml"
    assert _resolve_path(explicit, name=None) == explicit


def test_resolve_name_uses_projects_dir(monkeypatch, tmp_path):
    fake_projects = tmp_path / "projects"
    monkeypatch.setattr(project_loader, "PROJECTS_DIR", fake_projects)
    assert _resolve_path(None, name="foo") == fake_projects / "foo" / "project.yaml"


def test_resolve_path_falls_back_to_env_var(monkeypatch, tmp_path):
    env_path = tmp_path / "via_env.yaml"
    monkeypatch.setenv("CODE_LOOPS_PROJECT", str(env_path))
    monkeypatch.setattr(project_loader, "PROJECTS_DIR", tmp_path / "no_projects_here")
    assert _resolve_path(None, name=None) == env_path


def test_resolve_autoselects_single_project(monkeypatch, tmp_path):
    monkeypatch.delenv("CODE_LOOPS_PROJECT", raising=False)
    fake_projects = tmp_path / "projects"
    (fake_projects / "only_one").mkdir(parents=True)
    (fake_projects / "only_one" / "project.yaml").write_text("project:\n  name: x\n")
    monkeypatch.setattr(project_loader, "PROJECTS_DIR", fake_projects)
    assert _resolve_path(None, name=None) == fake_projects / "only_one" / "project.yaml"


def test_resolve_raises_when_multi_project_no_explicit(monkeypatch, tmp_path):
    """Multi-project without explicit choice = loud error, not silent None.

    Before this fix: returned None silently, caused agents to run without
    {PROJECT_BRIEF} during Step 10's first production run.
    """
    monkeypatch.delenv("CODE_LOOPS_PROJECT", raising=False)
    fake_projects = tmp_path / "projects"
    for n in ("a", "b"):
        (fake_projects / n).mkdir(parents=True)
        (fake_projects / n / "project.yaml").write_text("project:\n  name: x\n")
    monkeypatch.setattr(project_loader, "PROJECTS_DIR", fake_projects)
    with pytest.raises(ValueError, match="Multiple projects configured"):
        _resolve_path(None, name=None)


def test_resolve_returns_none_when_projects_dir_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("CODE_LOOPS_PROJECT", raising=False)
    monkeypatch.setattr(project_loader, "PROJECTS_DIR", tmp_path / "absent")
    assert _resolve_path(None, name=None) is None


def test_resolve_explicit_path_wins_over_name_and_env(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.yaml"
    monkeypatch.setenv("CODE_LOOPS_PROJECT", "/somewhere/else.yaml")
    assert _resolve_path(explicit, name="ignored") == explicit


# ---- load_project_config ----


def test_load_returns_none_for_nonexistent_path(tmp_path):
    assert load_project_config(tmp_path / "nope.yaml") is None


def test_load_parses_and_annotates_source(tmp_path):
    cfg_path = tmp_path / "p.yaml"
    cfg_path.write_text(yaml.safe_dump({"project": {"name": "myproj", "base_repo": "/repo"}}))
    cfg = load_project_config(cfg_path)
    assert cfg["project"]["name"] == "myproj"
    assert cfg["_source_path"] == str(cfg_path)


def test_load_rejects_non_mapping_yaml(tmp_path):
    cfg_path = tmp_path / "p.yaml"
    cfg_path.write_text("- list\n- not_a_map\n")
    with pytest.raises(ValueError, match="must parse to a YAML mapping"):
        load_project_config(cfg_path)


def test_load_by_name(monkeypatch, tmp_path):
    fake_projects = tmp_path / "projects"
    (fake_projects / "foo").mkdir(parents=True)
    (fake_projects / "foo" / "project.yaml").write_text(
        yaml.safe_dump({"project": {"name": "foo", "base_repo": "/x"}})
    )
    monkeypatch.setattr(project_loader, "PROJECTS_DIR", fake_projects)
    cfg = load_project_config(name="foo")
    assert cfg["project"]["name"] == "foo"


# ---- list_projects ----


def test_list_projects_returns_sorted_names(monkeypatch, tmp_path):
    fake_projects = tmp_path / "projects"
    for n in ("zeta", "alpha", "mu"):
        (fake_projects / n).mkdir(parents=True)
        (fake_projects / n / "project.yaml").write_text("project:\n  name: x\n")
    # one entry without project.yaml — must be skipped
    (fake_projects / "incomplete").mkdir()
    monkeypatch.setattr(project_loader, "PROJECTS_DIR", fake_projects)
    assert list_projects() == ["alpha", "mu", "zeta"]


def test_list_projects_empty_when_dir_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(project_loader, "PROJECTS_DIR", tmp_path / "absent")
    assert list_projects() == []


# ---- get_base_repo ----


def test_get_base_repo_returns_path():
    cfg = {"project": {"base_repo": "/home/x/repo"}}
    assert get_base_repo(cfg) == Path("/home/x/repo")


def test_get_base_repo_returns_none_for_empty():
    assert get_base_repo(None) is None
    assert get_base_repo({}) is None
    assert get_base_repo({"project": {"name": "x"}}) is None


# ---- get_brief_path / read_brief ----


def test_get_brief_path_relative_resolves_against_source(tmp_path):
    cfg_path = tmp_path / "p.yaml"
    cfg_path.write_text("dummy")  # contents irrelevant; we build cfg manually
    cfg = {
        "brief_file": "brief.md",
        "_source_path": str(cfg_path),
    }
    assert get_brief_path(cfg) == tmp_path / "brief.md"


def test_get_brief_path_absolute_passes_through(tmp_path):
    abs_brief = tmp_path / "abs.md"
    cfg = {"brief_file": str(abs_brief), "_source_path": str(tmp_path / "p.yaml")}
    assert get_brief_path(cfg) == abs_brief


def test_get_brief_path_returns_none_when_no_brief_file():
    assert get_brief_path({"project": {"name": "x"}, "_source_path": "/tmp/p.yaml"}) is None


def test_read_brief_returns_content(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("# Project Brief\n\nstuff")
    cfg = {"brief_file": "brief.md", "_source_path": str(tmp_path / "p.yaml")}
    assert read_brief(cfg) == "# Project Brief\n\nstuff"


def test_read_brief_returns_none_when_file_missing(tmp_path):
    cfg = {"brief_file": "missing.md", "_source_path": str(tmp_path / "p.yaml")}
    assert read_brief(cfg) is None


# ---- PROJECTS_DIR sanity ----


def test_projects_dir_lives_inside_code_loops_repo():
    """Documents the layout: projects/ is at the code-loops repo root."""
    assert PROJECTS_DIR.name == "projects"
    # Resolves to a path inside the code-loops repo (sibling of project_loader.py)
    assert PROJECTS_DIR.parent.name == "code-loops"


# ---- inject_project_brief ----


def test_inject_no_op_when_placeholder_absent(tmp_path):
    cfg = {"brief_file": "brief.md", "_source_path": str(tmp_path / "p.yaml")}
    (tmp_path / "brief.md").write_text("BRIEF")
    assert inject_project_brief("plain prompt", cfg) == "plain prompt"


def test_inject_replaces_placeholder_with_brief(tmp_path):
    (tmp_path / "brief.md").write_text("# Project Brief\n\nstuff")
    cfg = {"brief_file": "brief.md", "_source_path": str(tmp_path / "p.yaml")}
    out = inject_project_brief("before\n\n{PROJECT_BRIEF}\n\nafter", cfg)
    assert "{PROJECT_BRIEF}" not in out
    assert "# Project Brief\n\nstuff" in out
    assert "before" in out and "after" in out


def test_inject_uses_fallback_when_no_config():
    out = inject_project_brief("a {PROJECT_BRIEF} b", None)
    assert "{PROJECT_BRIEF}" not in out
    assert "no project brief configured" in out


def test_inject_uses_fallback_when_brief_file_missing(tmp_path):
    cfg = {"brief_file": "missing.md", "_source_path": str(tmp_path / "p.yaml")}
    out = inject_project_brief("a {PROJECT_BRIEF} b", cfg)
    assert "no project brief configured" in out


def test_inject_handles_multiple_placeholder_occurrences(tmp_path):
    (tmp_path / "brief.md").write_text("BRIEF")
    cfg = {"brief_file": "brief.md", "_source_path": str(tmp_path / "p.yaml")}
    out = inject_project_brief("{PROJECT_BRIEF} and {PROJECT_BRIEF}", cfg)
    assert out == "BRIEF and BRIEF"


# ---- get_test_infrastructure ----


def test_test_infra_returns_defaults_when_no_config():
    out = get_test_infrastructure(None)
    assert out == {
        "enabled": True,
        "test_paths": ["tests"],
        "lock_strategy": "chmod_444_dir",
    }


def test_test_infra_returns_defaults_when_section_absent():
    out = get_test_infrastructure({"project": {"name": "x"}})
    assert out["enabled"] is True
    assert out["test_paths"] == ["tests"]
    assert out["lock_strategy"] == "chmod_444_dir"


def test_test_infra_disabled_overrides_default():
    out = get_test_infrastructure({"test_infrastructure": {"enabled": False}})
    assert out["enabled"] is False
    # Other fields keep defaults
    assert out["test_paths"] == ["tests"]


def test_test_infra_custom_paths_and_none_strategy():
    out = get_test_infrastructure(
        {
            "test_infrastructure": {
                "test_paths": ["src/spec", "e2e"],
                "lock_strategy": "none",
            }
        }
    )
    assert out["test_paths"] == ["src/spec", "e2e"]
    assert out["lock_strategy"] == "none"


def test_test_infra_rejects_invalid_lock_strategy():
    with pytest.raises(ValueError, match="lock_strategy must be one of"):
        get_test_infrastructure({"test_infrastructure": {"lock_strategy": "not_a_strategy"}})


def test_test_infra_rejects_non_list_test_paths():
    with pytest.raises(ValueError, match="test_paths must be a list of strings"):
        get_test_infrastructure({"test_infrastructure": {"test_paths": "tests"}})


def test_test_infra_rejects_non_string_path_items():
    with pytest.raises(ValueError, match="test_paths must be a list of strings"):
        get_test_infrastructure({"test_infrastructure": {"test_paths": ["tests", 42]}})


def test_test_infra_empty_paths_list_allowed():
    out = get_test_infrastructure({"test_infrastructure": {"test_paths": []}})
    assert out["test_paths"] == []
