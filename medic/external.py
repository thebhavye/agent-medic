"""External mode: Medic on an agent we didn't write.

The target is someone else's agent, run unmodified from a clone of its repository and
traced to neatlogs by its own instrumentation. Medic has no healthy baseline for it, so it
audits: when most recent runs fail, it opens an incident. The pipeline is the same shape as
for our own agent (evidence from spans, triage, parallel investigators, a decision, a fix
verified by re-running the real agent), but the fix is *proposed* on a branch of the fork,
through an Entire Trail, instead of deployed. Upstreaming it is the maintainers' call.

    python -m medic.external run --target neatlogs-gemini
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from medic import paths

TARGETS = {
    "neatlogs-gemini": {
        "title": "neatlogs SDK example: google_genai_multiagent",
        "repo": paths.MEDIC_DIR / "external" / "neatlogs",
        "forge": "gh/thebhavye/neatlogs",
        "upstream": "NeatLogs/neatlogs",
        "subdir": "examples/sdk_examples/google_genai_multiagent",
        "python": ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python",
        "args": ["main.py"],
        "workflow": "blog-creation",
        "files": ["agents.py", "main.py", "requirements.txt"],
        "topics": ["The future of renewable energy", "How batteries store grid power",
                   "Why open-source tools win developers"],
        "env": ["NEATLOGS_API_KEY", "GOOGLE_API_KEY"],
    },
}

AUDIT_RUNS = 2            # runs of the target before judging it
FAILING = 0.5             # share of failing runs that opens an incident
RUN_TIMEOUT_S = 600


@dataclass
class Case:
    target: str
    id: str = field(default_factory=lambda: time.strftime("ext-%m%d-%H%M%S"))
    phase: str = "audit"   # audit -> investigate -> decide -> fix -> propose -> done | healthy | escalated
    runs: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    triage: dict = field(default_factory=dict)
    findings: dict = field(default_factory=dict)
    decision: dict = field(default_factory=dict)
    attempts: list = field(default_factory=list)
    proposal: dict = field(default_factory=dict)
    timeline: list = field(default_factory=list)

    def note(self, phase: str, msg: str) -> None:
        self.timeline.append({"t": round(time.time(), 1), "phase": phase, "msg": msg})
        print(f"{time.strftime('%H:%M:%S')} [medic:external] {phase}: {msg}", flush=True)


def _state_path(target: str) -> Path:
    return paths.MEDIC_DIR / "external" / f"case-{target}.json"


def save(case: Case) -> None:
    p = _state_path(case.target)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.with_suffix(".tmp").write_text(json.dumps(asdict(case), indent=1, default=str), encoding="utf-8")
    p.with_suffix(".tmp").replace(p)


def load(target: str) -> Case | None:
    p = _state_path(target)
    return Case(**json.loads(p.read_text(encoding="utf-8"))) if p.exists() else None


# -- running the target --------------------------------------------------------------------

def workdir(spec: dict) -> Path:
    return spec["repo"] / spec["subdir"]


def run_target(spec: dict, topic: str) -> dict:
    wd = workdir(spec)
    env = {k: v for k, v in os.environ.items() if k not in ("NEATLOGS_TOKEN",)}
    env["PYTHONIOENCODING"] = "utf-8"
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    proc = subprocess.run([str(wd / spec["python"]), "-W", "always", *spec["args"], topic], cwd=wd, env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT_S)
    stderr = [l for l in proc.stderr.splitlines() if "neatlogs - INFO" not in l]
    return {"topic": topic, "exit": proc.returncode, "started": started.isoformat(),
            "seconds": round(time.monotonic() - t0, 1), "stdout_tail": proc.stdout[-800:],
            "warnings": sorted({l.strip() for l in stderr if "Warning" in l})[:5],
            "stderr_tail": "\n".join(stderr[-6:])}


def trace_for(reader, spec: dict, run: dict, wait_s: float = 120) -> dict | None:
    """The neatlogs trace this run produced: the workflow's first trace started after it."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        traces = [t for t in reader.list_traces(workflowName=spec["workflow"], **{"from": run["started"]})
                  if t.get("finalizationStatus") == "finalized"]
        if traces:
            return sorted(traces, key=lambda t: t["startedAt"])[0]
        time.sleep(5)
    return None


def span_errors(reader, trace: dict) -> list[dict]:
    """Every failing span, deepest (LLM, TOOL) first, with its error message."""
    out = []
    for s in reader.spans(trace["traceId"]):
        if s["status"].upper() != "ERROR":
            continue
        err = reader.payload(trace["traceId"], s["spanId"], fields="error").get("error") or ""
        try:
            err = json.loads(err).get("message", err)
        except (ValueError, AttributeError):
            pass
        out.append({"span": s["spanName"], "type": s["spanType"], "model": s.get("model"), "message": err[:1500]})
    rank = {"LLM": 0, "TOOL": 1, "AGENT": 2, "CHAIN": 3, "WORKFLOW": 4}
    return sorted(out, key=lambda e: rank.get(e["type"], 9))


# -- investigators -------------------------------------------------------------------------

def investigate_provider(spec: dict, errors: list[dict]) -> dict:
    """Classify model-provider failures and test any replacement the provider names."""
    llm = [e for e in errors if e["type"] == "LLM"] or errors
    if not llm:
        return {"verdict": "no_provider_error", "confidence": 0.5, "detail": "no failing model calls"}
    msg = llm[0]["message"]
    model = llm[0].get("model") or (re.search(r"models/([\w.\-]+)", msg) or [None, None])[1]
    code = (re.search(r"\b(4\d\d|5\d\d)\b", msg) or [None, None])[1]
    replacement = (re.search(r"use (?:models/)?([\w.\-]+)", msg) or [None, None])[1]
    finding = {"failing_model": model, "http_status": code, "message": msg[:600], "replacement": replacement}
    if re.search(r"no longer available|not found|deprecated|retired|decommission", msg, re.I) and code in ("404", "410", None):
        probes = {m: _probe_gemini(m) for m in filter(None, [model, replacement])}
        finding["probes"] = probes
        ok = replacement and probes.get(replacement) == 200
        return {**finding, "verdict": "model_retired", "confidence": 0.95 if ok else 0.7,
                "detail": f"the provider no longer serves {model} (HTTP {code}); "
                          + (f"its suggested replacement {replacement} answers (HTTP 200)" if ok
                             else "no working replacement confirmed")}
    if code == "429" or re.search(r"rate|quota|exhausted", msg, re.I):
        return {**finding, "verdict": "rate_limited", "confidence": 0.8, "detail": f"provider rate limit or quota: {msg[:160]}"}
    if code in ("401", "403") or re.search(r"api key|permission|unauthori", msg, re.I):
        return {**finding, "verdict": "auth", "confidence": 0.8, "detail": f"credentials rejected: {msg[:160]}"}
    return {**finding, "verdict": "unknown_provider_error", "confidence": 0.4, "detail": msg[:200]}


def _probe_gemini(model: str) -> int:
    try:
        r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                       headers={"x-goog-api-key": os.environ.get("GOOGLE_API_KEY", "")},
                       json={"contents": [{"parts": [{"text": "Reply with OK."}]}]}, timeout=60)
        return r.status_code
    except httpx.HTTPError:
        return 0


def investigate_dependencies(spec: dict, runs: list[dict]) -> dict:
    """Deprecation and rename warnings the target emits at runtime."""
    warnings = sorted({w for r in runs for w in r.get("warnings", [])})
    renamed = [w for w in warnings if re.search(r"renamed|deprecated|no longer", w, re.I)]
    if renamed:
        return {"verdict": "deprecated_dependency", "confidence": 0.8, "warnings": renamed,
                "detail": "; ".join(w[-200:] for w in renamed[:2])}
    return {"verdict": "clean", "confidence": 0.6, "warnings": warnings, "detail": "no deprecation warnings"}


def investigate_history(spec: dict) -> dict:
    """Did the target's code change recently, or did the world change under it?"""
    repo, sub = spec["repo"], spec["subdir"]
    log = subprocess.run(["git", "log", "-3", "--format=%h|%ad|%s", "--date=short", "--", sub], cwd=repo,
                         capture_output=True, text=True).stdout.strip().splitlines()
    last = log[0].split("|") if log else ["", "", ""]
    days = (datetime.now(timezone.utc).date() - datetime.fromisoformat(last[1]).date()).days if last[1] else None
    return {"verdict": "no_recent_code_change" if days and days > 7 else "recent_code_change",
            "confidence": 0.8, "last_change": {"commit": last[0], "date": last[1], "subject": last[2]},
            "days_since_change": days,
            "detail": f"the example's code last changed {days} days ago ({last[0]} {last[2]!r}); it fails at that "
                      "same commit today, so something outside the code changed" if days and days > 7 else
                      f"the example changed {days} days ago: check that commit"}


# -- decision, fix, verification, proposal --------------------------------------------------

def decide(findings: dict) -> dict:
    p, d, h = findings.get("provider", {}), findings.get("dependencies", {}), findings.get("history", {})
    rationale = [f"{k}: {v.get('verdict')}: {v.get('detail')}" for k, v in findings.items()]
    if p.get("verdict") == "model_retired" and p.get("replacement"):
        remedy = {"kind": "update_model", "from": p["failing_model"], "to": p["replacement"],
                  "also": "rename_dependency" if d.get("verdict") == "deprecated_dependency" else None}
        return {"cause": "provider_model_retired", "confidence": p["confidence"], "remedy": remedy,
                "rationale": rationale}
    if p.get("verdict") in ("rate_limited", "auth"):
        return {"cause": f"provider_{p['verdict']}", "confidence": p["confidence"],
                "remedy": {"kind": "escalate"}, "rationale": rationale + ["not a code problem: needs an account change"]}
    return {"cause": "unexplained", "confidence": 0.3, "remedy": {"kind": "escalate"}, "rationale": rationale}


FIX_NOTE = """You are fixing an open-source example agent that broke because the outside world changed,
not because of a bad commit. Make the smallest change that makes the example run again today, keep
its structure and behavior, and do not touch unrelated code. If a dependency was renamed, update the
import and requirements together."""


def fix_prompt(spec: dict, case: Case, previous: list[dict]) -> str:
    wd = workdir(spec)
    files = "\n".join(f'<file path="{f}">\n{(wd / f).read_text(encoding="utf-8")}\n</file>' for f in spec["files"])
    sections = [
        f"<diagnosis>\n{FIX_NOTE}\n\ncause: {case.decision['cause']}\nremedy: {json.dumps(case.decision['remedy'])}\n"
        + "\n".join(case.decision["rationale"]) + "\n</diagnosis>",
        "<failing_spans_from_neatlogs>\n" + json.dumps(case.triage.get("errors", [])[:3], indent=1) + "\n</failing_spans_from_neatlogs>",
        "<runtime_warnings>\n" + json.dumps(case.findings.get("dependencies", {}).get("warnings", []), indent=1)
        + "\n</runtime_warnings>",
        f"<current_files>\n{files}\n</current_files>",
    ]
    for i, a in enumerate(previous, 1):
        sections.append(f"<previous_attempt n=\"{i}\">\nedits: {json.dumps(a.get('edits'))}\nresult: {a.get('feedback')}\n</previous_attempt>")
    return "\n\n".join(sections)


def git(spec: dict, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=spec["repo"], capture_output=True, text=True)
    if check and proc.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def attempt_fix(spec: dict, case: Case, reader) -> dict:
    from bisect_agent import fixer
    from bisect_agent.edits import EditError, apply_edits
    branch = f"medic/{case.id}"
    if git(spec, "rev-parse", "--abbrev-ref", "HEAD") != branch:
        git(spec, "checkout", "-q", "-B", branch, "origin/main")
    git(spec, "checkout", "-q", "--", ".")
    record = {"n": len(case.attempts) + 1, "accepted": False}
    try:
        proposal = fixer.propose_fix(fix_prompt(spec, case, case.attempts))
    except Exception as exc:
        record["feedback"] = f"no usable proposal: {type(exc).__name__}: {exc}"
        return record
    record.update({k: proposal.get(k) for k in ("diagnosis", "commit_title", "edits", "usage", "how_intent_is_kept")})
    edits = [{**e, "path": e["path"] if e["path"].startswith(spec["subdir"]) else f"{spec['subdir']}/{e['path']}"}
             for e in proposal["edits"]]
    try:
        apply_edits(spec["repo"], edits)
    except EditError as exc:
        record["feedback"] = f"edits did not apply: {exc}"
        return record
    if any(e["path"].endswith("requirements.txt") for e in edits):
        wd = workdir(spec)
        subprocess.run(["uv", "pip", "install", "-q", "--python", str(wd / spec["python"]), "-r", "requirements.txt"],
                       cwd=wd, capture_output=True)

    # Verify by running the real agent again, twice, and reading back its traces.
    results = []
    for topic in spec["topics"][:2]:
        run = run_target(spec, topic)
        trace = trace_for(reader, spec, run)
        results.append({**run, "trace_status": trace["status"] if trace else None,
                        "trace_id": trace["traceId"] if trace else None})
    record["verification"] = results
    ok = all(r["exit"] == 0 and (r["trace_status"] or "").upper() == "OK" for r in results)
    still_warn = [w for r in results for w in r["warnings"] if re.search(r"renamed|deprecated", w, re.I)]
    if not ok:
        git(spec, "checkout", "-q", "--", ".")
        record["feedback"] = "; ".join(f"run {i + 1}: exit {r['exit']}, trace {r['trace_status']}: {r['stderr_tail'][-200:]}"
                                       for i, r in enumerate(results))
        return record
    git(spec, "add", "-A", spec["subdir"])
    git(spec, "commit", "-q", "-m", f"{proposal.get('commit_title') or 'Fix example'}\n\n{proposal.get('diagnosis', '')}\n\n"
                                    f"Verified by running the example twice: both runs exit 0 and their neatlogs traces are OK.\n"
                                    f"Agent-Medic-Case: {case.id}\n")
    record.update(accepted=True, commit=git(spec, "rev-parse", "HEAD"), branch=branch,
                  remaining_warnings=still_warn,
                  feedback=f"verified: {len(results)}/{len(results)} real runs succeed with OK traces")
    return record


def propose(spec: dict, case: Case) -> dict:
    """Push the verified fix to the fork and open an Entire Trail for review."""
    a = next(a for a in case.attempts if a.get("accepted"))
    git(spec, "push", "-q", "-f", "origin", a["branch"])
    body = report(spec, case)
    out = {"branch": a["branch"], "commit": a["commit"]}
    proc = subprocess.run(["entire", "trail", "create", "--repo", spec["forge"], "--branch", a["branch"], "--base", "main",
                           "--title", f"Medic: {a.get('commit_title') or case.decision['cause']}", "--type", "bug",
                           "--priority", "high", "--body", body], cwd=spec["repo"], capture_output=True, text=True)
    out["trail"] = (proc.stdout or proc.stderr).strip()[-400:]
    out["trail_created"] = proc.returncode == 0
    return out


def report(spec: dict, case: Case) -> str:
    a = next((a for a in case.attempts if a.get("accepted")), {})
    lines = [f"## {spec['title']}: {case.decision.get('cause')}", "",
             f"Found by Agent Medic from neatlogs traces of workflow `{spec['workflow']}`, case `{case.id}`.", "",
             "### What failed", "", case.triage.get("summary", ""), "",
             "```", (case.triage.get("errors") or [{}])[0].get("message", "")[:500], "```", "", "### Diagnosis", ""]
    lines += [f"- {r}" for r in case.decision.get("rationale", [])]
    if a:
        lines += ["", "### Fix", "", a.get("diagnosis", ""), ""]
        for e in a.get("edits", []):
            lines += [f"`{e['path']}`", "```diff"] + [f"- {l}" for l in e["find"].splitlines()] + \
                     [f"+ {l}" for l in e["replace"].splitlines()] + ["```"]
        lines += ["", "### Verification", "", a.get("feedback", "")]
        lines += [f"- run `{r['topic']}`: exit {r['exit']}, {r['seconds']}s, trace `{r['trace_id']}` {r['trace_status']}"
                  for r in a.get("verification", [])]
    return "\n".join(lines) + "\n"


# -- the loop -------------------------------------------------------------------------------

def step(spec: dict, case: Case, reader) -> None:
    if case.phase == "audit":
        for topic in spec["topics"][:AUDIT_RUNS]:
            run = run_target(spec, topic)
            trace = trace_for(reader, spec, run)
            run["trace_id"], run["trace_status"] = (trace["traceId"], trace["status"]) if trace else (None, None)
            run["errors"] = span_errors(reader, trace) if trace and trace["status"].upper() == "ERROR" else []
            case.runs.append(run)
            case.note("audit", f"run {topic!r}: exit {run['exit']}, trace {run['trace_status']}"
                               + (f", {run['errors'][0]['type']} {run['errors'][0]['span']}: {run['errors'][0]['message'][:120]}"
                                  if run["errors"] else ""))
        failing = [r for r in case.runs if r["exit"] != 0 or (r["trace_status"] or "").upper() != "OK"]
        if len(failing) / len(case.runs) < FAILING:
            case.phase = "healthy"
            case.note("healthy", f"{len(case.runs) - len(failing)}/{len(case.runs)} runs succeed")
            return
        sig = Counter((e["type"], e["span"], e["message"][:80]) for r in failing for e in r["errors"][:1])
        top = sig.most_common(1)[0][0] if sig else ("?", "?", "?")
        errors = [r["errors"][0] for r in failing if r["errors"]]
        case.triage = {"summary": f"{len(failing)}/{len(case.runs)} runs fail; first failing span {top[0]} {top[1]}",
                       "errors": errors}
        case.phase = "investigate"
        case.note("triaged", case.triage["summary"] + "; sending provider, dependencies, history")
    elif case.phase == "investigate":
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs = {"provider": pool.submit(investigate_provider, spec, case.triage["errors"]),
                    "dependencies": pool.submit(investigate_dependencies, spec, case.runs),
                    "history": pool.submit(investigate_history, spec)}
            for name, fut in jobs.items():
                case.findings[name] = fut.result()
                case.note("investigated", f"{name}: {case.findings[name]['verdict']}: {case.findings[name]['detail']}")
        case.phase = "decide"
    elif case.phase == "decide":
        case.decision = decide(case.findings)
        case.note("decided", f"cause {case.decision['cause']} (confidence {case.decision['confidence']}), "
                             f"remedy {case.decision['remedy']}")
        case.phase = "escalated" if case.decision["remedy"]["kind"] == "escalate" else "fix"
    elif case.phase == "fix":
        rec = attempt_fix(spec, case, reader)
        case.attempts.append(rec)
        case.note("fix", f"attempt {rec['n']}: {'accepted' if rec['accepted'] else 'rejected'}: {rec.get('feedback')}")
        if rec["accepted"]:
            case.phase = "propose"
        elif rec["n"] >= 3:
            case.phase = "escalated"
    elif case.phase == "propose":
        case.proposal = propose(spec, case)
        out = paths.ROOT / "docs" / "real-world" / f"{case.target}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report(spec, case), encoding="utf-8")
        case.note("proposed", f"branch {case.proposal['branch']} pushed to the fork; "
                              + ("Entire Trail opened" if case.proposal["trail_created"] else
                                 f"Trail not created: {case.proposal['trail'][-160:]}") + f"; report {out.name}")
        case.phase = "done"


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(paths.ROOT / ".env")
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="medic.external")
    parser.add_argument("cmd", choices=["run", "show", "reset"])
    parser.add_argument("--target", default="neatlogs-gemini", choices=list(TARGETS))
    args = parser.parse_args()
    spec = TARGETS[args.target]
    if args.cmd == "reset":
        _state_path(args.target).unlink(missing_ok=True)
        return 0
    case = load(args.target)
    if args.cmd == "show":
        print(json.dumps(asdict(case), indent=1, default=str) if case else "no case")
        return 0
    import neatlogs
    neatlogs.init(workflow_name="agent-medic", tags=["agent-medic", "external", f"target:{args.target}"])
    from bisect_agent.neatlogs_api import NeatlogsReader
    reader = NeatlogsReader()
    case = case or Case(target=args.target)
    try:
        while case.phase not in ("done", "healthy", "escalated"):
            with neatlogs.trace(f"medic:external:{case.phase}", kind="AGENT", session_id=f"external-{case.id}"):
                step(spec, case, reader)
            save(case)
    finally:
        neatlogs.flush()
        neatlogs.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
