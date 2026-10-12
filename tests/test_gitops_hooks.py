"""Medic's own commits skip the repo's hooks: a stuck or failing hook can't block a fix."""

import subprocess

from bisect_agent import gitops


def test_commit_skips_a_failing_hook(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    (tmp_path / "f.txt").write_text("x")
    gitops.git("add", "-A", cwd=tmp_path)
    gitops.commit("Medic fix", cwd=tmp_path)
    assert gitops.subject("HEAD", tmp_path) == "Medic fix"
