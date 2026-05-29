"""Tests для Claude Code permission sandbox — per-invocation settings.local.json."""

from __future__ import annotations

import json

from code_loops.sandbox import (
    DEFAULT_ALLOWED_TOOLS,
    SETTINGS_RELATIVE,
    build_settings,
    install_settings,
    scoped_sandbox,
    uninstall_settings,
)


def test_build_settings_emits_allow_per_path():
    s = build_settings(["app/foo.py", "tests/test_foo.py"])
    allow = s["permissions"]["allow"]
    assert "Write(app/foo.py)" in allow
    assert "Edit(app/foo.py)" in allow
    assert "Write(tests/test_foo.py)" in allow
    assert "Edit(tests/test_foo.py)" in allow


def test_build_settings_always_denies_dangerous_bash():
    s = build_settings(["x.py"])
    deny = s["permissions"]["deny"]
    assert any("rm -rf" in r for r in deny)
    assert any("git push" in r for r in deny)
    assert any("curl" in r and "| sh" in r for r in deny)


def test_build_settings_accepts_extra_deny():
    s = build_settings(["x.py"], extra_deny=["Bash(custom dangerous)"])
    assert "Bash(custom dangerous)" in s["permissions"]["deny"]


def test_install_settings_creates_file(tmp_path):
    path = install_settings(tmp_path, ["app/foo.py"])
    assert path.exists()
    assert path == tmp_path / SETTINGS_RELATIVE
    content = json.loads(path.read_text())
    assert "permissions" in content
    assert "Write(app/foo.py)" in content["permissions"]["allow"]


def test_install_settings_creates_parent_dir(tmp_path):
    # .claude/ dir doesn't exist yet
    install_settings(tmp_path, ["x.py"])
    assert (tmp_path / ".claude").is_dir()


def test_uninstall_settings_removes_file(tmp_path):
    install_settings(tmp_path, ["x.py"])
    uninstall_settings(tmp_path)
    assert not (tmp_path / SETTINGS_RELATIVE).exists()


def test_uninstall_settings_idempotent(tmp_path):
    # No file → no error
    uninstall_settings(tmp_path)


def test_scoped_sandbox_installs_and_cleans(tmp_path):
    with scoped_sandbox(tmp_path, ["a.py", "b.json"]):
        assert (tmp_path / SETTINGS_RELATIVE).exists()
        content = json.loads((tmp_path / SETTINGS_RELATIVE).read_text())
        assert "Write(a.py)" in content["permissions"]["allow"]
        assert "Write(b.json)" in content["permissions"]["allow"]
    # After exit — file cleaned up
    assert not (tmp_path / SETTINGS_RELATIVE).exists()


def test_scoped_sandbox_cleans_on_exception(tmp_path):
    try:
        with scoped_sandbox(tmp_path, ["x.py"]):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    # Settings still cleaned up
    assert not (tmp_path / SETTINGS_RELATIVE).exists()


def test_default_allowed_tools_includes_basic_set():
    assert "Read" in DEFAULT_ALLOWED_TOOLS
    assert "Edit" in DEFAULT_ALLOWED_TOOLS
    assert "Write" in DEFAULT_ALLOWED_TOOLS
    assert "Bash" in DEFAULT_ALLOWED_TOOLS
