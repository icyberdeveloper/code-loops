"""Tests for Worktree class — uses real git in tmp_path repos."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_loops.worktree import (
    RoleScopeViolation,
    TestProtectionViolation,
    Worktree,
    WorktreeError,
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout


def _make_base_repo(tmp_path: Path) -> Path:
    """Create a tiny git repo with one commit and a tests/ folder."""
    repo = tmp_path / "base"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@x")
    (repo / "README.md").write_text("base repo\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_existing.py").write_text("def test_x(): assert True\n")
    (repo / "app").mkdir()
    (repo / "app" / "main.py").write_text("def hello(): return 'hi'\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-m", "init")
    return repo


# ---- create / cleanup ----


def test_create_makes_worktree_on_new_branch(tmp_path):
    base = _make_base_repo(tmp_path)
    target = tmp_path / "wt"
    wt = Worktree.create(base, "code-loops/test", target)

    assert wt.path == target.resolve()
    assert (wt.path / "README.md").exists()
    assert (wt.path / "app" / "main.py").exists()
    # On the new branch
    branch = _git(wt.path, "branch", "--show-current").strip()
    assert branch == "code-loops/test"


def test_create_fails_if_target_exists_without_force(tmp_path):
    base = _make_base_repo(tmp_path)
    target = tmp_path / "wt"
    target.mkdir()
    with pytest.raises(WorktreeError, match="Target path already exists"):
        Worktree.create(base, "code-loops/test", target)


def test_create_force_overrides_existing(tmp_path):
    base = _make_base_repo(tmp_path)
    target = tmp_path / "wt"
    Worktree.create(base, "code-loops/test", target)
    # Now try to create again with force
    wt = Worktree.create(base, "code-loops/test", target, force=True)
    assert wt.path.exists()


def test_cleanup_removes_worktree_and_branch(tmp_path):
    base = _make_base_repo(tmp_path)
    target = tmp_path / "wt"
    wt = Worktree.create(base, "code-loops/test", target)
    wt.cleanup()
    assert not target.exists()
    branches = _git(base, "branch", "--list", "code-loops/test").strip()
    assert branches == ""


def test_cleanup_is_idempotent(tmp_path):
    base = _make_base_repo(tmp_path)
    target = tmp_path / "wt"
    wt = Worktree.create(base, "code-loops/test", target)
    wt.cleanup()
    wt.cleanup()  # no error second time


# ---- commits + diffs ----


def test_commit_all_returns_sha_and_advances_head(tmp_path):
    base = _make_base_repo(tmp_path)
    wt = Worktree.create(base, "code-loops/test", tmp_path / "wt")
    initial = wt.head_sha()
    (wt.path / "app" / "new_module.py").write_text("# new\n")
    sha = wt.commit_all("add new_module")
    assert sha != initial
    assert wt.head_sha() == sha


def test_commit_all_raises_on_clean_worktree(tmp_path):
    base = _make_base_repo(tmp_path)
    wt = Worktree.create(base, "code-loops/test", tmp_path / "wt")
    with pytest.raises(WorktreeError, match="Nothing to commit"):
        wt.commit_all("empty")


def test_diff_since_captures_all_changes(tmp_path):
    base = _make_base_repo(tmp_path)
    wt = Worktree.create(base, "code-loops/test", tmp_path / "wt")
    (wt.path / "app" / "main.py").write_text("def hello(): return 'changed'\n")
    diff = wt.diff_since(wt.head_sha())
    assert "changed" in diff
    assert "app/main.py" in diff


def test_files_changed_since_lists_paths(tmp_path):
    base = _make_base_repo(tmp_path)
    wt = Worktree.create(base, "code-loops/test", tmp_path / "wt")
    base_sha = wt.head_sha()
    (wt.path / "app" / "x.py").write_text("# x\n")
    (wt.path / "app" / "y.py").write_text("# y\n")
    wt.commit_all("add x and y")
    changed = wt.files_changed_since(base_sha)
    assert set(changed) == {"app/x.py", "app/y.py"}


def test_tag_base_and_diff_vs_base(tmp_path):
    base = _make_base_repo(tmp_path)
    wt = Worktree.create(base, "code-loops/test", tmp_path / "wt")
    wt.tag_base()
    (wt.path / "app" / "feature.py").write_text("# new feature\n")
    wt.commit_all("add feature")
    files = wt.files_vs_base()
    assert "app/feature.py" in files
    diff = wt.diff_vs_base()
    assert "feature.py" in diff


# ---- test protection ----


def test_lock_tests_readonly_chmods_files(tmp_path):
    base = _make_base_repo(tmp_path)
    wt = Worktree.create(base, "code-loops/test", tmp_path / "wt")
    locked = wt.lock_tests_readonly()
    assert (wt.path / "tests" / "test_existing.py") in [Path(p) for p in locked]
    # Verify mode
    mode = (wt.path / "tests" / "test_existing.py").stat().st_mode & 0o777
    assert mode == 0o444


def test_unlock_tests_restores_writability(tmp_path):
    base = _make_base_repo(tmp_path)
    wt = Worktree.create(base, "code-loops/test", tmp_path / "wt")
    wt.lock_tests_readonly()
    wt.unlock_tests()
    mode = (wt.path / "tests" / "test_existing.py").stat().st_mode & 0o777
    assert mode == 0o644


def test_assert_no_test_changes_passes_when_only_app_changed(tmp_path):
    base = _make_base_repo(tmp_path)
    wt = Worktree.create(base, "code-loops/test", tmp_path / "wt")
    base_sha = wt.head_sha()
    (wt.path / "app" / "main.py").write_text("def hello(): return 'new'\n")
    wt.commit_all("change app")
    wt.assert_no_test_changes(base_sha)  # should not raise


def test_assert_no_test_changes_raises_on_committed_test_change(tmp_path):
    base = _make_base_repo(tmp_path)
    wt = Worktree.create(base, "code-loops/test", tmp_path / "wt")
    base_sha = wt.head_sha()
    (wt.path / "tests" / "test_existing.py").write_text("def test_z(): pass\n")
    wt.commit_all("modify test")
    with pytest.raises(TestProtectionViolation) as exc:
        wt.assert_no_test_changes(base_sha)
    assert "tests/test_existing.py" in exc.value.files


def test_assert_no_test_changes_catches_uncommitted_test_change(tmp_path):
    base = _make_base_repo(tmp_path)
    wt = Worktree.create(base, "code-loops/test", tmp_path / "wt")
    base_sha = wt.head_sha()
    # Working-tree edit, NOT committed
    (wt.path / "tests" / "test_existing.py").write_text("def test_z(): pass\n")
    with pytest.raises(TestProtectionViolation) as exc:
        wt.assert_no_test_changes(base_sha)
    assert "tests/test_existing.py" in exc.value.files


def test_assert_no_test_changes_catches_new_test_file(tmp_path):
    base = _make_base_repo(tmp_path)
    wt = Worktree.create(base, "code-loops/test", tmp_path / "wt")
    base_sha = wt.head_sha()
    (wt.path / "tests" / "test_new.py").write_text("def test_q(): pass\n")
    with pytest.raises(TestProtectionViolation) as exc:
        wt.assert_no_test_changes(base_sha)
    assert "tests/test_new.py" in exc.value.files


# ---- per-role scope enforcement (Fix B) ----


def test_assert_only_touched_passes_when_allowed(tmp_path):
    """Role wrote file внутри can_write scope — no raise."""
    base = _make_base_repo(tmp_path)
    wt = Worktree.create(base, "code-loops/test-scope-ok", tmp_path / "wt-sc-ok")
    base_sha = wt.head_sha()
    (wt.path / "app" / "main.py").write_text("# changed\n")
    # Allowed list includes app/main.py exactly
    wt.assert_only_touched(["app/main.py"], since_sha=base_sha)


def test_assert_only_touched_raises_on_out_of_scope_write(tmp_path):
    """Role wrote file НЕ в can_write list — raises RoleScopeViolation."""
    base = _make_base_repo(tmp_path)
    wt = Worktree.create(base, "code-loops/test-scope-bad", tmp_path / "wt-sc-bad")
    base_sha = wt.head_sha()
    # eval_engineer был ограничен tests/eval_data/, но тронул tests/integration
    (wt.path / "tests" / "test_existing.py").write_text("# overstepped\n")
    with pytest.raises(RoleScopeViolation) as exc:
        wt.assert_only_touched(["tests/eval_data/baseline.json"], since_sha=base_sha)
    assert "tests/test_existing.py" in exc.value.violations
    assert "tests/eval_data/baseline.json" in exc.value.allowed


def test_assert_only_touched_handles_directory_prefix(tmp_path):
    """can_write поддерживает directory prefix — все files под prefix allowed."""
    base = _make_base_repo(tmp_path)
    wt = Worktree.create(base, "code-loops/test-dir-prefix", tmp_path / "wt-dir-pre")
    base_sha = wt.head_sha()
    (wt.path / "tests" / "test_existing.py").write_text("# changed\n")
    # tests/ directory prefix — should match tests/test_existing.py
    wt.assert_only_touched(["tests"], since_sha=base_sha)


def test_assert_only_touched_empty_allowed_blocks_any_write(tmp_path):
    """can_write: [] means role cannot write anything — any change raises."""
    base = _make_base_repo(tmp_path)
    wt = Worktree.create(base, "code-loops/test-empty-scope", tmp_path / "wt-empty-sc")
    base_sha = wt.head_sha()
    (wt.path / "app" / "main.py").write_text("# any change\n")
    with pytest.raises(RoleScopeViolation):
        wt.assert_only_touched([], since_sha=base_sha)


def test_assert_only_touched_catches_uncommitted_changes(tmp_path):
    """Out-of-scope working tree changes (uncommitted) trigger raise."""
    base = _make_base_repo(tmp_path)
    wt = Worktree.create(base, "code-loops/test-uncommit", tmp_path / "wt-uncommit")
    base_sha = wt.head_sha()
    # Working tree edit, NOT committed
    (wt.path / "tests" / "test_existing.py").write_text("# uncommitted\n")
    with pytest.raises(RoleScopeViolation) as exc:
        wt.assert_only_touched(["app/main.py"], since_sha=base_sha)
    assert any("tests/test_existing.py" in v for v in exc.value.violations)


# ---- configurable test_paths / lock_strategy ----


def test_lock_strategy_none_skips_chmod(tmp_path):
    base = _make_base_repo(tmp_path)
    wt = Worktree.create(base, "code-loops/test-strat-none", tmp_path / "wt-none")
    locked = wt.lock_tests_readonly(test_paths=["tests"], strategy="none")
    assert locked == []
    # File still writable
    test_file = wt.path / "tests" / "test_existing.py"
    assert test_file.stat().st_mode & 0o200  # owner-write bit set
    wt.cleanup()


def test_lock_custom_test_paths(tmp_path):
    base = _make_base_repo(tmp_path)
    # Add custom test dirs
    (base / "spec").mkdir()
    (base / "spec" / "thing_spec.py").write_text("# spec\n")
    (base / "e2e").mkdir()
    (base / "e2e" / "flow.py").write_text("# e2e\n")
    _git(base, "add", "-A")
    _git(base, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-m", "add specs")
    wt = Worktree.create(base, "code-loops/multi-paths", tmp_path / "wt-multi")
    locked = wt.lock_tests_readonly(test_paths=["spec", "e2e"], strategy="chmod_444_dir")
    locked_names = sorted(p.name for p in locked)
    assert locked_names == ["flow.py", "thing_spec.py"]
    wt.unlock_tests(test_paths=["spec", "e2e"])
    wt.cleanup()


def test_lock_skips_missing_test_paths(tmp_path):
    base = _make_base_repo(tmp_path)
    wt = Worktree.create(base, "code-loops/missing", tmp_path / "wt-missing")
    # Doesn't exist — silently skipped
    locked = wt.lock_tests_readonly(test_paths=["nonexistent_dir"])
    assert locked == []
    wt.cleanup()


def test_assert_no_test_changes_uses_custom_paths(tmp_path):
    base = _make_base_repo(tmp_path)
    (base / "spec").mkdir()
    (base / "spec" / "thing_spec.py").write_text("# spec\n")
    _git(base, "add", "-A")
    _git(base, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-m", "add spec")
    wt = Worktree.create(base, "code-loops/custom-guard", tmp_path / "wt-cguard")
    base_sha = wt.head_sha()
    # Modify the spec file (treated as test by custom config)
    (wt.path / "spec" / "thing_spec.py").write_text("# spec modified\n")
    wt.commit_all("touch spec")
    # Default paths (tests/) — no violation
    wt.assert_no_test_changes(base_sha, test_paths=["tests"])
    # Custom paths (spec/) — violation
    with pytest.raises(TestProtectionViolation) as exc:
        wt.assert_no_test_changes(base_sha, test_paths=["spec"])
    assert "spec/thing_spec.py" in exc.value.files
    wt.cleanup()


def test_assert_no_test_changes_empty_paths_is_noop(tmp_path):
    base = _make_base_repo(tmp_path)
    wt = Worktree.create(base, "code-loops/empty-guard", tmp_path / "wt-eguard")
    base_sha = wt.head_sha()
    (wt.path / "tests" / "test_existing.py").write_text("def test_x(): assert False\n")
    wt.commit_all("change tests")
    # Empty test_paths → no violation even if tests/ changed
    wt.assert_no_test_changes(base_sha, test_paths=[])
    wt.cleanup()


def test_rollback_paths_removes_new_files_not_in_tree(tmp_path):
    """Rollback к pre-role tree → newly-created file gets unlinked."""
    base = _make_base_repo(tmp_path)
    target = tmp_path / "wt"
    wt = Worktree.create(base, "code-loops/rollback-new", target)
    pre_tree = wt.snapshot_tree()  # baseline без new file
    (wt.path / "new_artifact.txt").write_text("created by role\n")
    assert (wt.path / "new_artifact.txt").exists()
    wt.rollback_paths(["new_artifact.txt"], pre_tree)
    assert not (wt.path / "new_artifact.txt").exists()


def test_rollback_paths_restores_modified_file_from_tree(tmp_path):
    """Rollback restores tracked file к its content в pre-role tree."""
    base = _make_base_repo(tmp_path)
    target = tmp_path / "wt"
    wt = Worktree.create(base, "code-loops/rollback-mod", target)
    pre_tree = wt.snapshot_tree()
    (wt.path / "app" / "main.py").write_text("def hello(): return 'CORRUPTED'\n")
    wt.rollback_paths(["app/main.py"], pre_tree)
    assert (wt.path / "app" / "main.py").read_text() == "def hello(): return 'hi'\n"


def test_rollback_paths_preserves_other_writes(tmp_path):
    """Rolling back violation file leaves other in-scope writes alone."""
    base = _make_base_repo(tmp_path)
    target = tmp_path / "wt"
    wt = Worktree.create(base, "code-loops/rollback-mixed", target)
    pre_tree = wt.snapshot_tree()
    (wt.path / "in_scope.json").write_text("{}\n")  # in-scope (kept)
    (wt.path / "out_of_scope.py").write_text("# bad\n")  # violation (rolled back)
    wt.rollback_paths(["out_of_scope.py"], pre_tree)
    assert (wt.path / "in_scope.json").exists()
    assert not (wt.path / "out_of_scope.py").exists()


def test_path_under_any_helper():
    from code_loops.worktree import _path_under_any

    # Exact match
    assert _path_under_any("tests", ["tests"]) is True
    # Inside dir
    assert _path_under_any("tests/foo.py", ["tests"]) is True
    assert _path_under_any("tests/sub/bar.py", ["tests"]) is True
    # Not inside (prefix collision)
    assert _path_under_any("testsuite/foo.py", ["tests"]) is False
    # Multiple prefixes
    assert _path_under_any("e2e/run.py", ["spec", "e2e"]) is True
    # Trailing slash normalized away
    assert _path_under_any("tests/x.py", ["tests/"]) is True
