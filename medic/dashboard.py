"""Live dashboard: watch production break and Medic heal it.

A stdlib HTTP server with one page (dashboard.html) and one endpoint, /api/state, which reads
MEDIC_DIR fresh on every request. The page polls it, so a running incident moves through its
phases on screen. Point --medic-dir at a benchmark run to replay a finished incident.

The page tells each incident as a case file: what was detected, what each investigator found,
which repair candidates competed and why the winner won, and what the canary showed before
the fix was promoted.
"""

import argparse
import calendar
import difflib
import json
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from medic import paths
from medic.evidence import NON_ANSWER
from medic.watcher import RECENT              # the window Medic decides health on

PAGE = Path(__file__).with_name("dashboard.html")
TRACE_LIMIT = 120
PHASES = ["open", "triaged", "investigated", "decided", "remedied", "canary", "deployed", "resolved"]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _git(repo: Path, *args) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, encoding="utf-8",
                              errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def releases(deploy: Path) -> dict:
    """sha -> {subject, at} for every commit in the deployment repo (fix branches included)."""
    out = {}
    for line in _git(deploy, "log", "--all", "--format=%H%x1f%s%x1f%ct", "-n", "300").splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            out[parts[0]] = {"subject": parts[1], "at": float(parts[2] or 0)}
    return out


def trace_failed(trace: dict) -> bool:
    """Same rule as the watcher: an error, or no real answer."""
    answer = trace.get("answer") or ""
    return str(trace.get("status", "")).upper() == "ERROR" or not answer or bool(NON_ANSWER.search(answer))


def _utc(value) -> float | None:
    """ISO-8601 UTC ("2026-10-10T11:14:10.105Z") or epoch seconds -> epoch seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or len(value) < 19:
        return None
    try:
        return calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def slim_trace(trace: dict) -> dict:
    return {"id": trace.get("trace_id"), "at": _utc(trace.get("started_at")),
            "release": (trace.get("release") or "")[:12], "failed": trace_failed(trace),
            "error": str(trace.get("status", "")).upper() == "ERROR",
            "tool_errors": len(trace.get("tool_errors") or []), "tool_calls": trace.get("tool_calls"),
            "question": (trace.get("question") or "")[:200], "answer": (trace.get("answer") or "")[:240]}


def diff_lines(find: str, replace: str) -> list[list[str]]:
    """[op, text] rows, op in ' ', '-', '+'; unchanged runs longer than 4 lines are folded."""
    rows = []
    for line in difflib.ndiff(find.splitlines(), replace.splitlines()):
        if line[:1] in " -+":
            rows.append([line[:1], line[2:]])
    folded, run = [], []
    for row in rows + [["!", ""]]:
        if row[0] == " ":
            run.append(row)
            continue
        if len(run) > 4:
            run = run[:2] + [["~", f"{len(run) - 4} unchanged lines"]] + run[-2:]
        folded += run
        run = []
        if row[0] != "!":
            folded.append(row)
    return folded


def _edits(edits) -> list[dict]:
    return [{"path": e.get("path", ""), "lines": diff_lines(e.get("find", ""), e.get("replace", ""))}
            for e in edits or [] if isinstance(e, dict)]


def _target(t) -> str:
    if not isinstance(t, dict):
        return ""
    return f"{t.get('kind', '')} {t.get('name', '')}".strip() + (f" · {t['file']}:{t['start']}" if t.get("file") else "")


def candidate_view(c: dict) -> dict:
    m = c.get("metrics") or {}
    return {"n": c.get("candidate"), "target": _target(c.get("target")), "kind": (c.get("target") or {}).get("kind"),
            "title": c.get("commit_title") or "", "verdict": c.get("verdict") or "", "reason": c.get("feedback") or "",
            "pass_rate": m.get("pass_rate"), "error_rate": m.get("error_rate"), "runs": m.get("n"),
            "lines": c.get("changed_lines"), "root_cause": bool(c.get("at_root_cause")), "winner": bool(c.get("winner")),
            "diagnosis": c.get("diagnosis") or "", "edits": _edits(c.get("edits"))}


def attempt_view(a: dict) -> dict:
    view = {"n": a.get("n"), "accepted": bool(a.get("accepted")), "feedback": a.get("feedback") or "",
            "title": a.get("commit_title") or "", "diagnosis": a.get("diagnosis") or "",
            "intent": a.get("original_intent") or "", "intent_kept": a.get("how_intent_is_kept") or "",
            "commit": a.get("commit") or "", "winner": a.get("winner") or "", "model": (a.get("usage") or {}).get("model"),
            "metrics": a.get("metrics") or {}, "edits": _edits(a.get("edits")),
            "candidates": [candidate_view(c) for c in a.get("tournament") or []]}
    if view["candidates"]:
        view["candidates"].sort(key=lambda c: (not c["winner"], c["verdict"] != "passed", c["n"] or 0))
    return view


def investigator_view(name: str, f: dict) -> dict:
    view = {"name": name, "verdict": f.get("verdict") or "", "confidence": f.get("confidence"),
            "detail": f.get("detail") or ""}
    if name == "regression":
        verdicts = f.get("verdicts") or {}
        view.update(culprit=f.get("culprit"), culprit_subject=f.get("culprit_subject"), good=f.get("good"),
                    bad=f.get("bad"), tested=[[sha, v] for sha, v in verdicts.items()],
                    before=f.get("good_release_today"), after=f.get("bad_release_today"),
                    surfaces=[_target(s) for s in f.get("changed_surfaces") or []])
    elif name == "localize":
        view.update(divergences=[{"kind": d.get("kind"), "step": d.get("step"), "detail": d.get("detail"),
                                  "numbers": d.get("numbers") or {}, "traces": len(d.get("traces") or [])}
                                 for d in f.get("divergences") or []],
                    healthy=f.get("healthy_traces"), failing=f.get("failing_traces"),
                    suspects=[{"name": _target(s), "score": s.get("score")} for s in f.get("suspects") or []])
    return view


def incident_view(inc: dict) -> dict:
    timeline = [e for e in inc.get("timeline") or [] if isinstance(e, dict)]
    first = {}
    for e in timeline:
        phase = e.get("phase")
        if phase == "remedy":
            # Rejected attempts log a remedy event too; the repair is done only once one is accepted.
            if not re.search(r": accepted\b", e.get("msg", "")):
                continue
            phase = "remedied"
        first.setdefault(phase, e.get("t"))
    resolved_at = first.get("resolved") or first.get("escalated")
    deploy = inc.get("deploy") or {}
    triage = inc.get("triage") or {}
    findings = inc.get("findings") or {}
    order = ["dependency", "regression", "localize", "loops"]
    names = sorted(findings, key=lambda n: (order.index(n) if n in order else 9, n))
    return {
        "id": inc.get("id"), "phase": inc.get("phase"), "opened_at": inc.get("opened_at"), "ended_at": resolved_at,
        "release": inc.get("release"), "healthy_release": inc.get("healthy_release"),
        "phases": [{"phase": p, "t": first.get(p)} for p in PHASES if p in first or p in ("open", "resolved")]
        if inc.get("phase") != "escalated" else
        [{"phase": p, "t": first.get(p)} for p in PHASES[:-1] if p in first] + [{"phase": "escalated", "t": first.get("escalated")}],
        "timeline": [{"t": e.get("t"), "phase": e.get("phase"), "msg": e.get("msg", "")} for e in timeline],
        "signals": inc.get("signals") or [],
        "symptom": triage.get("summary") or "",
        "examples": [{"question": x.get("question", ""), "answer": (x.get("answer") or "")[:280],
                      "expect": x.get("expect"), "tool_errors": x.get("tool_errors") or []}
                     for x in (triage.get("examples") or [])[:2]],
        "investigators": [investigator_view(n, findings[n]) for n in names if isinstance(findings[n], dict)],
        "decision": inc.get("decision") or {},
        "attempts": [attempt_view(a) for a in (inc.get("remedy") or {}).get("attempts") or []],
        "canary": {"rollout": deploy.get("canary"), "verdict": deploy.get("canary_verdict"),
                   "from": deploy.get("from"),
                   "release": deploy.get("release"), "deployed": deploy.get("deployed"), "at": deploy.get("at")},
        "monitor": inc.get("monitor") or {},
    }


def window(traces: list[dict]) -> dict:
    n = len(traces)
    if not n:
        return {"n": 0}
    return {"n": n, "failure_rate": sum(t["failed"] for t in traces) / n,
            "tool_error_rate": sum(1 for t in traces if t["tool_errors"]) / n}


def build_state(medic_dir: Path) -> dict:
    medic_dir = Path(medic_dir)
    deploy = medic_dir / "deploy"
    state = _read_json(medic_dir / "state.json") or {}
    commits = releases(deploy)
    head = _git(deploy, "rev-parse", "HEAD").strip()
    traces = sorted((slim_trace(t) for t in state.get("traces") or [] if isinstance(t, dict)),
                    key=lambda t: t["at"] or 0)[-TRACE_LIMIT:]

    incidents = [incident_view(i) for i in
                 (_read_json(p) for p in sorted((medic_dir / "incidents").glob("*.json"))) if isinstance(i, dict)]
    incidents.sort(key=lambda i: i.get("opened_at") or 0, reverse=True)

    # Every release the page mentions gets a subject and a role, so a sha reads as a story.
    roles = {}
    for inc in incidents:
        for key, role in (("healthy_release", "healthy"), ("release", "broken")):
            if inc.get(key):
                roles.setdefault(inc[key][:12], role)
        culprit = (inc.get("decision") or {}).get("culprit")
        if culprit:
            roles[culprit[:12]] = "culprit"
        for a in inc["attempts"]:
            if a["commit"]:
                roles[a["commit"][:12]] = "fix"
    shas = {t["release"] for t in traces} | set(roles) | ({head[:12]} if head else set())
    release_info = {}
    for short in shas:
        full = next((s for s in commits if s.startswith(short)), None)
        release_info[short] = {"sha": full or short, "subject": (commits.get(full) or {}).get("subject", ""),
                               "role": roles.get(short, "release")}

    rollout = _read_json(medic_dir / "rollout.json")
    if isinstance(rollout, dict):
        mine = [t for t in traces if t["release"] == str(rollout.get("candidate", ""))[:12]]
        rollout = {**rollout, "canary_traces": len(mine), "canary_window": window(mine)}

    bench = []
    for p in sorted((medic_dir / "bench").glob("*/result.json")):
        r = _read_json(p)
        if isinstance(r, dict):
            bench.append({k: r.get(k) for k in ("scenario", "outcome", "cause", "remedy", "culprit_expected",
                                                "culprit_found", "detect_s", "resolve_s", "passed", "started",
                                                "fixer_provider", "attempts")} | {"dir": p.parent.name})

    external = []
    for p in sorted((medic_dir / "external").glob("case-*.json")):
        c = _read_json(p)
        if isinstance(c, dict):
            external.append({"id": c.get("id"), "target": c.get("target"), "phase": c.get("phase"),
                             "summary": (c.get("triage") or {}).get("summary", ""),
                             "decision": c.get("decision") or {},
                             "findings": [{"name": k, "verdict": (v or {}).get("verdict"), "detail": (v or {}).get("detail")}
                                          for k, v in (c.get("findings") or {}).items() if isinstance(v, dict)],
                             "attempts": [{"n": a.get("n"), "accepted": a.get("accepted"), "verdict": "blocked" if a.get("blocked") else ("accepted" if a.get("accepted") else "rejected"),
                                           "title": a.get("commit_title") or "",
                                           "feedback": a.get("feedback") or a.get("reason") or "",
                                           "review": a.get("review") or [], "edits": _edits(a.get("edits")),
                                           "runs": [{"topic": r.get("topic"), "exit": r.get("exit"),
                                                     "status": r.get("trace_status") or r.get("status"),
                                                     "trace": r.get("trace_id")} for r in a.get("verification") or []
                                                    if isinstance(r, dict)]}
                                          for a in c.get("attempts") or [] if isinstance(a, dict)],
                             "trail": (c.get("proposal") or {}).get("trail_url") or
                                      next(iter(re.findall(r"https://\S+", str((c.get("proposal") or {}).get("trail", "")))), None)})

    canary_runs = [{"at": r.get("at"), "release": (r.get("release") or "")[:12], "pass_rate": r.get("pass_rate"),
                    "n": r.get("n")} for r in state.get("canary") or [] if isinstance(r, dict)]
    return {
        "generated_at": time.time(), "source": medic_dir.name,
        "deploy": {"sha": head, "subject": (commits.get(head) or {}).get("subject", ""),
                   "at": (commits.get(head) or {}).get("at")} if head else None,
        "releases": release_info, "rollout": rollout,
        "production": {"traces": traces, "baseline": state.get("baseline"), "current": window(traces[-RECENT:]),
                       "total": len(state.get("traces") or []), "canary_runs": canary_runs,
                       "canary_baseline": state.get("canary_baseline"), "healthy_release": state.get("healthy_release")},
        "incidents": incidents, "bench": bench, "external": external,
    }


class Handler(BaseHTTPRequestHandler):
    medic_dir: Path = paths.MEDIC_DIR

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route in ("/", "/index.html"):
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif route == "/api/state":
            body = json.dumps(build_state(self.medic_dir), ensure_ascii=False, default=str).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def log_message(self, *args):
        pass


class Server(ThreadingHTTPServer):
    # On Windows, address reuse lets a second server bind a port that's already serving, and
    # requests then land on either one: fail loudly instead. On Linux reuse only skips
    # TIME_WAIT, which a quick systemd restart needs, so keep it there.
    allow_reuse_address = not sys.platform.startswith("win")


def serve(port: int, medic_dir: Path, host: str = "127.0.0.1") -> None:
    """Local only by default; pass host="0.0.0.0" to serve others (the page is read-only)."""
    handler = type("BoundHandler", (Handler,), {"medic_dir": Path(medic_dir)})
    server = Server((host, port), handler)
    print(f"[dashboard] http://{host}:{port}  (reading {medic_dir})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent-medic dashboard")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to serve other machines (e.g. on EC2)")
    parser.add_argument("--medic-dir", type=Path, default=paths.MEDIC_DIR,
                        help="a MEDIC_DIR, or a benchmark run under .medic/bench to replay it")
    args = parser.parse_args(argv)
    serve(args.port, args.medic_dir, args.host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
