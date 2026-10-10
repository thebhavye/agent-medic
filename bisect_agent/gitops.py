"""Thin wrappers around git. Commits are tested in throwaway worktrees so the user's
checkout is never touched."""

import shutil
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(*args: str, cwd: Path, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def rev_parse(rev: str, repo: Path) -> str:
    return git("rev-parse", "--verify", f"{rev}^{{commit}}", cwd=repo)


def subject(sha: str, repo: Path) -> str:
    return git("log", "-1", "--format=%s", sha, cwd=repo)


def message(sha: str, repo: Path) -> str:
    return git("log", "-1", "--format=%B", sha, cwd=repo)


def commits_between(good: str, bad: str, repo: Path) -> list[str]:
    """[good, ..., bad] along the first-parent line, oldest first."""
    if not is_ancestor(good, bad, repo):
        raise GitError(f"{good[:10]} is not an ancestor of {bad[:10]}")
    middle = git("rev-list", "--first-parent", "--reverse", f"{good}..{bad}", cwd=repo).split()
    return [good, *middle]


def is_ancestor(a: str, b: str, repo: Path) -> bool:
    """True when a is b or an ancestor of b."""
    proc = subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=repo, capture_output=True)
    return proc.returncode == 0


def show(sha: str, repo: Path) -> str:
    return git("show", "--format=commit %H%nAuthor: %an%nDate: %ad%n%n%B", sha, cwd=repo)


def changed_files(sha: str, repo: Path) -> list[str]:
    out = git("diff-tree", "--no-commit-id", "--name-only", "-r", sha, cwd=repo)
    return [p for p in out.splitlines() if p]


def added_lines(sha: str, path: str, repo: Path) -> list[str]:
    diff = git("show", "--format=", "--unified=0", sha, "--", path, cwd=repo)
    return [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]


def worktree_add(path: Path, rev: str, repo: Path, branch: str | None = None) -> None:
    worktree_remove(path, repo)
    if branch:
        git("worktree", "add", str(path), branch, cwd=repo)
    else:
        git("worktree", "add", "--detach", str(path), rev, cwd=repo)


def worktree_remove(path: Path, repo: Path) -> None:
    if path.exists():
        git("worktree", "remove", "--force", str(path), cwd=repo, check=False)
        shutil.rmtree(path, ignore_errors=True)
    git("worktree", "prune", cwd=repo, check=False)
