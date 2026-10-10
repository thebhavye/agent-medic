"""Single-file HTML report: bisection timeline, culprit, intent, fix attempts, before/after."""

import time
from html import escape
from pathlib import Path

from bisect_agent import gitops
from bisect_agent.metrics import SIGNATURES
from bisect_agent.report import STATUS_LABEL, run_cost
from bisect_agent.state import RunState

SIGNATURE_TITLE = {"errors": "Errors", "quality": "Answer quality", "cost": "Cost", "tool_calls": "Tool calls"}

CSS = """
:root {
  --bg: #f7f7f5; --surface: #ffffff; --text: #1d1d1b; --muted: #6b6b66; --border: #e3e2dd;
  --good: #1f7a4d; --good-bg: #e5f3eb; --bad: #b4372b; --bad-bg: #fbe9e6;
  --skip: #8a8a84; --accent: #3554d1; --accent-bg: #e9edfb; --code-bg: #f0efeb;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #161615; --surface: #1f1f1d; --text: #ecebe6; --muted: #9c9b94; --border: #33332f;
    --good: #5cc28c; --good-bg: #1c3327; --bad: #f07a6c; --bad-bg: #3b1f1b;
    --skip: #8a8a84; --accent: #8ea2f5; --accent-bg: #23294a; --code-bg: #2a2a27;
  }
}
:root[data-theme="dark"] {
  --bg: #161615; --surface: #1f1f1d; --text: #ecebe6; --muted: #9c9b94; --border: #33332f;
  --good: #5cc28c; --good-bg: #1c3327; --bad: #f07a6c; --bad-bg: #3b1f1b;
  --skip: #8a8a84; --accent: #8ea2f5; --accent-bg: #23294a; --code-bg: #2a2a27;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 920px; margin: 0 auto; padding: 32px 16px 64px; }
code, .mono { font-family: ui-monospace, "Cascadia Code", Consolas, monospace; font-size: 0.9em; }
h1 { font-size: 26px; margin: 4px 0 6px; letter-spacing: -0.01em; }
h2 { font-size: 19px; margin: 0; }
h3 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin: 22px 0 10px; }
.eyebrow { color: var(--muted); font-size: 13px; }
.lede { color: var(--muted); margin: 0 0 20px; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 28px; }
.chip { background: var(--surface); border: 1px solid var(--border); border-radius: 999px; padding: 4px 12px; font-size: 13px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 20px; }
.card-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.badge { font-size: 12px; font-weight: 600; border-radius: 999px; padding: 2px 10px; }
.badge.fixed { background: var(--good-bg); color: var(--good); }
.badge.open { background: var(--bad-bg); color: var(--bad); }
.metric-line { margin: 6px 0 0; color: var(--muted); }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { text-align: right; padding: 8px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--muted); font-weight: 500; font-size: 13px; }
tr:last-child td { border-bottom: 0; }
.worse { color: var(--bad); font-weight: 600; }
.better { color: var(--good); font-weight: 600; }
.timeline { list-style: none; margin: 0; padding: 0; }
.commit { display: grid; grid-template-columns: 18px 72px minmax(0, 1fr) auto; gap: 10px; align-items: center;
  padding: 6px 8px; border-radius: 8px; }
.commit .dot { width: 12px; height: 12px; border-radius: 50%; border: 2px solid var(--skip); }
.commit.good .dot { background: var(--good); border-color: var(--good); }
.commit.bad .dot { background: var(--bad); border-color: var(--bad); }
.commit.skip .dot { background: var(--skip); }
.commit .subject { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.commit .tag { font-size: 12px; color: var(--muted); }
.commit.culprit { background: var(--bad-bg); }
.commit.culprit .tag { color: var(--bad); font-weight: 600; }
.commit.untested .subject { color: var(--muted); }
blockquote { margin: 0; padding: 10px 14px; border-left: 3px solid var(--accent); background: var(--accent-bg);
  border-radius: 0 8px 8px 0; white-space: pre-wrap; font-size: 14px; }
.source { font-size: 13px; color: var(--muted); margin-bottom: 6px; }
.attempt { border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; margin-bottom: 10px; }
.attempt-head { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
.attempt-head .result { font-weight: 600; }
.attempt.accepted .result { color: var(--good); }
.attempt.rejected .result { color: var(--bad); }
.attempt p { margin: 6px 0 0; font-size: 14px; }
.attempt .label { color: var(--muted); }
details { margin-top: 8px; }
summary { cursor: pointer; color: var(--accent); font-size: 13px; }
pre { background: var(--code-bg); padding: 10px 12px; border-radius: 8px; overflow-x: auto; font-size: 12.5px; margin: 6px 0; }
pre.del { border-left: 3px solid var(--bad); }
pre.add { border-left: 3px solid var(--good); }
footer { color: var(--muted); font-size: 13px; margin-top: 28px; }
@media (max-width: 600px) {
  .commit { grid-template-columns: 14px minmax(0, 1fr); }
  .commit .sha { display: none; }
  .commit .tag { grid-column: 2; }
}
"""

COLUMNS = [("Pass rate", "pass_rate", "pct", False), ("Error rate", "error_rate", "pct", True),
           ("Cost / run", "cost_per_run", "usd", True), ("Tokens / run", "tokens_per_run", "int", True),
           ("Tool calls / run", "tool_calls_per_run", "dec", True)]


def _fmt(kind: str, v: float) -> str:
    return {"pct": f"{v:.0%}", "usd": f"${v:.4f}", "int": f"{v:,.0f}", "dec": f"{v:.1f}"}[kind]


def _metrics_table(rows: list[tuple[str, dict | None]], baseline: dict | None) -> str:
    head = "".join(f"<th>{escape(c[0])}</th>" for c in COLUMNS)
    body = []
    for label, m in rows:
        if not m:
            continue
        cells = []
        for _, key, kind, higher_is_worse in COLUMNS:
            cls = ""
            if baseline and label != "Baseline":
                b, v = baseline[key], m[key]
                delta = (v - b) if kind in ("pct",) else (v / b - 1 if b else 0)
                threshold = 0.1 if kind == "pct" else 0.25
                if abs(delta) >= threshold:
                    cls = "worse" if (delta > 0) == higher_is_worse else "better"
            cells.append(f'<td class="{cls}">{_fmt(kind, m[key])}</td>')
        body.append(f"<tr><td>{escape(label)}</td>{''.join(cells)}</tr>")
    return f'<div class="table-wrap"><table><thead><tr><th></th>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _timeline(state: RunState, inv, commits: list[str], repo: Path) -> str:
    order = [s for s in inv.verdicts if s not in (state.good, state.bad)]
    items = []
    for sha in commits:
        verdict = inv.verdicts.get(sha, "untested")
        classes = ["commit", verdict]
        if sha == state.good:
            tag = "known good"
        elif sha == state.bad:
            tag = "known bad"
        elif sha in order:
            tag = f"step {order.index(sha) + 1}: {verdict}"
        else:
            tag = ""
        if sha == inv.culprit:
            classes.append("culprit")
            tag = "first bad commit" + (f" · {tag}" if tag and not tag.startswith("known") else "")
        items.append(
            f'<li class="{" ".join(classes)}"><span class="dot"></span>'
            f'<code class="sha">{sha[:8]}</code>'
            f'<span class="subject">{escape(gitops.subject(sha, repo))}</span>'
            f'<span class="tag">{escape(tag)}</span></li>')
    return f'<ol class="timeline">{"".join(items)}</ol>'


def _intent(inv) -> str:
    if not inv.intent:
        return ""
    if inv.intent["source"] == "entire":
        src = "Entire" + (f" checkpoint <code>{escape(inv.intent['checkpoint'])}</code>"
                          if inv.intent.get("checkpoint") else "")
        body = inv.intent.get("explain") or "\n\n".join(w["why"] for w in inv.intent.get("why", []))
    else:
        src = "the commit message (no Entire session for this commit)"
        body = inv.intent.get("commit_message", "")
    lines = body.strip().splitlines()
    shown = "\n".join(lines[:20]) + ("\n…" if len(lines) > 20 else "")
    return (f'<h3>Why the culprit was written</h3><div class="source">From {src}</div>'
            f"<blockquote>{escape(shown)}</blockquote>")


def _attempts(inv) -> str:
    if not inv.attempts:
        return ""
    out = ["<h3>Fix attempts</h3>"]
    for a in inv.attempts:
        status = "accepted" if a.get("accepted") else "rejected"
        title = escape(a.get("commit_title") or "(no proposal)")
        parts = [f'<div class="attempt {status}"><div class="attempt-head">'
                 f'<span class="result">Attempt {a["n"]}: {status}</span><span>{title}</span></div>']
        if a.get("diagnosis"):
            parts.append(f'<p><span class="label">Diagnosis:</span> {escape(a["diagnosis"])}</p>')
            parts.append(f'<p><span class="label">Intent kept:</span> {escape(a["how_intent_is_kept"])}</p>')
        if a.get("feedback"):
            parts.append(f'<p><span class="label">Verification:</span> {escape(a["feedback"])}</p>')
        if a.get("edits"):
            edits = "".join(f'<div class="mono">{escape(e["path"])}</div>'
                            f'<pre class="del">{escape(e["find"])}</pre><pre class="add">{escape(e["replace"])}</pre>'
                            for e in a["edits"])
            parts.append(f"<details><summary>{len(a['edits'])} edit(s)</summary>{edits}</details>")
        parts.append("</div>")
        out.append("".join(parts))
    return "".join(out)


def render(state: RunState, repo: Path) -> str:
    ev = state.evaluations
    baseline = ev.get(state.good[:12])
    bad = ev.get(state.bad[:12])
    tip = ev.get(f"tip-{state.fix_tip[:12]}") if state.fix_tip else None
    commits = gitops.commits_between(state.good, state.bad, repo)
    fixed = sum(i.status == "fixed" for i in state.investigations)
    eval_cost, fixer_cost = run_cost(state)
    source = (tip or bad or {}).get("source", "local")

    cards = []
    for idx, inv in enumerate(state.investigations, 1):
        sig = SIGNATURES[inv.signature]
        fmt = "usd" if sig.metric == "cost_per_run" else "pct" if not sig.ratio else "dec"
        badge = "fixed" if inv.status == "fixed" else "open"
        cards.append(
            f'<section class="card"><div class="card-head"><h2>{idx}. {SIGNATURE_TITLE[inv.signature]} regression</h2>'
            f'<span class="badge {badge}">{escape(STATUS_LABEL[inv.status])}</span></div>'
            f'<p class="metric-line"><code>{sig.metric}</code> {_fmt(fmt, inv.good_value)} at baseline, '
            f'{_fmt(fmt, inv.bad_value)} at the bad commit</p>'
            f"<h3>Bisection</h3>{_timeline(state, inv, commits, repo)}"
            f"{_intent(inv)}{_attempts(inv)}</section>")

    chips = [f"{len(commits) - 2} commits searched", f"{len(state.investigations)} regressions",
             f"{fixed} fixed and verified", f"{len(ev)} evaluations",
             f"${eval_cost + fixer_cost:.2f} total cost" + (" (simulated)" if source == "simulated" else "")]
    if source == "simulated":
        chips.insert(0, "Simulated run: no model calls")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-bisect report</title><style>{CSS}</style></head>
<body><main>
<div class="eyebrow">agent-bisect · run <code>{escape(state.run_id)}</code></div>
<h1>{fixed} of {len(state.investigations)} regressions found and fixed</h1>
<p class="lede">Searched <code>{state.good[:8]}</code> (good) to <code>{state.bad[:8]}</code> (bad).
Fixes are on <code>{escape(state.fix_branch)}</code> at <code>{state.fix_tip[:8]}</code>.</p>
<div class="chips">{"".join(f'<span class="chip">{escape(c)}</span>' for c in chips)}</div>
<section class="card"><h2>Before and after</h2><h3>Same eval set at each version</h3>
{_metrics_table([("Baseline", baseline), ("Bad", bad), ("Fixed", tip)], baseline)}</section>
{"".join(cards)}
<footer>Metrics source: {escape(source)}. Eval runs ${eval_cost:.2f}, fixer ${fixer_cost:.2f}.
Generated {time.strftime("%Y-%m-%d %H:%M")}.</footer>
</main></body></html>
"""


def write(state: RunState, repo: Path) -> Path:
    path = repo / ".bisect" / f"report-{state.run_id}.html"
    path.write_text(render(state, repo), encoding="utf-8")
    return path
