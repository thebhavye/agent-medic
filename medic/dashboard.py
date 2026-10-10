"""Live demo page for Agent Medic.

One self-contained page (inline CSS/JS, stdlib HTTP only) that reads MEDIC_DIR fresh on
every request, so judges watch production break and Medic heal it without a reload.
"""

import argparse
import json
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NON_ANSWER = re.compile(r"could not|couldn't|unable to|does not (provide|contain|have|include)|"
                        r"no information|not available|no documents|cannot find|can't find", re.I)
TRAIL_URL = re.compile(r"https?://\S+")
OPEN_PHASES = {"open", "triaged", "investigated", "decided", "remedied", "deployed"}
STEPPER = ["open", "triaged", "investigated", "decided", "remedied", "deployed", "resolved"]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_failed(trace: dict) -> bool:
    if str(trace.get("status", "")).upper() == "ERROR":
        return True
    answer = trace.get("answer") or ""
    return not answer or bool(NON_ANSWER.search(answer))


def window_metrics(traces: list[dict]) -> dict:
    n = len(traces)
    if not n:
        return {"n": 0}
    failed = sum(1 for t in traces if is_failed(t))
    tool_err = sum(1 for t in traces if t.get("tool_errors"))
    non_ans = sum(1 for t in traces if not (t.get("answer") or "") or NON_ANSWER.search(t.get("answer") or ""))
    return {
        "n": n,
        "failure_rate": failed / n,
        "tool_error_rate": tool_err / n,
        "non_answer_rate": non_ans / n,
        "cost_per_run": sum(float(t.get("cost_usd") or 0) for t in traces) / n,
        "tokens_per_run": sum(int(t.get("tokens") or 0) for t in traces) / n,
        "tool_calls_per_run": sum(int(t.get("tool_calls") or 0) for t in traces) / n,
        "repeated_call_rate": sum(1 for t in traces if (t.get("repeated_calls") or 0) > 0) / n,
        "releases": sorted({str(t.get("release") or "") for t in traces if t.get("release")}),
    }


def slim_trace(trace: dict) -> dict:
    return {
        "trace_id": trace.get("trace_id", ""),
        "started_at": trace.get("started_at", ""),
        "release": str(trace.get("release") or ""),
        "status": trace.get("status", ""),
        "tokens": int(trace.get("tokens") or 0),
        "tool_calls": int(trace.get("tool_calls") or 0),
        "cost_usd": float(trace.get("cost_usd") or 0),
        "failed": is_failed(trace),
        "tool_errors": len(trace.get("tool_errors") or []),
    }


def deploy_info(medic_dir: Path):
    repo = medic_dir / "deploy"
    if not (repo / ".git").exists() and not (repo / "HEAD").exists():
        return None
    try:
        out = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%h|%s"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    sha, _, subject = out.stdout.strip().partition("|")
    return {"sha": sha, "subject": subject}


def build_state(medic_dir: Path) -> dict:
    """Assemble /api/state from disk; every file is optional or skippable."""
    medic_dir = Path(medic_dir)
    state = _read_json(medic_dir / "state.json") or {}
    traces = state.get("traces") or []
    if not isinstance(traces, list):
        traces = []

    incidents = []
    try:
        files = sorted((medic_dir / "incidents").glob("*.json"))
    except OSError:
        files = []
    for path in files:
        inc = _read_json(path)
        if isinstance(inc, dict):
            incidents.append(inc)
    incidents.sort(key=lambda i: (i.get("opened_at") or 0, i.get("id") or ""), reverse=True)

    bench = []
    try:
        results = sorted((medic_dir / "bench").glob("*/result.json"))
    except OSError:
        results = []
    for path in results:
        res = _read_json(path)
        if isinstance(res, dict):
            res = dict(res)
            res.setdefault("name", path.parent.name)
            bench.append(res)
    bench.sort(key=lambda r: r.get("name", ""))

    external = []
    try:
        cases = sorted((medic_dir / "external").glob("case-*.json"))
    except OSError:
        cases = []
    for path in cases:
        case = _read_json(path)
        if isinstance(case, dict):
            external.append(case)

    canary_runs = state.get("canary") or []
    production = {
        "traces": [slim_trace(t) for t in traces[-60:] if isinstance(t, dict)],
        "n_traces": len(traces),
        "baseline": state.get("baseline"),
        "current": window_metrics([t for t in traces[-6:] if isinstance(t, dict)]),
        "canary": canary_runs[-1] if isinstance(canary_runs, list) and canary_runs else None,
        "canary_baseline": state.get("canary_baseline"),
        "healthy_release": state.get("healthy_release"),
        "since": state.get("since"),
    }
    return {
        "production": production,
        "incidents": incidents,
        "deploy": deploy_info(medic_dir),
        "bench": bench,
        "external": external,
    }


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Medic</title>
<style>
:root{
  color-scheme:dark;
  --bg:#0d1117; --panel:#161b22; --panel2:#1c2330; --border:#2d3442;
  --fg:#e6edf3; --muted:#8b949e; --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --green:#3fb950; --red:#f85149; --amber:#d29922; --blue:#58a6ff; --purple:#bc8cff;
}
@media (prefers-color-scheme: light){
  :root{
    color-scheme:light;
    --bg:#ffffff; --panel:#f6f8fa; --panel2:#eef1f4; --border:#d0d7de;
    --fg:#1f2328; --muted:#59636e;
    --green:#1a7f37; --red:#d1242c; --amber:#9a6700; --blue:#0969da; --purple:#8250df;
  }
}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);margin:0;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  line-height:1.5}
.wrap{max-width:1180px;margin:0 auto;padding:20px 20px 60px}
header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:18px}
header h1{font-size:26px;margin:0;letter-spacing:-.02em}
.deploy{font-family:var(--mono);font-size:12.5px;color:var(--muted)}
.pill{font-weight:700;font-size:15px;padding:6px 16px;border-radius:999px;letter-spacing:.04em;
  border:1px solid var(--border);transition:background .5s}
.pill.ok{background:color-mix(in srgb,var(--green) 22%,transparent);color:var(--green);border-color:var(--green)}
.pill.warn{background:color-mix(in srgb,var(--amber) 22%,transparent);color:var(--amber);border-color:var(--amber)}
.pill.bad{background:color-mix(in srgb,var(--red) 22%,transparent);color:var(--red);border-color:var(--red)}
section{background:var(--panel);border:1px solid var(--border);border-radius:12px;
  padding:18px 20px;margin:16px 0}
section h2{margin:0 0 4px;font-size:17px}
.sub{color:var(--muted);font-size:13px;margin:0 0 12px}
.grid2{display:grid;grid-template-columns:1.4fr 1fr;gap:18px}
@media (max-width:700px){.grid2{grid-template-columns:1fr}.wrap{padding:12px 12px 40px}}
.dots{display:flex;gap:4px;flex-wrap:wrap;align-items:flex-end;min-height:26px}
.dot{width:12px;height:12px;border-radius:50%;flex:none}
.dot.ok{background:var(--green)} .dot.bad{background:var(--red)}
.relmark{font-family:var(--mono);font-size:10px;color:var(--muted);writing-mode:vertical-rl;
  margin-left:2px;user-select:none}
svg.chart{width:100%;height:64px;display:block;background:var(--panel2);
  border:1px solid var(--border);border-radius:8px;margin-top:8px}
.chartlbl{font-size:11px;color:var(--muted);margin:8px 0 0}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--border);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
code,.mono{font-family:var(--mono);font-size:12.5px}
.muted{color:var(--muted)}
.stepper{display:flex;gap:0;margin:12px 0;flex-wrap:wrap}
.step{flex:1 1 0;min-width:90px;text-align:center;font-size:11.5px;color:var(--muted);
  padding-top:14px;position:relative;transition:color .4s}
.step::before{content:"";position:absolute;top:4px;left:0;right:0;height:3px;
  background:var(--border);border-radius:2px;transition:background .4s}
.step.done{color:var(--fg)} .step.done::before{background:var(--green)}
.step.now{color:var(--amber);font-weight:700} .step.now::before{background:var(--amber)}
.step .t{display:block;font-size:10.5px;font-family:var(--mono)}
.step.esc::before{background:var(--red)} .step.esc{color:var(--red);font-weight:700}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin-top:12px}
.card{background:var(--panel2);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.card h3{margin:0 0 8px;font-size:13.5px}
.badge{display:inline-block;font-size:11.5px;font-weight:700;padding:2px 10px;border-radius:999px;
  border:1px solid var(--border);margin:0 6px 6px 0}
.badge.g{color:var(--green);border-color:var(--green)}
.badge.r{color:var(--red);border-color:var(--red)}
.badge.a{color:var(--amber);border-color:var(--amber)}
.badge.b{color:var(--blue);border-color:var(--blue)}
.badge.p{color:var(--purple);border-color:var(--purple)}
ul{margin:6px 0;padding-left:20px;font-size:13.5px}
.diff{font-family:var(--mono);font-size:12px;border:1px solid var(--border);border-radius:8px;
  overflow:hidden;margin:8px 0}
.diff .path{background:var(--panel);color:var(--muted);padding:4px 10px;font-size:11.5px}
.diff pre{margin:0;padding:6px 10px;white-space:pre-wrap;word-break:break-word}
.diff .del{background:color-mix(in srgb,var(--red) 16%,transparent)}
.diff .add{background:color-mix(in srgb,var(--green) 16%,transparent)}
.empty{text-align:center;color:var(--muted);padding:60px 20px}
.empty .big{font-size:22px;color:var(--fg);margin-bottom:8px}
a{color:var(--blue)}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:24px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Agent Medic</h1>
    <span class="deploy" id="deploy">…</span>
    <span class="pill warn" id="pill">LOADING</span>
  </header>
  <div id="root"><div class="empty"><div class="big">Loading…</div></div></div>
  <footer>polls /api/state every 3s · no reload needed</footer>
</div>
<script>
"use strict";
var lastPhase = null;
function T(tag, text, cls){
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  e.textContent = (text === null || text === undefined) ? "" : String(text);
  return e;
}
function fmtPct(x){
  if (x === null || x === undefined || isNaN(x)) return "—";
  return (x * 100).toFixed(1) + "%";
}
function fmtNum(x, d){
  if (x === null || x === undefined || isNaN(x)) return "—";
  return Number(x).toFixed(d === undefined ? 1 : d);
}
function fmtTime(s){
  if (!s) return "—";
  try {
    if (typeof s === "number") return new Date(s * 1000).toLocaleString();
    return new Date(s).toLocaleString();
  } catch (e) { return String(s); }
}
function fmtClock(s){
  if (!s) return "";
  try {
    if (typeof s === "number") return new Date(s * 1000).toLocaleTimeString();
    return new Date(s).toLocaleTimeString();
  } catch (e) { return ""; }
}
function openIncidents(all){
  return all.filter(function(i){
    return i && ["resolved", "escalated", "done"].indexOf(i.phase) < 0;
  });
}
function renderStatus(data){
  var pill = document.getElementById("pill");
  var dep = document.getElementById("deploy");
  dep.textContent = "";
  if (data.deploy) {
    dep.appendChild(T("span", (data.deploy.sha || "") + " "));
    dep.appendChild(T("span", data.deploy.subject || ""));
  } else {
    dep.appendChild(T("span", "no deployment yet"));
  }
  var open = openIncidents(data.incidents);
  var esc = data.incidents.filter(function(i){ return i && i.phase === "escalated"; });
  pill.className = "pill " + (esc.length ? "bad" : open.length ? "warn" : "ok");
  pill.textContent = esc.length ? "ESCALATED"
    : open.length ? "INCIDENT · " + String(open[0].phase).toUpperCase()
    : "HEALTHY";
}
function sparkline(svg, vals, color){
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  var ns = "http://www.w3.org/2000/svg";
  if (!vals.length) return;
  var w = 600, h = 64, max = Math.max.apply(null, vals.concat([1]));
  var pts = vals.map(function(v, i){
    var x = vals.length === 1 ? 0 : (i / (vals.length - 1)) * w;
    return x.toFixed(1) + "," + (h - 4 - (v / max) * (h - 10)).toFixed(1);
  }).join(" ");
  var pl = document.createElementNS(ns, "polyline");
  pl.setAttribute("points", pts);
  pl.setAttribute("fill", "none");
  pl.setAttribute("stroke", color);
  pl.setAttribute("stroke-width", "2");
  svg.appendChild(pl);
}
function renderProduction(sec, prod){
  sec.appendChild(T("h2", "Production health"));
  var traces = prod.traces || [];
  sec.appendChild(T("p", prod.n_traces + " traces total · showing last " + traces.length, "sub"));
  if (!traces.length) {
    sec.appendChild(T("p", "No production traces yet — run traffic to light this up.", "muted"));
    return;
  }
  var grid = T("div", "", "grid2");
  var left = T("div");
  var dots = T("div", "", "dots");
  var lastRel = null;
  traces.forEach(function(t){
    if (t.release && t.release !== lastRel && lastRel !== null) {
      dots.appendChild(T("span", String(t.release).slice(0, 7), "relmark"));
    }
    lastRel = t.release || lastRel;
    var d = document.createElement("span");
    d.className = "dot " + (t.failed ? "bad" : "ok");
    d.title = (t.trace_id || "").slice(0, 8) + " · " + (t.release || "").slice(0, 7)
      + " · " + t.tool_calls + " calls · " + t.tokens + " tok";
    dots.appendChild(d);
  });
  left.appendChild(dots);
  left.appendChild(T("p", "tool calls / trace", "chartlbl"));
  var c1 = document.createElementNS ? document.createElement("div") : null;
  var svg1 = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg1.setAttribute("class", "chart");
  svg1.setAttribute("viewBox", "0 0 600 64");
  svg1.setAttribute("preserveAspectRatio", "none");
  left.appendChild(svg1);
  left.appendChild(T("p", "tokens / trace", "chartlbl"));
  var svg2 = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg2.setAttribute("class", "chart");
  svg2.setAttribute("viewBox", "0 0 600 64");
  svg2.setAttribute("preserveAspectRatio", "none");
  left.appendChild(svg2);
  sparkline(svg1, traces.map(function(t){ return t.tool_calls || 0; }), "#58a6ff");
  sparkline(svg2, traces.map(function(t){ return t.tokens || 0; }), "#bc8cff");
  if (c1) void c1;
  grid.appendChild(left);
  var right = T("div");
  var tbl = document.createElement("table");
  var head = document.createElement("tr");
  ["metric", "baseline", "current"].forEach(function(h){ head.appendChild(T("th", h)); });
  tbl.appendChild(head);
  var base = prod.baseline || {};
  var cur = prod.current || {};
  [["failure_rate", fmtPct], ["tool_error_rate", fmtPct],
   ["tokens_per_run", function(x){ return fmtNum(x, 0); }],
   ["tool_calls_per_run", function(x){ return fmtNum(x, 2); }]].forEach(function(row){
    var tr = document.createElement("tr");
    tr.appendChild(T("td", row[0]));
    tr.appendChild(T("td", row[1](base[row[0]]), "mono"));
    tr.appendChild(T("td", row[1](cur[row[0]]), "mono"));
    tbl.appendChild(tr);
  });
  right.appendChild(tbl);
  var can = T("p", "", "muted");
  if (prod.canary && prod.canary.pass_rate !== undefined) {
    can.appendChild(T("span", "latest canary: " + fmtPct(prod.canary.pass_rate)
      + " (" + (prod.canary.n || "?") + " runs, " + fmtTime(prod.canary.at) + ")"));
  } else {
    can.appendChild(T("span", "no canary run yet"));
  }
  right.appendChild(can);
  grid.appendChild(right);
  sec.appendChild(grid);
}
function phaseTimes(inc){
  var m = {};
  (inc.timeline || []).forEach(function(e){
    if (e && e.phase && !(e.phase in m)) m[e.phase] = e.t;
  });
  return m;
}
var RESOLVED_PHASES = ["open", "triaged", "investigated", "decided", "remedied", "deployed", "resolved"];
function renderIncident(sec, inc){
  sec.appendChild(T("h2", "Current incident · " + (inc.id || "?")));
  sec.appendChild(T("p", "opened " + fmtTime(inc.opened_at) + " · release "
    + String(inc.release || "").slice(0, 8), "sub"));
  var steps = inc.phase === "escalated"
    ? ["open", "triaged", "investigated", "decided", "escalated"] : RESOLVED_PHASES;
  var times = phaseTimes(inc);
  var order = {};
  steps.forEach(function(p, i){ order[p] = i; });
  var curIdx = order[inc.phase] === undefined ? steps.length - 1 : order[inc.phase];
  var bar = T("div", "", "stepper");
  steps.forEach(function(p, i){
    var s = T("div", "", "step"
      + (i < curIdx ? " done" : i === curIdx ? (p === "escalated" ? " esc" : " now") : "")
      + (p === "escalated" && i === curIdx ? " esc" : ""));
    s.appendChild(T("div", p));
    if (times[p]) s.appendChild(T("span", fmtClock(times[p]), "t"));
    bar.appendChild(s);
  });
  sec.appendChild(bar);
  var cards = T("div", "", "cards");
  if ((inc.signals || []).length) {
    var c = T("div", "", "card");
    c.appendChild(T("h3", "Signals"));
    inc.signals.forEach(function(s){
      var p = T("p", "", "mono");
      p.appendChild(T("span", (s.name || s.metric || "?") + ": "));
      p.appendChild(T("span", fmtPct(s.baseline) + " → " + fmtPct(s.current)));
      c.appendChild(p);
    });
    cards.appendChild(c);
  }
  if (inc.triage && (inc.triage.summary || inc.triage.symptoms)) {
    var ct = T("div", "", "card");
    ct.appendChild(T("h3", "Triage"));
    if (inc.triage.summary) ct.appendChild(T("p", inc.triage.summary));
    if (inc.triage.symptoms) ct.appendChild(T("p", "Symptoms: " + inc.triage.symptoms, "muted"));
    cards.appendChild(ct);
  }
  Object.keys(inc.findings || {}).forEach(function(name){
    var f = inc.findings[name] || {};
    var cf = T("div", "", "card");
    cf.appendChild(T("h3", name));
    var v = String(f.verdict || "?");
    var good = /healthy|no_code_change|no_loop|no_recent/.test(v);
    var b = T("span", v, "badge " + (good ? "g" : "a"));
    cf.appendChild(b);
    if (f.confidence !== undefined) cf.appendChild(T("span", Math.round(f.confidence * 100) + "%", "badge b"));
    if (f.detail) cf.appendChild(T("p", f.detail));
    cards.appendChild(cf);
  });
  var dec = inc.decision || {};
  if (dec.cause) {
    var cd = T("div", "", "card");
    cd.appendChild(T("h3", "Coordinator decision"));
    cd.appendChild(T("span", dec.cause, "badge p"));
    if (dec.remedy) cd.appendChild(T("span", String(dec.remedy), "badge b"));
    if (dec.confidence !== undefined) cd.appendChild(T("span", Math.round(dec.confidence * 100) + "%", "badge b"));
    (dec.rationale || []).forEach(function(r){
      var li = T("p", "• " + r);
      cd.appendChild(li);
    });
    (dec.disagreements || []).forEach(function(d){
      cd.appendChild(T("p", "Settled: " + d, "muted"));
    });
    cards.appendChild(cd);
  }
  ((inc.remedy || {}).attempts || []).forEach(function(a){
    var ca = T("div", "", "card");
    ca.appendChild(T("h3", "Fix attempt " + (a.n !== undefined ? a.n : "")));
    ca.appendChild(T("span", a.accepted ? "accepted" : "rejected",
      "badge " + (a.accepted ? "g" : "r")));
    if (a.commit_title) ca.appendChild(T("p", a.commit_title, "mono"));
    if (a.diagnosis) ca.appendChild(T("p", a.diagnosis));
    (a.edits || []).forEach(function(e){
      var d = document.createElement("div");
      d.className = "diff";
      d.appendChild(T("div", e.path || "edit", "path"));
      (String(e.find || "").split("\\n")).forEach(function(line){
        var pre = T("pre", line, "del"); d.appendChild(pre);
      });
      (String(e.replace || "").split("\\n")).forEach(function(line){
        var pre = T("pre", line, "add"); d.appendChild(pre);
      });
      ca.appendChild(d);
    });
    if (a.feedback) ca.appendChild(T("p", "Feedback: " + a.feedback, "muted"));
    cards.appendChild(ca);
  });
  if (inc.deploy && (inc.deploy.release || inc.deploy.deployed !== undefined)) {
    var cdp = T("div", "", "card");
    cdp.appendChild(T("h3", "Deploy"));
    cdp.appendChild(T("p", (inc.deploy.deployed ? "live" : "not deployed")
      + " · " + String(inc.deploy.release || "").slice(0, 8)
      + (inc.deploy.at ? " · " + fmtTime(inc.deploy.at) : ""), "mono"));
    cards.appendChild(cdp);
  }
  if (inc.monitor && (inc.monitor.window || (inc.monitor.signals || []).length)) {
    var cm = T("div", "", "card");
    cm.appendChild(T("h3", "Monitor"));
    if (inc.monitor.window) cm.appendChild(T("p", JSON.stringify(inc.monitor.window), "mono"));
    (inc.monitor.signals || []).forEach(function(s){
      cm.appendChild(T("p", (s.name || s.metric || "?") + ": " + fmtPct(s.current), "mono"));
    });
    cards.appendChild(cm);
  }
  sec.appendChild(cards);
}
function renderHistory(sec, incidents){
  sec.appendChild(T("h2", "Incident history"));
  if (!incidents.length) {
    sec.appendChild(T("p", "No incidents yet.", "muted"));
    return;
  }
  var tbl = document.createElement("table");
  var head = document.createElement("tr");
  ["id", "opened", "cause", "outcome", "resolve time", "fixes"].forEach(function(h){
    head.appendChild(T("th", h));
  });
  tbl.appendChild(head);
  incidents.forEach(function(i){
    var tr = document.createElement("tr");
    tr.appendChild(T("td", i.id || "?", "mono"));
    tr.appendChild(T("td", fmtTime(i.opened_at)));
    tr.appendChild(T("td", (i.decision || {}).cause || "—"));
    var out = i.phase || "—";
    var td = T("td", out, "badge " + (out === "resolved" || out === "done" ? "g"
      : out === "escalated" ? "r" : "a"));
    tr.appendChild(td);
    var dt = "—";
    try {
      var tl = i.timeline || [];
      if (tl.length && i.opened_at) {
        var s = Math.round(tl[tl.length - 1].t - i.opened_at);
        dt = Math.floor(s / 60) + "m " + (s % 60) + "s";
      }
    } catch (e) {}
    tr.appendChild(T("td", dt, "mono"));
    tr.appendChild(T("td", String((((i.remedy || {}).attempts) || []).length), "mono"));
    tbl.appendChild(tr);
  });
  sec.appendChild(tbl);
}
function renderBench(sec, bench){
  sec.appendChild(T("h2", "Benchmark"));
  if (!bench.length) {
    sec.appendChild(T("p", "No benchmark runs yet.", "muted"));
    return;
  }
  var passed = bench.filter(function(r){ return r.passed; }).length;
  sec.appendChild(T("p", passed + "/" + bench.length + " passed", "sub"));
  var tbl = document.createElement("table");
  var head = document.createElement("tr");
  ["scenario", "outcome", "cause", "remedy", "culprit", "detect", "resolve", "pass"]
    .forEach(function(h){ head.appendChild(T("th", h)); });
  tbl.appendChild(head);
  bench.forEach(function(r){
    var tr = document.createElement("tr");
    tr.appendChild(T("td", r.scenario || r.name || "?"));
    tr.appendChild(T("td", r.outcome || "—"));
    tr.appendChild(T("td", r.cause || "—", "mono"));
    tr.appendChild(T("td", r.remedy || "—", "mono"));
    var cul = "—";
    if (r.culprit_expected || r.culprit_found) {
      cul = (r.culprit_expected === r.culprit_found) ? "✓" : "✗";
      if (r.culprit_found) cul += " " + String(r.culprit_found).slice(0, 7);
    }
    tr.appendChild(T("td", cul, "mono"));
    tr.appendChild(T("td", r.detect_s !== undefined ? Math.round(r.detect_s) + "s" : "—", "mono"));
    tr.appendChild(T("td", r.resolve_s !== undefined ? Math.round(r.resolve_s) + "s" : "—", "mono"));
    tr.appendChild(T("td", r.passed ? "✓" : "✗",
      "badge " + (r.passed ? "g" : "r")));
    tbl.appendChild(tr);
  });
  sec.appendChild(tbl);
}
function trailUrl(proposal){
  if (!proposal) return null;
  var m = String(proposal.trail || proposal.url || "").match(/https?:\\/\\/\\S+/);
  return m ? m[0].replace(/[).,;]+$/, "") : null;
}
function renderExternal(sec, cases){
  sec.appendChild(T("h2", "Real-world case"));
  if (!cases.length) {
    sec.appendChild(T("p", "No external cases yet.", "muted"));
    return;
  }
  cases.forEach(function(c){
    var box = T("div", "", "card");
    box.appendChild(T("h3", (c.target || c.id || "case") + " · " + (c.phase || "")));
    var dec = c.decision || {};
    if (dec.cause) box.appendChild(T("span", dec.cause, "badge p"));
    (dec.rationale || []).forEach(function(r){ box.appendChild(T("p", "• " + r)); });
    (c.attempts || []).forEach(function(a){
      var line = T("p", "attempt " + (a.n !== undefined ? a.n : "?") + ": "
        + (a.accepted ? "accepted" : a.blocked ? "blocked" : "rejected")
        + (a.feedback ? " — " + a.feedback : ""), "muted");
      box.appendChild(line);
      if (a.review) box.appendChild(T("p", "Review: " + JSON.stringify(a.review), "muted"));
      (a.edits || []).forEach(function(e){
        var d = document.createElement("div");
        d.className = "diff";
        d.appendChild(T("div", e.path || "edit", "path"));
        (String(e.find || "").split("\\n")).forEach(function(ln){
          d.appendChild(T("pre", ln, "del"));
        });
        (String(e.replace || "").split("\\n")).forEach(function(ln){
          d.appendChild(T("pre", ln, "add"));
        });
        box.appendChild(d);
      });
      (a.verification || []).forEach(function(v){
        box.appendChild(T("p", "verify " + (v.topic || "") + ": exit "
          + v.exit + " · " + (v.trace_id || "").slice(0, 8) + " · "
          + (v.trace_status || ""), "mono"));
      });
    });
    var url = trailUrl(c.proposal);
    if (url) {
      var p = T("p");
      var a = document.createElement("a");
      a.href = url; a.textContent = "Entire Trail";
      a.target = "_blank"; a.rel = "noopener";
      p.appendChild(a);
      box.appendChild(p);
    }
    sec.appendChild(box);
  });
}
function render(data){
  renderStatus(data);
  var root = document.getElementById("root");
  while (root.firstChild) root.removeChild(root.firstChild);
  var prod = data.production || {};
  var incidents = data.incidents || [];
  var bench = data.bench || [];
  var external = data.external || [];
  if (!prod.n_traces && !incidents.length && !bench.length && !external.length) {
    var e = T("div", "", "empty");
    e.appendChild(T("div", "No Medic data yet", "big"));
    e.appendChild(T("p", "Point --medic-dir at a run (or run traffic / bench) and this page lights up."));
    root.appendChild(e);
    return;
  }
  var s1 = document.createElement("section");
  renderProduction(s1, prod);
  root.appendChild(s1);
  if (incidents.length) {
    var open = openIncidents(incidents);
    var cur = open[0] || incidents[0];
    if (lastPhase !== null && lastPhase !== cur.phase) {
      document.getElementById("pill").animate(
        [{transform: "scale(1)"}, {transform: "scale(1.12)"}, {transform: "scale(1)"}],
        {duration: 450});
    }
    lastPhase = cur.phase;
    var s2 = document.createElement("section");
    renderIncident(s2, cur);
    root.appendChild(s2);
    var s3 = document.createElement("section");
    renderHistory(s3, incidents);
    root.appendChild(s3);
  }
  var s4 = document.createElement("section");
  renderBench(s4, bench);
  root.appendChild(s4);
  if (external.length) {
    var s5 = document.createElement("section");
    renderExternal(s5, external);
    root.appendChild(s5);
  }
}
function poll(){
  fetch("/api/state", {cache: "no-store"}).then(function(r){ return r.json(); })
    .then(render)
    .catch(function(err){
      var root = document.getElementById("root");
      if (!root.firstChild || !root.querySelector("section")) {
        while (root.firstChild) root.removeChild(root.firstChild);
        var e = T("div", "", "empty");
        e.appendChild(T("div", "Dashboard unreachable", "big"));
        e.appendChild(T("p", String(err)));
        root.appendChild(e);
      }
    });
}
poll();
setInterval(poll, 3000);
</script>
</body>
</html>
"""


def make_handler(medic_dir: Path):
    root = Path(medic_dir)
    page = PAGE.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentMedicDashboard/1.0"

        def log_message(self, fmt, *args):  # quieter than the default stderr chatter
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/api/state":
                body = json.dumps(build_state(root), default=str).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif path == "/":
                self._send(200, page, "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

    return Handler


def serve(medic_dir: Path, port: int) -> int:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(Path(medic_dir)))
    print(f"Agent Medic dashboard at http://127.0.0.1:{server.server_port}/  (medic-dir {medic_dir})",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None) -> int:
    from medic import paths
    parser = argparse.ArgumentParser(prog="medic.dashboard")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--medic-dir", type=Path, default=paths.MEDIC_DIR)
    args = parser.parse_args(argv)
    return serve(args.medic_dir, args.port)


if __name__ == "__main__":
    import sys
    sys.exit(main())
