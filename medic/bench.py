"""Scenario benchmark: break production in known ways and score Medic's response.

Each run gets its own world (fresh deployment repo, fresh Medic state) and is driven step by
step: healthy traffic until Medic freezes a baseline, then one fault, then alternating
traffic batches and Medic ticks until the incident is resolved or escalated.

    python -m medic.bench run --scenario bad_release --runs 1
    python -m medic.bench report                       # writes docs/benchmark.md

Scored per run: correct cause, correct remedy, correct culprit release (where one is to
blame), expected outcome, time to detect, time to resolve, and false alarms.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / ".medic" / "bench"
REPORT = ROOT / "docs" / "benchmark.md"

# fault: ("chaos", mode) | ("release", [names]) shipped together as one deploy | None
SCENARIOS = {
    "api_change": {"fault": ("chaos", "schema_v2"), "cause": "dependency_contract", "remedy": "adapt_client",
                   "outcome": "resolved",
                   "story": "The knowledge-base provider ships API v2 under live traffic."},
    "bad_release": {"fault": ("release", ["timeout", "short-params", "docstring"]), "cause": "code_regression",
                    "remedy": "code_fix", "culprit": "short-params", "outcome": "resolved",
                    "story": "A deploy bundles three commits; one renames a tool parameter in the schema only."},
    "retry_loop": {"fault": ("release", ["careers", "double-check", "rename-title"]), "cause": "code_regression",
                   "remedy": "code_fix", "culprit": "double-check", "outcome": "resolved",
                   "story": "A deploy bundles three commits; one tells the agent to re-verify every fact."},
    "truncated_docs": {"fault": ("release", ["docs", "cap-docs", "rename-title"]), "cause": "code_regression",
                       "remedy": "code_fix", "culprit": "cap-docs", "outcome": "resolved",
                       "story": "A deploy bundles three commits; one caps read_doc output at 300 characters to save "
                                "tokens. Nothing errors: answers just lose their facts."},
    "outage": {"fault": ("chaos", "outage"), "cause": "dependency_outage", "remedy": "escalate",
               "outcome": "escalated",
               "story": "The knowledge-base API goes down. There is no code fix; Medic should page a human."},
    "stale_data": {"fault": ("chaos", "stale_data"), "cause": "dependency_data", "remedy": "escalate",
                   "outcome": "escalated",
                   "story": "The provider starts serving an outdated data snapshot through the same API. Nothing errors; "
                            "only the canary eval sees the wrong answers. No code fix can correct data, so Medic escalates."},
    "healthy": {"fault": None, "outcome": "no_incident",
                "story": "Nothing breaks. Medic should stay quiet (false-alarm check)."},
}

BASELINE_TIMEOUT_S = 900
INCIDENT_TIMEOUT_S = 2400
HEALTHY_BATCHES = 8


def drive(scenario: str, workdir: Path) -> dict:
    """Run one scenario. Runs in its own process with MEDIC_DIR=workdir."""
    import neatlogs
    neatlogs.init(workflow_name="agent-medic", tags=["agent-medic", "bench", f"scenario:{scenario}"])
    from bisect_agent.neatlogs_api import NeatlogsReader
    from medic import incidents, traffic, world
    from medic.agent import Medic

    spec = SCENARIOS[scenario]
    log_lines: list[str] = []

    def log(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        log_lines.append(line)
        print(line, flush=True)

    world.chaos("normal")
    initial = world.init(force=True)
    medic = Medic(NeatlogsReader(), log=log, alerts_since=time.time())   # a fresh world

    def tick() -> None:
        try:
            medic.tick()
        except Exception as exc:   # count it, keep going: Medic retries the step next tick
            result["tick_errors"] = result.get("tick_errors", 0) + 1
            log(f"[bench] tick failed: {type(exc).__name__}: {exc}")
    result = {"scenario": scenario, "provider": os.environ.get("LLM_PROVIDER", ""),
              "fixer_provider": os.environ.get("FIXER_PROVIDER", ""), "started": time.time(),
              "initial_release": initial}

    started = time.time()
    while medic.watcher.state.get("baseline") is None:
        if time.time() - started > BASELINE_TIMEOUT_S:
            return {**result, "error": "no baseline within the time limit"}
        traffic.run_batch(3, 3)
        tick()
    while medic.watcher.state.get("canary_baseline") is None:     # the canary needs a healthy pass rate too
        if time.time() - started > BASELINE_TIMEOUT_S:
            return {**result, "error": "no canary baseline within the time limit"}
        traffic.run_batch(3, 3)
        tick()
    result["baseline"] = medic.watcher.state["baseline"]

    fault = spec["fault"]
    culprit_sha = None
    if fault is None:
        for _ in range(HEALTHY_BATCHES):
            traffic.run_batch(3, 3)
            tick()
        result.update(outcome="no_incident" if not incidents.load_all() else "false_alarm",
                      incidents=len(incidents.load_all()))
        return _score(result, spec, None)

    fault_at = time.time()
    if fault[0] == "chaos":
        world.chaos(fault[1])
    else:
        for name in fault[1]:
            sha = world.release(name)
            if name == spec.get("culprit"):
                culprit_sha = sha
    result.update(fault=list(fault), fault_at=fault_at, culprit_expected=culprit_sha)
    log(f"[bench] fault injected: {fault}")

    inc = None
    while time.time() - fault_at < INCIDENT_TIMEOUT_S:
        traffic.run_batch(3, 3)
        tick()
        found = incidents.load_all()
        inc = found[0] if found else None
        for _ in range(10):       # advance phases that don't need new traffic
            if not inc or not inc.is_open or inc.phase in ("deployed", "canary"):
                break
            tick()
            inc = incidents.load_all()[0]
        if inc and not inc.is_open:
            break
    world.chaos("normal")
    if inc is None:
        return _score({**result, "outcome": "missed"}, spec, culprit_sha)

    detected = next(t["t"] for t in inc.timeline if t["phase"] == "open")
    closed = inc.timeline[-1]["t"] if not inc.is_open else None
    fixer_cost = sum(a.get("fixer_cost_usd", 0) for a in inc.remedy.get("attempts", []))
    fixer_tokens = sum((a.get("usage") or {}).get("input_tokens", 0) + (a.get("usage") or {}).get("output_tokens", 0)
                       for a in inc.remedy.get("attempts", []))
    result.update(
        incident=inc.id, outcome=inc.phase if not inc.is_open else f"timeout_in_{inc.phase}",
        cause=inc.decision.get("cause"), remedy=inc.decision.get("remedy"),
        culprit_found=inc.findings.get("regression", {}).get("culprit"),
        attempts=len(inc.remedy.get("attempts", [])), detect_s=round(detected - fault_at),
        resolve_s=round(closed - fault_at) if closed else None,
        fixer_tokens=fixer_tokens, fixer_cost_usd=fixer_cost,
        first_aid=inc.first_aid.get("status"),
        first_aid_s=round(inc.first_aid["applied_at"] - fault_at) if inc.first_aid.get("applied_at") else None,
        disagreements=inc.decision.get("disagreements", []), timeline=inc.timeline)
    return _score(result, spec, culprit_sha)


def _score(result: dict, spec: dict, culprit_sha: str | None) -> dict:
    checks = {"outcome": result.get("outcome") == spec["outcome"]}
    if "cause" in spec:
        checks["cause"] = result.get("cause") == spec["cause"]
        checks["remedy"] = result.get("remedy") == spec["remedy"]
    if culprit_sha:
        checks["culprit"] = result.get("culprit_found") == culprit_sha
    return {**result, "checks": checks, "passed": all(checks.values())}


def run(scenario: str, runs: int) -> None:
    for i in range(runs):
        workdir = BENCH / f"{scenario}-{time.strftime('%m%d-%H%M%S')}"
        workdir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "MEDIC_DIR": str(workdir), "PYTHONIOENCODING": "utf-8",
               "MEDIC_NEATLOGS_STATE": os.environ.get("MEDIC_NEATLOGS_STATE", str(ROOT / ".medic" / "neatlogs.json")),
               "MEDIC_CANARY_INTERVAL_S": os.environ.get("MEDIC_CANARY_INTERVAL_S", "60")}
        print(f"[bench] {scenario} run {i + 1}/{runs} in {workdir.name}", flush=True)
        proc = subprocess.run([sys.executable, "-m", "medic.bench", "_drive", "--scenario", scenario,
                               "--out", str(workdir / "result.json")], env=env, cwd=ROOT)
        if proc.returncode != 0:
            (workdir / "result.json").write_text(json.dumps(
                {"scenario": scenario, "error": f"driver exited {proc.returncode}", "passed": False}))


def report() -> str:
    rows = []
    for path in sorted(BENCH.glob("*/result.json")):
        r = json.loads(path.read_text(encoding="utf-8"))
        rows.append(r)
    lines = ["# Agent Medic scenario benchmark", "",
             "Each run: fresh deployment, healthy traffic until Medic freezes a baseline, one injected fault, "
             "then Medic runs unattended. Generated by `python -m medic.bench report`.", "",
             "| Scenario | Traffic model | Fixer model | Outcome | Cause | Remedy | Culprit | Detect | Resolve | Fix attempts | Pass |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        c = r.get("checks", {})
        mark = lambda k: "" if k not in c else (" ✅" if c[k] else " ❌")
        lines.append(
            f"| {r['scenario']} | {r.get('provider', '')} | {r.get('fixer_provider', '')} | {r.get('outcome', r.get('error', ''))}{mark('outcome')} "
            f"| {r.get('cause') or '-'}{mark('cause')} | {r.get('remedy') or '-'}{mark('remedy')} "
            f"| {(r.get('culprit_found') or '-')[:8]}{mark('culprit')} "
            f"| {_dur(r.get('detect_s'))} | {_dur(r.get('resolve_s'))} | {r.get('attempts', '-')} "
            f"| {'✅' if r.get('passed') else '❌'} |")
    passed = sum(r.get("passed", False) for r in rows)
    lines += ["", f"**{passed}/{len(rows)} runs passed.**", ""]
    for name, spec in SCENARIOS.items():
        lines.append(f"- **{name}**: {spec['story']}")
    text = "\n".join(lines) + "\n"
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(text, encoding="utf-8")
    return text


def _dur(seconds) -> str:
    if seconds is None:
        return "-"
    return f"{seconds // 60}m {seconds % 60:02d}s"


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="medic.bench")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--scenario", choices=[*SCENARIOS, "all"], required=True)
    r.add_argument("--runs", type=int, default=1)
    d = sub.add_parser("_drive")
    d.add_argument("--scenario", required=True)
    d.add_argument("--out", required=True)
    sub.add_parser("report")
    args = parser.parse_args()
    if args.cmd == "run":
        for name in (SCENARIOS if args.scenario == "all" else [args.scenario]):
            run(name, args.runs)
        print(report())
    elif args.cmd == "_drive":
        result = drive(args.scenario, Path(os.environ["MEDIC_DIR"]))
        Path(args.out).write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
        print(json.dumps({k: v for k, v in result.items() if k not in ("timeline", "baseline")}, default=str))
    else:
        print(report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
