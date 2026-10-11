"""A stuck git hook fails one step with a clear error instead of freezing Medic."""

import subprocess
import sys
import time

import pytest

from bisect_agent import gitops


def test_a_hanging_hook_is_a_git_error_not_a_freeze(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text(f'#!/bin/sh\n"{sys.executable.replace(chr(92), "/")}" -c "import time; time.sleep(30)"\n')
    hook.chmod(0o755)
    (tmp_path / "a.txt").write_text("a")
    gitops.git("add", "a.txt", cwd=tmp_path)
    started = time.monotonic()
    with pytest.raises(gitops.GitError, match="timed out after 2s"):
        gitops.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x", cwd=tmp_path, timeout=2)
    assert time.monotonic() - started < 15                 # the hook was ended too, not waited out (30 s)
