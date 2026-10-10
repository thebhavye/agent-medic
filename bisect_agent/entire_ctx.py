"""Ask Entire why the culprit commit was made.

When an AI agent wrote the culprit, Entire links the commit to the agent session through an
`Entire-Checkpoint:` trailer, and `entire checkpoint explain` / `entire why` return the
prompt and reasoning behind it. The fixer uses that to repair the bug while keeping
what the change was trying to achieve. Without Entire, it falls back to the commit message.
"""

import re
import shutil
import subprocess
from pathlib import Path

from bisect_agent import gitops

MAX_WHY_LINES = 3


def _entire(*args: str, repo: Path, timeout: int = 120) -> str | None:
    exe = shutil.which("entire")
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, *args], cwd=repo, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else None


def checkpoint_id(sha: str, repo: Path) -> str | None:
    match = re.search(r"^Entire-Checkpoint:\s*(\S+)", gitops.message(sha, repo), re.MULTILINE)
    return match.group(1) if match else None


def gather_intent(sha: str, repo: Path, head_root: Path) -> dict:
    """head_root is a checkout of the current tip, where `entire why` looks up line numbers."""
    checkpoint = checkpoint_id(sha, repo)
    explain = _entire("checkpoint", "explain", checkpoint or sha, repo=repo)

    why = []
    for path in gitops.changed_files(sha, repo):
        head_file = head_root / path
        if not head_file.exists():
            continue
        head_lines = head_file.read_text(encoding="utf-8", errors="replace").splitlines()
        for added in gitops.added_lines(sha, path, repo):
            if len(why) >= MAX_WHY_LINES or len(added.strip()) < 8:
                continue
            if added in head_lines:
                lineno = head_lines.index(added) + 1
                answer = _entire("why", f"{path}:{lineno}", repo=head_root)
                if answer:
                    why.append({"file": path, "line": lineno, "text": added.strip(), "why": answer})

    return {
        "source": "entire" if (explain or why) else "git",
        "checkpoint": checkpoint,
        "explain": explain or "",
        "why": why,
        "commit_message": gitops.message(sha, repo),
    }


def render(intent: dict) -> str:
    parts = [f"Commit message:\n{intent['commit_message']}"]
    if intent["checkpoint"]:
        parts.append(f"Entire checkpoint: {intent['checkpoint']}")
    if intent["explain"]:
        parts.append(f"`entire checkpoint explain` (the agent session that wrote this commit):\n{intent['explain']}")
    for w in intent["why"]:
        parts.append(f"`entire why {w['file']}:{w['line']}` for `{w['text']}`:\n{w['why']}")
    if intent["source"] == "git":
        parts.append("(No Entire session data for this commit; the commit message is the only record of intent.)")
    return "\n\n".join(parts)
