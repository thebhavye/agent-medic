"""agent-bisect command line.

    agent-bisect run --good <rev> [--bad HEAD]   find, explain, fix and verify regressions
    agent-bisect run --resume                    continue an interrupted run
    agent-bisect status | report [--html]        inspect the current run
    agent-bisect simulate                        the whole loop offline, no API keys
    agent-bisect watch --workflow research-agent compare recent production traces in neatlogs
    agent-bisect pr                              push the fix branch and open a PR with the report
    agent-bisect clean                           remove worktrees and run state
"""

import argparse
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


def _repo() -> Path:
    from bisect_agent import gitops
    return Path(gitops.git("rev-parse", "--show-toplevel", cwd=Path.cwd()))


def cmd_run(args) -> int:
    import neatlogs
    neatlogs.init(workflow_name="agent-bisect", tags=["agent-bisect"])

    from bisect_agent import gitops, report, state as st
    from bisect_agent.evaluator import Evaluator
    from bisect_agent.neatlogs_api import NeatlogsReader
    from bisect_agent.orchestrator import Orchestrator

    repo = _repo()
    existing = st.load(repo)
    if args.resume:
        if not existing:
            print("nothing to resume; start with --good <rev>", file=sys.stderr)
            return 2
        run_state = existing
        run_state.log(f"resuming run {run_state.run_id} at phase {run_state.phase}")
    else:
        if existing and existing.phase != "done":
            print(f"run {existing.run_id} is unfinished: use --resume, or `agent-bisect clean`", file=sys.stderr)
            return 2
        if not args.good:
            print("--good is required", file=sys.stderr)
            return 2
        run_state = st.RunState(good=gitops.rev_parse(args.good, repo), bad=gitops.rev_parse(args.bad, repo))
        run_state.log(f"run {run_state.run_id}: good={run_state.good[:10]} bad={run_state.bad[:10]}")
    st.save(run_state, repo)

    reader = NeatlogsReader()
    if not reader.enabled:
        run_state.log("NEATLOGS_TOKEN/NEATLOGS_PROJECT_ID not set: scoring from local measurements only")
    evaluator = Evaluator(repo, run_state, Path(args.cases), reader)
    try:
        Orchestrator(repo, run_state, evaluator, max_attempts=args.max_attempts,
                     max_investigations=args.max_investigations).run()
    finally:
        st.save(run_state, repo)
        neatlogs.flush()
        neatlogs.shutdown()
    _write_reports(run_state, repo)
    return 0


def _write_reports(run_state, repo: Path) -> None:
    from bisect_agent import html_report, report
    print(f"\nreport: {report.write(run_state, repo)}")
    print(f"report: {html_report.write(run_state, repo)}")


def cmd_simulate(args) -> int:
    from bisect_agent import simulate
    dest = Path(args.dir).resolve() if args.dir else None
    if dest and dest.exists() and any(dest.iterdir()):
        print(f"{dest} is not empty", file=sys.stderr)
        return 2
    repo, run_state = simulate.run(dest)
    _write_reports(run_state, repo)
    return 0


def cmd_status(args) -> int:
    from bisect_agent import report, state as st
    repo = _repo()
    run_state = st.load(repo)
    if not run_state:
        print("no run")
        return 1
    print(f"run {run_state.run_id} phase={run_state.phase} evaluations={len(run_state.evaluations)}")
    for i, inv in enumerate(run_state.investigations, 1):
        print(f"  {i}. {inv.signature}: {inv.status}"
              + (f" culprit={inv.culprit[:8]}" if inv.culprit else "")
              + f" attempts={len(inv.attempts)}")
    evals, fixer = report.run_cost(run_state)
    print(f"spent: ${evals:.2f} evals + ${fixer:.2f} fixer")
    return 0


def cmd_report(args) -> int:
    from bisect_agent import report, state as st
    repo = _repo()
    run_state = st.load(repo)
    if not run_state:
        print("no run")
        return 1
    if args.html:
        from bisect_agent import html_report
        print(html_report.write(run_state, repo))
    else:
        print(report.render(run_state, repo))
    return 0


def cmd_watch(args) -> int:
    from bisect_agent.neatlogs_api import NeatlogsReader
    reader = NeatlogsReader()
    if not reader.enabled:
        print("set NEATLOGS_TOKEN and NEATLOGS_PROJECT_ID to read traces", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    window = timedelta(hours=args.hours)

    def window_stats(start, end):
        traces = reader.list_traces(workflowName=args.workflow, **{"from": start.isoformat(), "to": end.isoformat()})
        if not traces:
            return None
        n = len(traces)
        return {"n": n,
                "failure_rate": sum("err" in t["status"].lower() or "fail" in t["status"].lower() for t in traces) / n,
                "cost": sum(t["totalCostUsd"] for t in traces) / n,
                "tool_calls": sum(t["toolCallsCount"] for t in traces) / n}

    before = window_stats(now - 2 * window, now - window)
    after = window_stats(now - window, now)
    print(f"{args.workflow}: previous {args.hours}h {before}\n{args.workflow}: last {args.hours}h {after}")
    if not before or not after:
        print("not enough traces in one of the windows")
        return 0
    alerts = []
    if after["failure_rate"] - before["failure_rate"] >= 0.15:
        alerts.append(f"failure rate {before['failure_rate']:.0%} -> {after['failure_rate']:.0%}")
    if before["cost"] > 0 and after["cost"] / before["cost"] >= 1.5:
        alerts.append(f"cost/run ${before['cost']:.4f} -> ${after['cost']:.4f}")
    if before["tool_calls"] > 0 and after["tool_calls"] / before["tool_calls"] >= 1.5:
        alerts.append(f"tool calls/run {before['tool_calls']:.1f} -> {after['tool_calls']:.1f}")
    if alerts:
        print("REGRESSION: " + "; ".join(alerts))
        print("next: agent-bisect run --good <last known good commit>")
        return 3
    print("no regression")
    return 0


def cmd_pr(args) -> int:
    from bisect_agent import gitops, report, state as st
    repo = _repo()
    run_state = st.load(repo)
    if not run_state or not any(i.fix_commit for i in run_state.investigations):
        print("no verified fixes to propose", file=sys.stderr)
        return 1
    body = report.write(run_state, repo)
    gitops.git("push", "-u", args.remote, run_state.fix_branch, cwd=repo)
    title = f"Fix {sum(1 for i in run_state.investigations if i.fix_commit)} agent regression(s) found by agent-bisect"
    return subprocess.run(["gh", "pr", "create", "--head", run_state.fix_branch, "--base", args.base,
                           "--title", title, "--body-file", str(body)], cwd=repo).returncode


def cmd_clean(args) -> int:
    from bisect_agent import gitops
    repo = _repo()
    wt = repo / ".bisect" / "wt"
    if wt.exists():
        for path in wt.iterdir():
            gitops.worktree_remove(path, repo)
    shutil.rmtree(repo / ".bisect", ignore_errors=True)
    gitops.git("worktree", "prune", cwd=repo, check=False)
    print("removed .bisect (fix branches are kept)")
    return 0


def main() -> int:
    load_dotenv(find_dotenv(usecwd=True))
    for stream in (sys.stdout, sys.stderr):   # model text can contain any Unicode
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="agent-bisect")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="find, explain, fix and verify regressions")
    run.add_argument("--good", help="last known good revision")
    run.add_argument("--bad", default="HEAD")
    run.add_argument("--cases", default="evals/cases.jsonl")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--max-attempts", type=int, default=3)
    run.add_argument("--max-investigations", type=int, default=5)
    run.set_defaults(func=cmd_run)

    sub.add_parser("status").set_defaults(func=cmd_status)
    rep = sub.add_parser("report")
    rep.add_argument("--html", action="store_true", help="write the HTML report and print its path")
    rep.set_defaults(func=cmd_report)

    sim = sub.add_parser("simulate", help="run the whole loop offline on a seeded copy of the repo")
    sim.add_argument("--dir", help="where to create the simulation repo (default: a temp dir)")
    sim.set_defaults(func=cmd_simulate)

    watch = sub.add_parser("watch", help="compare recent production traces in neatlogs")
    watch.add_argument("--workflow", default="research-agent")
    watch.add_argument("--hours", type=float, default=24)
    watch.set_defaults(func=cmd_watch)

    pr = sub.add_parser("pr", help="push the fix branch and open a pull request")
    pr.add_argument("--remote", default="origin")
    pr.add_argument("--base", default="main")
    pr.set_defaults(func=cmd_pr)

    sub.add_parser("clean").set_defaults(func=cmd_clean)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
