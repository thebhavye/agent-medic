"""Canary rollout: a verified fix serves part of production first, then is promoted or rolled back.

Verification runs the fix against the eval set and replayed questions; production is bigger
than that. So instead of merging straight into the live release, Medic starts a canary: the
traffic router sends a share of production requests to the candidate release, the rest stays
on the stable one. Once enough canary traces are in, they're compared with the healthy
baseline. Healthy: promote (merge into the live release). Not healthy: roll back
automatically and send the incident back to the fix step with what production showed.

State lives in MEDIC_DIR/rollout.json, so the traffic router (another process) can see it.
"""

import json
import os
import random
import time
from pathlib import Path

from bisect_agent import gitops
from medic import paths

SHARE = float(os.environ.get("MEDIC_ROLLOUT_SHARE", 0.5))
MIN_TRACES = int(os.environ.get("MEDIC_ROLLOUT_TRACES", 4))
TIMEOUT_S = 1800


def _file() -> Path:
    return paths.MEDIC_DIR / "rollout.json"


def current() -> dict | None:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def root() -> Path:
    return paths.MEDIC_DIR / "worktrees" / "canary"


def start(incident_id: str, branch: str) -> dict:
    """Check the candidate out where the router can run it, and record the rollout."""
    candidate = gitops.rev_parse(branch, paths.DEPLOY)
    gitops.worktree_add(root(), candidate, paths.DEPLOY)
    state = {"incident": incident_id, "branch": branch, "candidate": candidate,
             "stable": gitops.rev_parse("HEAD", paths.DEPLOY), "share": SHARE, "started": time.time()}
    _file().write_text(json.dumps(state, indent=1), encoding="utf-8")
    return state


def route() -> tuple[Path, str]:
    """For the traffic router: (code directory, release sha) for the next batch."""
    state = current()
    if state and root().exists() and random.random() < state["share"]:
        return root(), state["candidate"]
    return paths.DEPLOY, gitops.rev_parse("HEAD", paths.DEPLOY)


def finish() -> None:
    _file().unlink(missing_ok=True)
    gitops.worktree_remove(root(), paths.DEPLOY)


def judge(watcher, compare) -> dict | None:
    """None while waiting for canary traffic; otherwise the verdict with its evidence."""
    state = current()
    if not state:
        return None
    from medic import evidence
    canary = watcher.recent(release=state["candidate"][:12])
    if len(canary) < MIN_TRACES:
        if time.time() - state["started"] > TIMEOUT_S:
            return {"verdict": "inconclusive", "detail": f"only {len(canary)} canary traces in "
                                                         f"{TIMEOUT_S // 60} minutes", "state": state}
        return None
    window = evidence.window(canary)
    signals = compare(watcher.state["baseline"], window)
    return {"verdict": "regressed" if signals else "healthy", "window": window, "state": state,
            "signals": [s.__dict__ for s in signals],
            "detail": ("; ".join(s.describe() for s in signals) if signals else
                       f"{window['n']} canary traces at failure rate {window['failure_rate']:.0%}")}
