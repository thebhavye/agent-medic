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


def evaluate(code_dir: Path, sha: str, first_aid: Path | None = None, tag: str = "canary") -> dict:
    """Run the known-answer eval on `code_dir` at release `sha` and return the result.

    The eval judges the code as it is: any first-aid bundle in Medic's own environment is
    dropped. Pass `first_aid` to judge the release *with* that bundle applied instead."""
    out = paths.MEDIC_DIR / "canary" / f"{time.strftime('%Y%m%d-%H%M%S')}-{sha[:8]}-{tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [paths.PYTHON, *paths.RUNNER, "--cases", str(paths.CASES), "--out", str(out),
           "--session", f"{tag}-{sha[:8]}-{out.stem}", "--tag", tag, "--tag", f"release:{sha[:12]}"]
    env = {k: v for k, v in os.environ.items()
           if k not in ("MEDIC_CONTROL", "MEDIC_RELEASE", "MEDIC_STANDBY", "MEDIC_RUNTIME_STATE")}
    env.update(PYTHONPATH=str(code_dir), KB_URL=paths.KB_URL)
    if first_aid is not None:
        env.update(MEDIC_CONTROL=str(first_aid), MEDIC_RELEASE=sha[:12])
    subprocess.run(cmd, cwd=code_dir, env=env, capture_output=True, timeout=1800)
    if not out.exists():
        return {"at": time.time(), "release": sha[:12], "error": f"{tag} eval did not produce results"}
    cases = json.loads(out.read_text(encoding="utf-8"))["cases"]
    m = from_cases(cases)
    return {"at": time.time(), "release": sha[:12], "pass_rate": m.pass_rate, "error_rate": m.error_rate, "n": m.n,
            "tool_calls_per_run": m.tool_calls_per_run, "tokens_per_run": m.tokens_per_run,
            "failed": [{"id": c["id"], "question": c["question"], "answer": c["answer"][:300],
                        "expect": c["expect"]} for c in cases if not c["passed"]]}


def run(state: dict, log=print, code_dir: Path | None = None, sha: str | None = None) -> dict:
    """Run the known-answer eval on the deployed release, or on `code_dir` (e.g. a rollout candidate)."""
    code_dir = code_dir or paths.DEPLOY
    sha = sha or world.head()
    record = evaluate(code_dir, sha)
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


def _low(state: dict, record: dict) -> bool:
    base = state.get("canary_baseline")
    return base is not None and "pass_rate" in record and base - record["pass_rate"] >= DROP


def needs_confirm(state: dict) -> bool:
    """The latest canary fell below the bar, but no run on that release has confirmed it yet.

    A model gives a different answer now and then, so with a handful of questions one unlucky
    run can drop a whole question's worth. Medic runs the questions again before calling it."""
    runs = state.get("canary", [])
    return bool(runs) and _low(state, runs[-1]) and regressed(state) is None


def regressed(state: dict) -> dict | None:
    """A quality signal if the last two canary runs on the same release both fell well below the
    canary baseline: one low run alone is noise until a second one agrees."""
    base, runs = state.get("canary_baseline"), state.get("canary", [])
    if len(runs) < 2 or not all(_low(state, r) for r in runs[-2:]) or runs[-1].get("release") != runs[-2].get("release"):
        return None
    current = max(r["pass_rate"] for r in runs[-2:])
    return {"name": "quality", "metric": "canary_pass_rate", "baseline": base, "current": current}
