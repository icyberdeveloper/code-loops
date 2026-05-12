"""Git worktree management for the subtask_iterator stage.

Each task gets its own worktree off the target project repo, branched as
`code-loops/<task_id>`. The iterator commits per-subtask in two phases
(tests, then code) with hard test-file protection in between.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class WorktreeError(RuntimeError):
    pass


class TestProtectionViolation(RuntimeError):
    """Coder modified files under tests/ — hard contract violation."""

    __test__ = False  # tell pytest not to try to collect this as a test class

    def __init__(self, files: list[str]):
        self.files = files
        super().__init__(f"Coder modified protected test files: {files}")


def _run_git(repo: Path, *args: str, check: bool = True, capture: bool = True) -> str:
    cmd = ["git", "-C", str(repo), *args]
    proc = subprocess.run(cmd, capture_output=capture, text=True, check=False)
    if check and proc.returncode != 0:
        raise WorktreeError(f"git {args[0]} failed (rc={proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


class Worktree:
    """Owns a single git worktree + its branch lifecycle.

    Created off `base_repo`'s current HEAD. All operations are scoped to
    `self.path`. Cleanup removes the worktree AND deletes the branch.
    """

    def __init__(self, base_repo: Path, branch: str, path: Path):
        self.base_repo = Path(base_repo)
        self.branch = branch
        self.path = Path(path)

    @classmethod
    def create(
        cls, base_repo: Path, branch: str, target_path: Path, *, force: bool = False
    ) -> Worktree:
        """Create a fresh worktree at target_path on a new branch off base_repo HEAD.

        If force=True and target_path or branch already exist, they're removed first.
        """
        base_repo = Path(base_repo).resolve()
        target_path = Path(target_path).resolve()

        if target_path.exists():
            if not force:
                raise WorktreeError(f"Target path already exists: {target_path}")
            # Best effort: try `git worktree remove --force`, then rmtree
            _run_git(base_repo, "worktree", "remove", "--force", str(target_path), check=False)
            if target_path.exists():
                shutil.rmtree(target_path)

        # Branch may exist from a prior failed run.
        existing = _run_git(base_repo, "branch", "--list", branch).strip()
        if existing:
            if not force:
                raise WorktreeError(f"Branch already exists: {branch}")
            _run_git(base_repo, "branch", "-D", branch, check=False)

        target_path.parent.mkdir(parents=True, exist_ok=True)
        _run_git(base_repo, "worktree", "add", "-b", branch, str(target_path))
        return cls(base_repo, branch, target_path)

    def cleanup(self) -> None:
        """Remove the worktree and delete the branch. Idempotent."""
        if self.path.exists():
            _run_git(self.base_repo, "worktree", "remove", "--force", str(self.path), check=False)
            if self.path.exists():
                shutil.rmtree(self.path, ignore_errors=True)
        _run_git(self.base_repo, "branch", "-D", self.branch, check=False)

    # ---- commits ----

    def head_sha(self) -> str:
        return _run_git(self.path, "rev-parse", "HEAD").strip()

    def commit_all(
        self, message: str, author_name: str = "code-loops", author_email: str = "code-loops@local"
    ) -> str:
        """Stage all changes and commit. Returns the new HEAD sha. Empty commits raise."""
        _run_git(self.path, "add", "-A")
        status = _run_git(self.path, "status", "--porcelain").strip()
        if not status:
            raise WorktreeError("Nothing to commit (worktree clean)")
        _run_git(
            self.path,
            "-c",
            f"user.name={author_name}",
            "-c",
            f"user.email={author_email}",
            "commit",
            "-m",
            message,
        )
        return self.head_sha()

    # ---- diffs ----

    def files_changed_since(self, since_sha: str) -> list[str]:
        """Files modified between since_sha and current HEAD (or working tree if dirty)."""
        out = _run_git(self.path, "diff", "--name-only", since_sha)
        return [line for line in out.splitlines() if line.strip()]

    def diff_since(self, since_sha: str) -> str:
        """Patch text of changes since since_sha."""
        return _run_git(self.path, "diff", since_sha)

    def diff_vs_base(self) -> str:
        """Full patch vs the branch we forked from (its initial HEAD)."""
        # The first commit on this branch is the worktree-add point's parent on the base.
        # Use merge-base with the default branch.
        base_sha = self._base_branch_sha()
        return _run_git(self.path, "diff", base_sha)

    def files_vs_base(self) -> set[str]:
        """All files changed across the whole branch vs base."""
        base_sha = self._base_branch_sha()
        out = _run_git(self.path, "diff", "--name-only", base_sha)
        return {line for line in out.splitlines() if line.strip()}

    def _base_branch_sha(self) -> str:
        """SHA of the commit this branch was created from."""
        # When `git worktree add -b <branch> <path>` runs, the new branch starts
        # at the current HEAD of base_repo. We can find this by walking back
        # to the merge-base of HEAD with the parent. Since the new branch
        # starts at HEAD, the initial HEAD == base ref.
        # We use `git rev-list <branch> ^HEAD~N` style — but simpler: look at
        # the first commit on this branch by parent count.
        # Pragmatic: assume worktree was just created so HEAD == base for the
        # first iterator entry; track explicitly via a tag.
        try:
            return _run_git(self.path, "rev-parse", "code-loops/base").strip()
        except WorktreeError:
            # Tag missing — return current HEAD (no diff yet)
            return self.head_sha()

    def tag_base(self) -> None:
        """Tag the current HEAD as `code-loops/base` for later diff_vs_base."""
        sha = self.head_sha()
        _run_git(self.path, "tag", "-f", "code-loops/base", sha, check=True)

    # ---- test protection ----
    #
    # Configurable per project via project.yaml `test_infrastructure`:
    #   - test_paths: dirs (relative to worktree root) the coder MUST NOT touch
    #   - lock_strategy: "chmod_444_dir" (default) or "none" (no chmod, only
    #     post-hoc git-diff guard via assert_no_test_changes)
    #
    # Defaults (when called without project_config in scope):
    #   test_paths = ["tests"], lock_strategy = "chmod_444_dir"
    # — preserves prior behavior for Python-default projects.

    def lock_tests_readonly(
        self,
        test_paths: list[str] | None = None,
        strategy: str = "chmod_444_dir",
    ) -> list[Path]:
        """chmod 444 on each path in test_paths. Returns flat list of locked files.

        No-op if strategy="none" or test_paths is empty/None. Missing paths
        are silently skipped (project may not have created tests/ yet).
        """
        if strategy == "none":
            return []
        if test_paths is None:
            test_paths = ["tests"]
        locked: list[Path] = []
        for rel in test_paths:
            tests_dir = self.path / rel
            if not tests_dir.exists():
                continue
            for root, _dirs, files in os.walk(tests_dir):
                for f in files:
                    p = Path(root) / f
                    p.chmod(0o444)
                    locked.append(p)
        return locked

    def unlock_tests(
        self,
        test_paths: list[str] | None = None,
        strategy: str = "chmod_444_dir",
    ) -> None:
        """Restore writability on each path in test_paths.

        No-op if strategy="none" (lock was no-op too). Missing paths skipped.
        """
        if strategy == "none":
            return
        if test_paths is None:
            test_paths = ["tests"]
        for rel in test_paths:
            tests_dir = self.path / rel
            if not tests_dir.exists():
                continue
            for root, _dirs, files in os.walk(tests_dir):
                for f in files:
                    (Path(root) / f).chmod(0o644)

    def assert_no_test_changes(
        self,
        since_sha: str,
        test_paths: list[str] | None = None,
    ) -> None:
        """Hard guard: raise TestProtectionViolation if any test path file moved.

        Checks both committed AND working-tree changes since since_sha.
        Always runs regardless of lock_strategy — git-diff guard is a separate
        layer of defense (catches violations the chmod missed, e.g. coder ran
        chmod itself, or chmod was never applied because strategy="none").

        No-op if test_paths is empty (project opted out of test protection).
        """
        if test_paths is None:
            test_paths = ["tests"]
        if not test_paths:
            return
        # Build matchers — a file is "in test_paths" if its relative path
        # starts with any of the configured prefixes.
        normalized = [p.rstrip("/") for p in test_paths]
        # Committed changes
        committed = _run_git(self.path, "diff", "--name-only", since_sha)
        # Working tree (unstaged + staged)
        working = _run_git(self.path, "status", "--porcelain")
        violations: list[str] = []
        for line in committed.splitlines():
            line = line.strip()
            if _path_under_any(line, normalized):
                violations.append(line)
        for line in working.splitlines():
            # status --porcelain format: " M tests/foo.py"
            parts = line.strip().split(maxsplit=1)
            if len(parts) >= 2:
                fname = parts[-1]
                if _path_under_any(fname, normalized):
                    violations.append(fname)
        if violations:
            raise TestProtectionViolation(sorted(set(violations)))


def _path_under_any(file_path: str, prefixes: list[str]) -> bool:
    """True if file_path is inside any of the given prefixes.

    Normalizes both sides — strips trailing slashes from prefixes so
    `tests/` and `tests` both match `tests/foo.py`.
    """
    for prefix in prefixes:
        norm = prefix.rstrip("/")
        if file_path == norm:
            return True
        if file_path.startswith(norm + "/"):
            return True
    return False
