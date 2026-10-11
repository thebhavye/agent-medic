"""Thin wrappers around git. Commits are tested in throwaway worktrees so the user's
checkout is never touched."""

import os
import shutil
import signal
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


GIT_TIMEOUT_S = 600     # a commit runs the repo's hooks, and a hook can hang


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def git(*args: str, cwd: Path, check: bool = True, timeout: float = GIT_TIMEOUT_S) -> str:
    # Its own process group: on a timeout the hooks git started are ended with it (killing git
    # alone leaves a hook holding the output pipe, and the wait never returns).
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    proc = subprocess.Popen(["git", *args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", **group)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        # Without this, one stuck git hook (seen live: a session-tracking prepare-commit-msg
        # hook) freezes Medic's whole loop mid-incident instead of failing this one step.
        raise GitError(f"git {' '.join(args)} timed out after {timeout:.0f}s "
                       "(a git hook in this repo may be stuck)") from None
    proc = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
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
