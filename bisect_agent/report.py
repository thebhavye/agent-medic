"""Write the run up as Markdown: what regressed, where, why, the fix, and the evidence."""

from pathlib import Path

from bisect_agent import gitops
from bisect_agent.metrics import SIGNATURES
from bisect_agent.state import RunState

STATUS_LABEL = {"fixed": "fixed and verified", "gave_up": "not fixed: needs a human",
                "ambiguous": "inconclusive", "bisecting": "in progress", "explaining": "in progress",
                "fixing": "in progress"}


def _metrics_row(label: str, m: dict | None) -> str:
    if not m:
        return f"| {label} | - | - | - | - | - |"
    return (f"| {label} | {m['pass_rate']:.0%} | {m['error_rate']:.0%} | ${m['cost_per_run']:.4f} | "
            f"{m['tokens_per_run']:.0f} | {m['tool_calls_per_run']:.1f} |")


def run_cost(state: RunState) -> tuple[float, float]:
    """(spent running evals, spent on the fixer) in USD."""
    evals = sum(e["cost_per_run"] * e["n"] for e in state.evaluations.values())
    fixer = sum(a.get("fixer_cost_usd", 0.0) for i in state.investigations for a in i.attempts)
    return evals, fixer


def render(state: RunState, repo: Path) -> str:
    ev = state.evaluations
    baseline = ev.get(state.good[:12])
    bad = ev.get(state.bad[:12])
    tip = ev.get(f"tip-{state.fix_tip[:12]}") if state.fix_tip else None
    fixed = sum(i.status == "fixed" for i in state.investigations)
    eval_cost, fixer_cost = run_cost(state)

    lines = [
        f"# agent-bisect run `{state.run_id}`",
        "",
        f"Range: `{state.good[:10]}` (good) .. `{state.bad[:10]}` (bad). "
        f"{len(state.investigations)} regression(s) investigated, {fixed} fixed and verified.",
        f"Fix branch: `{state.fix_branch}` at `{state.fix_tip[:10]}`." if state.fix_branch else "",
        "",
        "## Before and after",
        "",
        "| Version | Pass rate | Error rate | Cost/run | Tokens/run | Tool calls/run |",
        "|---|---|---|---|---|---|",
        _metrics_row(f"Baseline `{state.good[:8]}`", baseline),
        _metrics_row(f"Bad `{state.bad[:8]}`", bad),
        _metrics_row(f"Fixed `{state.fix_tip[:8]}`", tip),
        "",
        f"Metrics source: {(tip or bad or {}).get('source', 'local')}. "
        f"Cost of this run: ${eval_cost:.2f} on evals, ${fixer_cost:.2f} on the fixer, "
        f"{len(ev)} evaluations.",
    ]

    for idx, inv in enumerate(state.investigations, 1):
        sig = SIGNATURES[inv.signature]
        lines += ["", f"## {idx}. {inv.signature} regression: {STATUS_LABEL[inv.status]}", "",
                  f"`{sig.metric}`: {inv.good_value:.4g} (baseline) -> {inv.bad_value:.4g} (bad)."]
        if inv.culprit:
            lines += ["", f"**First bad commit:** `{inv.culprit[:10]}` {inv.culprit_subject}"]
        tested = [(s, v) for s, v in inv.verdicts.items() if s not in (state.good, state.bad)]
        if tested:
            lines += ["", "Bisection steps:", ""]
            for sha, verdict in tested:
                lines.append(f"- `{sha[:8]}` {gitops.subject(sha, repo)}: **{verdict}**")
        if inv.intent:
            src = "Entire" + (f" checkpoint `{inv.intent['checkpoint']}`" if inv.intent.get("checkpoint") else "") \
                if inv.intent["source"] == "entire" else "the commit message (no Entire session found)"
            lines += ["", f"Why it was made, from {src}:", ""]
            body = inv.intent.get("explain") or inv.intent.get("commit_message", "")
            lines += [f"> {line}" if line else ">" for line in body.strip().splitlines()[:15]]
        for a in inv.attempts:
            lines += ["", f"**Attempt {a['n']}:** {'accepted' if a['accepted'] else 'rejected'}. "
                          f"{a.get('feedback', '')}"]
            if a.get("diagnosis"):
                lines += ["", f"Diagnosis: {a['diagnosis']}", "", f"Intent kept: {a['how_intent_is_kept']}"]
        if inv.fix_commit:
            lines += ["", f"Fix commit: `{inv.fix_commit[:10]}`"]

    return "\n".join(lines).replace("\n\n\n", "\n\n") + "\n"


def write(state: RunState, repo: Path) -> Path:
    path = repo / ".bisect" / f"report-{state.run_id}.md"
    path.write_text(render(state, repo), encoding="utf-8")
    return path
