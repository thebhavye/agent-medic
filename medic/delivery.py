"""Deliver a verified fix as a pull request (medic.toml: [deploy] mode = "pr").

Medic doesn't own the user's deploys. The fix it proved goes to their repo as a PR with the
evidence; they review, merge and deploy as usual. Medic then watches production for releases
that contain the fix (their deploy tags traces with release:<GIT_SHA>), and confirms the heal
or, if production is still unhealthy, opens a revert PR.

Uses git and the GitHub CLI (gh) with the user's own credentials.
"""

import json
import subprocess
from pathlib import Path


class DeliveryError(RuntimeError):
    pass


def _run(cmd: list[str], root: Path, check: bool = True) -> str:
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=300)
    except FileNotFoundError:
        raise DeliveryError(f"{cmd[0]} is not installed (PR delivery needs git and the GitHub CLI, gh)") from None
    except subprocess.TimeoutExpired:
        raise DeliveryError(f"{' '.join(cmd[:3])}: timed out after 300s") from None
    if check and proc.returncode != 0:
        raise DeliveryError(f"{' '.join(cmd[:3])}: {(proc.stderr or proc.stdout).strip()[-400:]}")
    return proc.stdout.strip()


def fetch(root: Path) -> None:
    _run(["git", "fetch", "--quiet", "origin"], root, check=False)


def contains(root: Path, commit: str, release: str) -> bool:
    """Is `commit` part of `release`? (A production release carries the fix once it's deployed.)"""
    if not commit or not release:
        return False
    proc = subprocess.run(["git", "merge-base", "--is-ancestor", commit, release], cwd=root, capture_output=True)
    return proc.returncode == 0


def body(inc) -> str:
    """The PR description: what broke, what proved it, the fix and its verification."""
    d, reg = inc.decision or {}, (inc.findings or {}).get("regression") or {}
    accepted = next((a for a in reversed((inc.remedy or {}).get("attempts", [])) if a.get("accepted")), {})
    metrics = accepted.get("metrics") or {}
    fa = inc.first_aid or {}
    lines = [f"**Agent Medic** healed a production incident (`{inc.id}`); this is the cure.", "",
             "### What broke", inc.triage.get("summary", "") if inc.triage else "", ""]
    if d.get("culprit"):
        lines += [f"First bad commit: `{d['culprit'][:10]}` \"{reg.get('culprit_subject', '')}\". The release "
                  "before it passes the known-answer eval against today's dependencies; this one fails it.", ""]
    lines += ["### Diagnosis", f"{d.get('cause', '').replace('_', ' ')} ({d.get('confidence', 0):.0%} confidence)"]
    lines += [f"- {r}" for r in d.get("rationale", [])[:6]] + [""]
    lines += ["### The fix", accepted.get("diagnosis", ""), ""]
    if accepted.get("original_intent"):
        lines += [f"**Why the original change was made:** {accepted['original_intent']}",
                  f"**How this keeps that intent:** {accepted.get('how_intent_is_kept', '')}", ""]
    lines += [f"Verified on {metrics.get('n', '?')} known-answer runs: pass {metrics.get('pass_rate', 0):.0%}, "
              f"errors {metrics.get('error_rate', 0):.0%}."]
    if fa.get("status") in ("active", "auto"):
        lines += ["", f"Until this ships, first aid keeps users served: {fa.get('what', '')}."]
    lines += ["", "Merge and deploy as usual (with `GIT_SHA` set). Medic watches the new release in production "
                  "and confirms the heal, or opens a revert PR if it doesn't hold."]
    return "\n".join(lines)


def open_pr(inc, branch: str, base: str, root: Path, title: str) -> dict:
    _run(["git", "push", "--quiet", "--force-with-lease", "origin", f"{branch}:{branch}"], root)
    url = existing_pr(branch, root)     # Medic stopped after opening it last time: track that one
    if not url:
        url = _run(["gh", "pr", "create", "--base", base, "--head", branch, "--title", title,
                    "--body", body(inc)], root).splitlines()[-1].strip()
    return {"url": url, "branch": branch, "base": base}


def prepare_pr(inc, branch: str, base: str, title: str, out_dir: Path) -> dict:
    """The PR, ready but not opened (medic.toml: [deploy] pr = "ready"). Nothing is pushed: the fix
    branch stays in the local repo and the description is written to a file. The user opens it
    with `agent-medic approve <id>`, or with the two commands returned here."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{inc.id}.md"
    path.write_text(body(inc) + "\n", encoding="utf-8")
    return {"branch": branch, "base": base, "title": title, "body_file": str(path),
            "commands": [f"git push origin {branch}",
                         f'gh pr create --base {base} --head {branch} --title "{title.replace(chr(34), chr(39))}" '
                         f'--body-file "{path}"']}


def existing_pr(branch: str, root: Path) -> str:
    raw = _run(["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "url,state", "--limit", "5"], root)
    prs = json.loads(raw or "[]")
    live = [pr for pr in prs if pr.get("state") in ("OPEN", "MERGED")]
    return live[0]["url"] if live else ""


def state(url: str, root: Path) -> dict:
    """{"state": OPEN|MERGED|CLOSED, "merge_commit": sha or None}."""
    raw = _run(["gh", "pr", "view", url, "--json", "state,mergeCommit,mergedAt"], root)
    data = json.loads(raw or "{}")
    return {"state": data.get("state", ""), "merge_commit": (data.get("mergeCommit") or {}).get("oid"),
            "merged_at": data.get("mergedAt")}


def revert_pr(inc, merge_commit: str, base: str, root: Path, why: str, worktrees: Path) -> dict:
    """Open a PR that reverts the merged fix, in a worktree so the user's checkout is untouched."""
    branch = f"medic/revert-{inc.id}"
    work = worktrees / f"revert-{inc.id}"
    fetch(root)
    _run(["git", "worktree", "add", "--force", "-B", branch, str(work), f"origin/{base}"], root)
    try:
        parents = _run(["git", "rev-list", "--parents", "-n", "1", merge_commit], root).split()
        revert = ["git", "revert", "--no-edit"] + (["-m", "1"] if len(parents) > 2 else []) + [merge_commit]
        _run(["git", "-c", "user.name=Agent Medic", "-c", "user.email=medic@localhost", *revert[1:]], work)
        _run(["git", "push", "--quiet", "--force-with-lease", "origin", f"{branch}:{branch}"], work)
        url = _run(["gh", "pr", "create", "--base", base, "--head", branch,
                    "--title", f"Revert Medic's fix for incident {inc.id}",
                    "--body", f"**Agent Medic**: production is still unhealthy with the fix: {why}\n\n"
                              f"This reverts {merge_commit[:10]}; a human should look at it."], work)
    finally:
        _run(["git", "worktree", "remove", "--force", str(work)], root, check=False)
    return {"url": url.splitlines()[-1].strip(), "branch": branch}
