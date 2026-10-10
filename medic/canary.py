"""Canary eval: catch answers that are wrong but look fine.

Production traces have no ground truth. A confident wrong answer has no tool error, no
non-answer and normal cost, so the Watcher can't see it. Every few minutes Medic runs the
known-answer eval set against the deployed release; a drop in pass rate opens an incident.
Canary runs go to neatlogs in their own sessions (tag canary), apart from production.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from bisect_agent.metrics import from_cases
from medic import paths, world

INTERVAL_S = float(os.environ.get("MEDIC_CANARY_INTERVAL_S", 600))
DROP = 0.25          # pass-rate drop vs the canary baseline that counts as a regression


def due(state: dict) -> bool:
    runs = state.get("canary", [])
    return not runs or time.time() - runs[-1]["at"] >= INTERVAL_S


def run(state: dict, log=print, code_dir: Path | None = None, sha: str | None = None) -> dict:
    """Run the known-answer eval on the deployed release, or on `code_dir` (e.g. a rollout candidate)."""
    code_dir = code_dir or paths.DEPLOY
    sha = sha or world.head()
    out = paths.MEDIC_DIR / "canary" / f"{time.strftime('%Y%m%d-%H%M%S')}-{sha[:8]}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "target_agent.run_eval", "--cases", str(paths.CASES), "--out", str(out),
           "--session", f"canary-{sha[:8]}-{out.stem}", "--tag", "canary", "--tag", f"release:{sha[:12]}"]
    env = {**os.environ, "PYTHONPATH": str(code_dir), "KB_URL": paths.KB_URL}
    subprocess.run(cmd, cwd=code_dir, env=env, capture_output=True, timeout=1800)
    if not out.exists():
        record = {"at": time.time(), "release": sha[:12], "error": "canary eval did not produce results"}
    else:
        cases = json.loads(out.read_text(encoding="utf-8"))["cases"]
        m = from_cases(cases)
        record = {"at": time.time(), "release": sha[:12], "pass_rate": m.pass_rate, "n": m.n,
                  "failed": [{"id": c["id"], "question": c["question"], "answer": c["answer"][:300],
                              "expect": c["expect"]} for c in cases if not c["passed"]]}
    state.setdefault("canary", []).append(record)
    state["canary"] = state["canary"][-50:]
    if "pass_rate" in record:
        if state.get("canary_baseline") is None and record["pass_rate"] >= 0.75:
            state["canary_baseline"] = record["pass_rate"]
        log(f"[medic] canary on {sha[:8]}: pass {record['pass_rate']:.0%} ({record['n']} known-answer questions)")
    return record


def answers_hold(record: dict, state: dict) -> tuple[bool, str]:
    """Does a canary run keep answers at the healthy level? (No baseline: an absolute bar.)"""
    if "pass_rate" not in record:
        return False, record.get("error", "the canary eval produced no results")
    base = state.get("canary_baseline")
    bar = base - DROP if base is not None else 0.75
    ok = record["pass_rate"] >= bar
    return ok, (f"known-answer eval on the candidate: pass {record['pass_rate']:.0%} "
                f"({'meets' if ok else 'below'} the {bar:.0%} bar)")


def regressed(state: dict) -> dict | None:
    """A quality signal if the latest canary fell well below the canary baseline."""
    base, runs = state.get("canary_baseline"), state.get("canary", [])
    if base is None or not runs or "pass_rate" not in runs[-1]:
        return None
    current = runs[-1]["pass_rate"]
    if base - current >= DROP:
        return {"name": "quality", "metric": "canary_pass_rate", "baseline": base, "current": current}
    return None
