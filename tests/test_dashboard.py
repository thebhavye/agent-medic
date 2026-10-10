"""Dashboard /api/state shape: one fake incident plus one fake bench result."""

import json
import shutil
import subprocess
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from medic.dashboard import PAGE, build_state, make_handler


def write_state(root: Path):
    (root / "incidents").mkdir(parents=True, exist_ok=True)
    (root / "bench" / "api_change-test").mkdir(parents=True, exist_ok=True)
    (root / "external").mkdir(parents=True, exist_ok=True)
    state = {
        "traces": [
            {"trace_id": "t1", "started_at": "2026-10-10T00:00:00Z", "release": "aaa1111",
             "status": "OK", "cost_usd": 0.0, "tokens": 100, "tool_calls": 3,
             "answer": "It is in Narvik.", "tool_errors": [], "repeated_calls": 0},
            {"trace_id": "t2", "started_at": "2026-10-10T00:01:00Z", "release": "aaa1111",
             "status": "ERROR", "cost_usd": 0.0, "tokens": 50, "tool_calls": 1,
             "answer": "I could not find it.", "tool_errors": [{"tool": "search_docs"}],
             "repeated_calls": 0},
        ],
        "baseline": {"n": 1, "failure_rate": 0.0, "tool_error_rate": 0.0,
                     "tokens_per_run": 100.0, "tool_calls_per_run": 3.0},
        "healthy_release": "aaa1111",
        "canary": [{"at": "2026-10-10T00:02:00Z", "release": "aaa1111",
                    "pass_rate": 0.5, "n": 2, "failed": ["q2"]}],
    }
    (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
    incident = {
        "id": "1010-000000-abcd", "opened_at": 1791606500.0, "phase": "decided",
        "signals": [{"name": "failures", "metric": "failure_rate", "baseline": 0.0, "current": 1.0}],
        "release": "aaa1111", "healthy_release": "aaa1111", "trace_ids": ["t2"],
        "triage": {"summary": "search fails", "symptoms": "errors", "dominant": "dep",
                   "investigators": [], "examples": []},
        "findings": {"dependency": {"verdict": "contract_changed", "confidence": 0.9,
                                    "detail": "schema v2"}},
        "decision": {"cause": "dependency_contract", "remedy": "adapt_client",
                     "confidence": 0.9, "rationale": ["api changed"], "disagreements": []},
        "remedy": {"attempts": [], "branch": "", "commit": ""},
        "deploy": {"deployed": False, "release": "", "at": None},
        "monitor": {"window": {}, "signals": []},
        "timeline": [{"t": 1791606500.0, "phase": "open", "msg": "opened"}],
    }
    (root / "incidents" / "1010-000000-abcd.json").write_text(json.dumps(incident), encoding="utf-8")
    bench = {"scenario": "api_change", "provider": "test", "outcome": "resolved",
             "cause": "dependency_contract", "remedy": "adapt_client",
             "culprit_expected": "", "culprit_found": "", "attempts": 1,
             "detect_s": 5, "resolve_s": 40, "passed": True}
    (root / "bench" / "api_change-test" / "result.json").write_text(json.dumps(bench), encoding="utf-8")


def test_build_state_shape(tmp_path):
    write_state(tmp_path)
    # A half-written file must be skipped, not crash the endpoint.
    (tmp_path / "incidents" / "half.json").write_text("{broken", encoding="utf-8")
    data = build_state(tmp_path)
    assert set(data) == {"production", "incidents", "deploy", "bench", "external"}
    prod = data["production"]
    assert prod["n_traces"] == 2 and len(prod["traces"]) == 2
    assert prod["current"]["n"] == 2
    assert prod["baseline"]["failure_rate"] == 0.0
    assert prod["canary"]["pass_rate"] == 0.5
    assert [t["failed"] for t in prod["traces"]] == [False, True]
    assert len(data["incidents"]) == 1
    assert data["incidents"][0]["decision"]["cause"] == "dependency_contract"
    assert len(data["bench"]) == 1
    assert data["bench"][0]["scenario"] == "api_change"
    assert data["bench"][0]["name"] == "api_change-test"
    assert data["deploy"] is None  # no deploy repo in the fixture


def test_empty_medic_dir(tmp_path):
    data = build_state(tmp_path)
    assert data["production"]["n_traces"] == 0
    assert data["production"]["traces"] == []
    assert data["incidents"] == [] and data["bench"] == [] and data["external"] == []
    assert data["deploy"] is None


def test_http_endpoints(tmp_path):
    write_state(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tmp_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(base + "/api/state") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
        assert data["production"]["n_traces"] == 2
        assert data["incidents"][0]["id"] == "1010-000000-abcd"
        with urllib.request.urlopen(base + "/") as res:
            html = res.read().decode("utf-8")
        assert "Agent Medic" in html and "/api/state" in html
    finally:
        server.shutdown()
        thread.join()


def test_incident_shapes_pass_through(tmp_path):
    write_state(tmp_path)
    incident = json.loads((tmp_path / "incidents" / "1010-000000-abcd.json").read_text(encoding="utf-8"))
    incident["triage"]["symptoms"] = [{"symptom": ["tool_error", "search_docs", "TypeError"], "traces": 6}]
    incident["signals"].append({"name": "tool_calls", "metric": "tool_calls_per_run",
                                "baseline": 3.0, "current": 5.6666667})
    (tmp_path / "incidents" / "1010-000000-abcd.json").write_text(json.dumps(incident), encoding="utf-8")
    older = dict(incident, id="1009-000000-old", opened_at=1791500000.0, phase="escalated")
    (tmp_path / "incidents" / "1009-000000-old.json").write_text(json.dumps(older), encoding="utf-8")
    data = build_state(tmp_path)
    # Newest first: the resolved incident is current despite the older escalation.
    assert [i["id"] for i in data["incidents"]] == ["1010-000000-abcd", "1009-000000-old"]
    current = data["incidents"][0]
    assert current["triage"]["symptoms"] == [
        {"symptom": ["tool_error", "search_docs", "TypeError"], "traces": 6}]
    assert current["signals"][-1] == {"name": "tool_calls", "metric": "tool_calls_per_run",
                                      "baseline": 3.0, "current": 5.6666667}


JS_PRELUDE = r"""
const assert = require("assert");
let __fetchImpl = () => Promise.reject(new Error("boot"));
function fetch() { return __fetchImpl(); }
function setInterval() { return 0; }
class FakeEl {
  constructor(tag) { this.tag = tag; this.children = []; this.textContent = "";
    this.className = ""; this.hidden = false; this.attrs = {}; }
  appendChild(c) { this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter(function(x){ return x !== c; }); }
  get firstChild() { return this.children[0] || null; }
  setAttribute(k, v) { this.attrs[k] = v; }
  querySelector(sel) {
    if (this.tag === sel) return this;
    for (const c of this.children) {
      const f = c.querySelector ? c.querySelector(sel) : null;
      if (f) return f;
    }
    return null;
  }
  animate() {}
}
const __ids = {};
function __el(id) {
  if (!__ids[id]) { __ids[id] = new FakeEl("div"); __ids[id].id = id; }
  return __ids[id];
}
const document = {
  createElement: function(t) { return new FakeEl(t); },
  createElementNS: function(ns, t) { return new FakeEl(t); },
  getElementById: function(id) { return __el(id); },
};
"""

JS_DRIVER = r"""
(async () => {
const tick = () => new Promise(function(r){ setTimeout(r, 20); });
assert.strictEqual(fmtMetric("failure_rate", 0.005), "0.5%");
assert.strictEqual(fmtMetric("tool_error_rate", 0.3333333), "33.3%");
assert.strictEqual(fmtMetric("tool_calls_per_run", 5.6666667), "5.67");
assert.strictEqual(fmtMetric("tokens_per_run", 2618), "2618");
assert.strictEqual(fmtMetric("tokens_per_run", 1605.6666667), "1605.67");
assert.strictEqual(fmtMetric("canary_pass_rate", 0.75), "75.0%");
assert.strictEqual(fmtMetric("cost_per_run", 0.006), "0.01");
assert.strictEqual(fmtMetric("tool_calls_per_run", null), "—");
assert.strictEqual(fmtMetric("failure_rate", undefined), "—");
assert.strictEqual(symptomText({symptom: ["tool_error", "search_docs", "TypeError"], traces: 3}),
  "tool_error · search_docs · TypeError (3 traces)");
assert.strictEqual(symptomText({symptom: ["healthy"], traces: 1}), "healthy (1 trace)");
assert.strictEqual(symptomText("legacy string"), "legacy string");
assert.strictEqual(symptomText(null), "");
assert.strictEqual(symptomText({}), "");
assert.strictEqual(symptomText({symptom: ["a"]}), "a");
assert.strictEqual(symptomText(42), "42");
assert.strictEqual(symptomText({symptom: "odd", traces: null}), "odd");
const esc = {id: "old", opened_at: 100, phase: "escalated"};
const res = {id: "new", opened_at: 200, phase: "resolved"};
const opn = {id: "o", opened_at: 150, phase: "decided"};
assert.deepStrictEqual(statusFor([]), {cls: "ok", label: "HEALTHY"});
assert.deepStrictEqual(statusFor([res, esc]), {cls: "ok", label: "HEALTHY"});
assert.deepStrictEqual(statusFor([esc]), {cls: "bad", label: "ESCALATED"});
assert.deepStrictEqual(statusFor([opn]), {cls: "warn", label: "INCIDENT · DECIDED"});
assert.deepStrictEqual(statusFor([res, opn]), {cls: "warn", label: "INCIDENT · DECIDED"});
const early = {phase: "escalated", timeline: [
  {t: 1, phase: "open"}, {t: 2, phase: "triaged"}, {t: 3, phase: "investigated"},
  {t: 4, phase: "decided"}, {t: 5, phase: "escalated"}]};
assert.deepStrictEqual(stepsFor(early),
  ["open", "triaged", "investigated", "decided", "escalated"]);
const late = {phase: "escalated",
  remedy: {attempts: [{n: 1, accepted: true}]},
  timeline: [
  {t: 1, phase: "open"}, {t: 2, phase: "triaged"}, {t: 3, phase: "investigated"},
  {t: 4, phase: "decided"}, {t: 5, phase: "remedy", msg: "attempt 1: accepted: verified"},
  {t: 6, phase: "deployed"}, {t: 7, phase: "escalated"}]};
assert.deepStrictEqual(stepsFor(late),
  ["open", "triaged", "investigated", "decided", "remedied", "deployed", "escalated"]);
assert.deepStrictEqual(stepsFor({phase: "resolved", timeline: []}),
  ["open", "triaged", "investigated", "decided", "remedied", "deployed", "resolved"]);
assert.strictEqual(phaseTimes(late)["remedied"], 5);
assert.strictEqual(phaseTimes(late)["escalated"], 7);
const allFailed = {phase: "escalated",
  remedy: {attempts: [{n: 1, accepted: false}, {n: 2, accepted: false}]},
  timeline: [
  {t: 1, phase: "open"}, {t: 2, phase: "triaged"}, {t: 3, phase: "investigated"},
  {t: 4, phase: "decided"},
  {t: 5, phase: "remedy", msg: "attempt 1: rejected: no usable proposal"},
  {t: 6, phase: "remedy", msg: "attempt 2: rejected: still broken"},
  {t: 7, phase: "escalated", msg: "no verified fix after 2 attempts"}]};
assert.deepStrictEqual(stepsFor(allFailed),
  ["open", "triaged", "investigated", "decided", "escalated"]);
assert.strictEqual(phaseTimes(allFailed)["remedied"], undefined);
const retry = {phase: "escalated",
  remedy: {attempts: [{n: 1, accepted: false}, {n: 2, accepted: true}]},
  timeline: [
  {t: 1, phase: "open"}, {t: 2, phase: "triaged"}, {t: 3, phase: "investigated"},
  {t: 4, phase: "decided"},
  {t: 5, phase: "remedy", msg: "attempt 1: rejected: still broken"},
  {t: 8, phase: "remedy", msg: "attempt 2: accepted: verified"},
  {t: 9, phase: "deployed", msg: "release abc is live"},
  {t: 10, phase: "escalated", msg: "monitor failed"}]};
assert.deepStrictEqual(stepsFor(retry),
  ["open", "triaged", "investigated", "decided", "remedied", "deployed", "escalated"]);
assert.strictEqual(phaseTimes(retry)["remedied"], 8);
const root = document.getElementById("root");
const stale = document.getElementById("stale");
const INC = {id: "i1", opened_at: 1791606500, phase: "resolved", release: "aaa",
  signals: [], triage: {summary: "s", symptoms: [{symptom: ["tool_error", "x"], traces: 2}]},
  findings: {}, decision: {}, remedy: {attempts: []}, deploy: {}, monitor: {},
  timeline: [{t: 1791606500, phase: "open", msg: "o"}]};
const GOOD = {production: {n_traces: 0, traces: [], baseline: null, current: {n: 0}, canary: null},
  incidents: [INC], deploy: null, bench: [], external: []};
__fetchImpl = () => Promise.resolve({json: () => Promise.resolve(GOOD)});
poll(); await tick(); await tick();
assert.ok(root.querySelector("section"), "sections render after success");
assert.strictEqual(stale.hidden, true);
__fetchImpl = () => Promise.reject(new Error("down"));
poll(); await tick(); await tick();
assert.ok(root.querySelector("section"), "data kept after failure");
assert.strictEqual(stale.hidden, false);
assert.ok(/STALE/.test(stale.textContent));
poll(); await tick(); await tick();
assert.strictEqual(stale.hidden, false, "still stale, single banner node");
__fetchImpl = () => Promise.resolve({json: () => Promise.resolve(GOOD)});
poll(); await tick(); await tick();
assert.strictEqual(stale.hidden, true, "banner clears on recovery");
console.log("JS-OK");
})().catch(function(e) { console.error("JS-FAIL: " + (e && e.stack || e)); process.exit(1); });
"""


def test_js_rendering_logic(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available for dashboard JS tests")
    script = PAGE[PAGE.index("<script>") + len("<script>"):PAGE.index("</script>")]
    harness = tmp_path / "dash.js"
    harness.write_text(JS_PRELUDE + script + JS_DRIVER, encoding="utf-8")
    proc = subprocess.run([node, str(harness)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "JS-OK" in proc.stdout
