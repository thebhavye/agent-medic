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
    print(f"[medic] watching workflow 'research-agent' every {args.interval:.0f}s", flush=True)
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
    dashboard.serve(args.port, Path(args.medic_dir) if args.medic_dir else paths.MEDIC_DIR)
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

    d = sub.add_parser("dashboard", help="live dashboard: watch production break and Medic heal it")
    d.add_argument("--port", type=int, default=8790)
    d.add_argument("--medic-dir", default=None, help="a benchmark run under .medic/bench to replay it")
    d.set_defaults(func=cmd_dashboard)

    b = sub.add_parser("bisect", help="agent-bisect: the regression investigator on its own")
    b.add_argument("rest", nargs=argparse.REMAINDER)
    b.set_defaults(func=cmd_bisect)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
