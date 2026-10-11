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

from dotenv import load_dotenv


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
        if inc.id.startswith(args.id) and inc.phase == "remedied":
            inc.deploy["approved"] = True
            incidents.save(inc)
            print(f"approved deploy for {inc.id}")
            return 0
    print("no incident waiting for approval with that id", file=sys.stderr)
    return 1


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
    plan, edits, proj = init_mod.run(root, workflow=args.workflow, write=args.write, mode=args.mode,
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
    print("  1. commit, and deploy with GIT_SHA set to the commit (and MEDIC_ENV=prod) in production")
    print("  2. agent-medic neatlogs setup        # detections and alert rules in your neatlogs project")
    print("  3. agent-medic evals bootstrap       # known-answer questions from your healthy traffic")
    print("  4. agent-medic watch")
    return 0


def cmd_evals(args) -> int:
    from pathlib import Path

    from medic import evals, paths
    if args.questions:
        questions = [q.strip() for q in Path(args.questions).read_text(encoding="utf-8").splitlines() if q.strip()]
        cases = evals.from_questions(questions)
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
    load_dotenv()
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
    ini.add_argument("--branch", help="the branch fixes go to (default: the current one)")
    ini.set_defaults(func=cmd_init)

    ev = sub.add_parser("evals", help="known-answer questions from healthy traffic (no tests to write)")
    ev.add_argument("action", choices=["bootstrap"])
    ev.add_argument("--questions", help="a file of questions (one per line), for an app without traffic yet")
    ev.add_argument("--days", type=int, default=7)
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

    b = sub.add_parser("bisect", help="agent-bisect: the regression investigator on its own")
    b.add_argument("rest", nargs=argparse.REMAINDER)
    b.set_defaults(func=cmd_bisect)

    args = parser.parse_args()
    # A medic.toml means a user's project: Medic works on that repo instead of its demo world.
    if args.cmd in ("watch", "neatlogs", "evals", "incidents", "incident", "approve", "dashboard"):
        from medic import project
        found = project.find()
        if found:
            project.activate(project.load(found))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
