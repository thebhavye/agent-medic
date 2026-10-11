"""agent-medic command line.

The world (operated by you, or by the chaos script):
    agent-medic kb                         run the knowledge-base API (keep it running)
    agent-medic world init [--force]       create the production deployment
    agent-medic world release <name>       ship a release (see `world releases`)
    agent-medic world chaos <mode>         change the knowledge-base API: normal | schema_v2 | rate_limit | outage
    agent-medic traffic                    send production traffic to the deployed release

Medic:
    agent-medic watch                      watch production, open incidents, fix, deploy, confirm
    agent-medic incidents                  list incidents
    agent-medic incident <id>              show one incident in full
    agent-medic approve <id>               approve a deploy when watching with --no-auto-deploy
    agent-medic bisect ...                 the regression investigator on its own (agent-bisect)
"""

import argparse
import json
import sys

from dotenv import find_dotenv, load_dotenv


def cmd_kb(args) -> int:
    from target_agent import kb_service
    sys.argv = ["kb_service", "--port", str(args.port)]
    kb_service.main()
    return 0


def cmd_world(args) -> int:
    from medic import world
    if args.action == "init":
        print(f"deployed release {world.init(force=args.force)[:8]} at {world.paths.DEPLOY}")
    elif args.action == "release":
        print(f"released {world.release(args.name)[:8]}: {world.releases()[args.name]}")
    elif args.action == "releases":
        for name, subject in world.releases().items():
            print(f"  {name:14} {subject}")
    elif args.action == "chaos":
        print(f"knowledge-base API mode: {world.chaos(args.name)['mode']}")
    elif args.action == "status":
        print(f"deployed release: {world.head()[:8]} {world.git('log', '-1', '--format=%s')}")
        print(f"knowledge-base API: {world.kb_status()}")
    return 0


def cmd_traffic(args) -> int:
    import neatlogs  # noqa: F401  (the batches trace themselves; nothing to init here)
    from medic import traffic
    traffic.run(args.interval, args.size, args.workers, args.batches)
    return 0


def cmd_watch(args) -> int:
    import neatlogs
    neatlogs.init(workflow_name="agent-medic", tags=["agent-medic"])
    from bisect_agent.neatlogs_api import NeatlogsReader
    from medic.agent import Medic
    reader = NeatlogsReader()
    if not reader.enabled:
        print("set NEATLOGS_TOKEN and NEATLOGS_PROJECT_ID: Medic reads production from neatlogs", file=sys.stderr)
        return 2
    import time
    def log(message: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)
    medic = Medic(reader, auto_deploy=not args.no_auto_deploy, log=log)
    from medic import paths
    print(f"[medic] watching workflow '{paths.WORKFLOW}' every {args.interval:.0f}s", flush=True)
    try:
        if args.once:
            medic.tick()
        else:
            medic.run(args.interval)
    finally:
        neatlogs.flush()
        neatlogs.shutdown()
    return 0


def cmd_incidents(args) -> int:
    from medic import incidents
    for inc in incidents.load_all():
        cause = inc.decision.get("cause", "-")
        print(f"{inc.id}  {inc.phase:12} {cause:22} {'; '.join(s['name'] for s in inc.signals)}")
    return 0


def cmd_incident(args) -> int:
    from dataclasses import asdict
    from medic import incidents
    match = [i for i in incidents.load_all() if i.id.startswith(args.id)]
    if not match:
        print("no such incident", file=sys.stderr)
        return 1
    inc = match[-1]
    if args.json:
        print(json.dumps(asdict(inc), indent=1, default=str))
        return 0
    print(f"incident {inc.id}: {inc.phase}")
    for t in inc.timeline:
        print(f"  [{t['phase']}] {t['msg']}")
    return 0


def cmd_approve(args) -> int:
    from medic import incidents
    for inc in incidents.load_all():
        if inc.id.startswith(args.id) and inc.phase in ("remedied", "pr_ready"):
            incidents.approve(inc.id)
            print(f"approved: Medic opens the fix PR for {inc.id} on its next check" if inc.phase == "pr_ready"
                  else f"approved deploy for {inc.id}")
            return 0
    print("no incident waiting for approval with that id", file=sys.stderr)
    return 1


def cmd_doctor(args) -> int:
    from pathlib import Path

    from medic import doctor
    checks = doctor.run(Path(args.path).resolve())
    print(doctor.render(checks))
    return 1 if any(c.status == doctor.FAIL for c in checks) else 0


def cmd_start(args) -> int:
    from medic import service
    run = service.start(interval=args.interval, dashboard=args.dashboard, host=args.host)
    if run.get("already"):
        print(f"Medic is already running (pid {run['watch']['pid']}); `agent-medic stop` first to restart it")
        return 0
    if run.get("failed") is not None:
        print("Medic stopped right after starting. The end of its log:")
        for line in run["failed"] or ["(empty)"]:
            print("  " + line)
        return 1
    print(f"Medic started in the background (pid {run['watch']['pid']}), log: {run['watch']['log']}")
    if run.get("dashboard"):
        print(f"dashboard: {run['dashboard']['url']}")
    print("check on it: agent-medic status   |   stop it: agent-medic stop")
    return 0


def cmd_stop(args) -> int:
    from medic import service
    stopped = service.stop()
    print("stopped " + ", ".join(stopped) if stopped else "Medic wasn't running")
    return 0


def cmd_status(args) -> int:
    import time

    from medic import service
    st = service.status()
    if not st["running"]:
        print("Medic is not running (start it: agent-medic start)")
    else:
        since = time.strftime("%H:%M:%S", time.localtime(st["started"])) if st.get("started") else "?"
        print(f"Medic is running (pid {st['watch']['pid']}, since {since})")
        if st.get("dashboard"):
            d = st["dashboard"]
            print(f"dashboard: {d['url']}" + ("" if d["alive"] else " (stopped)"))
    for inc in st["open_incidents"]:
        print(f"open incident {inc['id']}: {inc['phase']}")
    for line in service.tail(5):
        print("  " + line)
    return 0 if st["running"] else 1


def cmd_logs(args) -> int:
    from medic import service
    lines = service.tail(args.n, "dashboard" if args.dashboard else "watch")
    print("\n".join(lines) if lines else "no log yet")
    return 0


def cmd_bisect(args) -> int:
    from bisect_agent import cli
    sys.argv = ["agent-medic bisect", *args.rest]
    return cli.main()


def cmd_dashboard(args) -> int:
    from pathlib import Path

    from medic import dashboard, paths
    dashboard.serve(args.port, Path(args.medic_dir) if args.medic_dir else paths.MEDIC_DIR, args.host)
    return 0


def cmd_init(args) -> int:
    from pathlib import Path

    from medic import init as init_mod
    root = Path(args.path)
    plan, edits, proj = init_mod.run(root, workflow=args.workflow, write=args.write, mode=args.mode, pr=args.pr,
                                     branch=args.branch)
    print(init_mod.report(plan))
    if proj is None:
        return 1
    if not args.write:
        print()
        print(init_mod.diff(plan, edits) or "(nothing to change: already connected)")
        print("\nThis was a dry run. Review the diff, then: agent-medic init --write")
        return 0
    print(f"\nwrote {', '.join(edits) or 'nothing (already connected)'}. Next:")
    print("  1. pip install -r requirements.txt, commit, and deploy with GIT_SHA set to the commit")
    print("     (and MEDIC_ENV=prod) in production")
    print("  2. agent-medic evals bootstrap --questions questions.txt   # a few real questions, one per line")
    print("     (or `agent-medic evals bootstrap` once production has traffic)")
    print("  3. agent-medic neatlogs setup        # detections and alert rules in your neatlogs project")
    print("  4. agent-medic doctor                # checks the rest, and says what's missing")
    print("  5. agent-medic start --dashboard     # Medic in the background; `agent-medic stop` to end it")
    return 0


def cmd_evals(args) -> int:
    from pathlib import Path

    from medic import evals, paths
    if args.questions:
        questions = [q.strip() for q in Path(args.questions).read_text(encoding="utf-8").splitlines() if q.strip()]
        cases = evals.from_questions(questions, runs=args.runs)
        source = f"{len(questions)} questions answered by the current code"
    else:
        from bisect_agent.neatlogs_api import NeatlogsReader
        cases = evals.from_traces(NeatlogsReader(), days=args.days, limit=args.limit)
        source = f"healthy production traces of '{paths.WORKFLOW}' from the last {args.days} days"
    if not cases:
        print(f"no usable known-answer cases from {source}")
        return 1
    out = evals.write(cases, Path(paths.CASES))
    print(f"{len(cases)} known-answer cases from {source} -> {out}")
    for c in cases[:5]:
        print(f"  {c['question'][:70]}  expects {', '.join(g[0] for g in c['expect'])}")
    return 0


def cmd_neatlogs(args) -> int:
    from medic import neatlogs_link, watcher
    if args.action == "setup":
        baseline = watcher.load_state().get("baseline")
        state = neatlogs_link.setup(neatlogs_link.Api(), baseline=baseline, webhook_url=args.webhook_url)
        print(f"detections: {len(state.get('detections', {}))}, alert rules: {len(state.get('rules', {}))}, "
              f"webhook: {'yes' if state.get('webhook') else 'no (Medic reads alert history instead)'}")
        for key, why in (state.get("skipped") or {}).items():
            print(f"  skipped {key}: {why}")
        return 0
    state = neatlogs_link.load()
    if not state.get("rules"):
        print("not set up: run `agent-medic neatlogs setup`")
        return 1
    api = neatlogs_link.Api()
    fires = api.call("GET", "/alert-history", limit=10).get("alertHistory", [])
    ours = set(state["rules"].values())
    print(f"detections {len(state['detections'])}, rules {len(ours)}, webhook {'yes' if state.get('webhook') else 'no'}")
    for f in fires:
        if f.get("ruleId") in ours:
            print(f"  {f.get('firedAt')}  {f.get('ruleName')}  count={f.get('count')}  traces={len(f.get('sampleTraceIds') or [])}")
    return 0


def main() -> int:
    load_dotenv(find_dotenv(usecwd=True))   # the project's .env (where the command runs), not Medic's
    for stream in (sys.stdout, sys.stderr):   # model text can contain any Unicode
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="agent-medic")
    sub = parser.add_subparsers(dest="cmd", required=True)

    kb = sub.add_parser("kb", help="run the knowledge-base API")
    kb.add_argument("--port", type=int, default=8781)
    kb.set_defaults(func=cmd_kb)

    w = sub.add_parser("world", help="operate the production world")
    w.add_argument("action", choices=["init", "release", "releases", "chaos", "status"])
    w.add_argument("name", nargs="?")
    w.add_argument("--force", action="store_true")
    w.set_defaults(func=cmd_world)

    t = sub.add_parser("traffic", help="send production traffic")
    t.add_argument("--interval", type=float, default=15)
    t.add_argument("--size", type=int, default=3)
    t.add_argument("--workers", type=int, default=3)
    t.add_argument("--batches", type=int)
    t.set_defaults(func=cmd_traffic)

    wa = sub.add_parser("watch", help="run Medic")
    wa.add_argument("--interval", type=float, default=20)
    wa.add_argument("--once", action="store_true")
    wa.add_argument("--no-auto-deploy", action="store_true")
    wa.set_defaults(func=cmd_watch)

    sub.add_parser("incidents").set_defaults(func=cmd_incidents)
    i = sub.add_parser("incident")
    i.add_argument("id")
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=cmd_incident)
    a = sub.add_parser("approve")
    a.add_argument("id")
    a.set_defaults(func=cmd_approve)

    ini = sub.add_parser("init", help="connect your agent to Medic: one reviewable diff and a medic.toml")
    ini.add_argument("path", nargs="?", default=".")
    ini.add_argument("--write", action="store_true", help="apply the edits (default: show the diff only)")
    ini.add_argument("--workflow", help="neatlogs workflow name (default: the folder name)")
    ini.add_argument("--mode", choices=["pr", "managed"], default="pr", help="pr: fixes arrive as pull requests")
    ini.add_argument("--pr", choices=["open", "ready"], default="open",
                     help="open: Medic opens the fix PR; ready: it prepares it and you open it")
    ini.add_argument("--branch", help="the branch fixes go to (default: the current one)")
    ini.set_defaults(func=cmd_init)

    ev = sub.add_parser("evals", help="known-answer questions from healthy traffic (no tests to write)")
    ev.add_argument("action", choices=["bootstrap"])
    ev.add_argument("--questions", help="a file of questions (one per line), for an app without traffic yet")
    ev.add_argument("--days", type=int, default=7)
    ev.add_argument("--runs", type=int, default=5, help="answers per question; only facts every answer states are kept")
    ev.add_argument("--limit", type=int, default=30)
    ev.set_defaults(func=cmd_evals)

    n = sub.add_parser("neatlogs", help="connect Medic to neatlogs: detections, alert rules, webhook")
    n.add_argument("action", choices=["setup", "status"])
    n.add_argument("--webhook-url", help="public URL of this Medic's /hooks/neatlogs (e.g. the EC2 dashboard)")
    n.set_defaults(func=cmd_neatlogs)

    d = sub.add_parser("dashboard", help="live dashboard: watch production break and Medic heal it")
    d.add_argument("--port", type=int, default=8790)
    d.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to serve other machines (e.g. on EC2)")
    d.add_argument("--medic-dir", default=None, help="a benchmark run under .medic/bench to replay it")
    d.set_defaults(func=cmd_dashboard)

    doc = sub.add_parser("doctor", help="check this project is ready for Medic, and say what to fix")
    doc.add_argument("path", nargs="?", default=".")
    doc.set_defaults(func=cmd_doctor)

    st = sub.add_parser("start", help="run Medic in the background (watch, and optionally the dashboard)")
    st.add_argument("--interval", type=float, default=20)
    st.add_argument("--dashboard", type=int, nargs="?", const=8790, help="also serve the dashboard (port 8790)")
    st.add_argument("--host", default="127.0.0.1", help="dashboard host; 0.0.0.0 to serve other machines")
    st.set_defaults(func=cmd_start)
    sub.add_parser("stop", help="stop the background Medic, and everything it started").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="is Medic running, and what is it working on").set_defaults(func=cmd_status)
    lg = sub.add_parser("logs", help="the end of Medic's log")
    lg.add_argument("-n", type=int, default=40)
    lg.add_argument("--dashboard", action="store_true", help="the dashboard's log instead")
    lg.set_defaults(func=cmd_logs)

    b = sub.add_parser("bisect", help="agent-bisect: the regression investigator on its own")
    b.add_argument("rest", nargs=argparse.REMAINDER)
    b.set_defaults(func=cmd_bisect)

    args = parser.parse_args()
    # A medic.toml means a user's project: Medic works on that repo instead of its demo world.
    if args.cmd in ("watch", "neatlogs", "evals", "incidents", "incident", "approve", "dashboard",
                    "start", "stop", "status", "logs"):
        from medic import project
        found = project.find()
        if found:
            project.activate(project.load(found))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
