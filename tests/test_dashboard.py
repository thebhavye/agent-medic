import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from medic import dashboard


def incident(**over):
    inc = {
        "id": "inc1", "opened_at": 1000.0, "phase": "resolved", "release": "b" * 40, "healthy_release": "a" * 40,
        "signals": [{"name": "quality", "metric": "canary_pass_rate", "baseline": 1.0, "current": 0.0}],
        "triage": {"summary": "8/8 known-answer questions wrong", "examples": [{"question": "q?", "answer": "wrong",
                                                                               "expect": [["Narvik"]]}]},
        "findings": {
            "localize": {"verdict": "output_changed", "confidence": 0.56, "detail": "read_doc shorter",
                         "divergences": [{"kind": "output_changed", "step": "TOOL read_doc",
                                          "numbers": {"median_now": 302, "median_before": 515}, "traces": ["t"]}],
                         "healthy_traces": 9, "failing_traces": 6, "suspects": []},
            "regression": {"verdict": "culprit", "confidence": 0.9, "culprit": "c" * 40, "culprit_subject": "Cap read_doc",
                           "verdicts": {"a" * 40: "good", "c" * 40: "bad"}, "changed_surfaces": []},
            "dependency": {"verdict": "healthy", "confidence": 0.7, "detail": "API unchanged"},
        },
        "decision": {"cause": "code_regression", "remedy": "code_fix", "confidence": 0.95, "culprit": "c" * 40},
        "remedy": {"attempts": [{"n": 1, "accepted": True, "commit": "f" * 40, "commit_title": "Return full document",
                                 "edits": [{"path": "tools.py", "find": "x = 1\nreturn d[:300]", "replace": "x = 1\nreturn d"}],
                                 "tournament": [
                                     {"candidate": 1, "verdict": "rejected_in_review", "feedback": "bypasses read_doc()",
                                      "target": {"kind": "tool_handler", "name": "read_doc", "file": "tools.py", "start": 54}},
                                     {"candidate": 0, "verdict": "passed", "winner": True, "at_root_cause": True,
                                      "changed_lines": 4, "metrics": {"n": 12, "pass_rate": 1.0},
                                      "target": {"kind": "tool_handler", "name": "read_doc", "file": "tools.py", "start": 47}}]}]},
        "deploy": {"canary": {"candidate": "f" * 40, "stable": "b" * 40, "share": 0.5}, "deployed": True, "release": "f" * 40,
                   "canary_verdict": {"verdict": "healthy", "window": {"n": 6, "failure_rate": 0.0}, "detail": "6 canary traces"}},
        "timeline": [{"t": 1000.0, "phase": "open", "msg": "opened"}, {"t": 1076.0, "phase": "triaged", "msg": "t"},
                     {"t": 1186.0, "phase": "decided", "msg": "d"}, {"t": 1219.0, "phase": "remedy", "msg": "r"},
                     {"t": 1221.0, "phase": "canary", "msg": "candidate serves 50%; canary traces decide promote or roll back"},
                     {"t": 1456.0, "phase": "deployed", "msg": "live"}, {"t": 1594.0, "phase": "resolved", "msg": "healed"}],
    }
    inc.update(over)
    return inc


def medic_dir(tmp_path, incidents=(), rollout=None):
    (tmp_path / "incidents").mkdir()
    for inc in incidents:
        (tmp_path / "incidents" / f"{inc['id']}.json").write_text(json.dumps(inc), encoding="utf-8")
    traces = [{"trace_id": f"t{i}", "started_at": f"2026-10-10T11:0{i}:00.000Z", "release": ("f" * 12 if i > 2 else "b" * 12),
               "status": "OK", "answer": "Narvik" if i != 1 else "I could not find that", "question": "q"} for i in range(5)]
    (tmp_path / "state.json").write_text(json.dumps({"traces": traces, "baseline": {"n": 6, "failure_rate": 0.0},
                                                     "canary": [{"at": 1.0, "release": "b" * 12, "pass_rate": 0.0, "n": 8}]}),
                                         encoding="utf-8")
    if rollout:
        (tmp_path / "rollout.json").write_text(json.dumps(rollout), encoding="utf-8")
    return tmp_path


def test_diff_lines_marks_changes_and_folds_long_context():
    rows = dashboard.diff_lines("a\nb\nc\nd\ne\nf\nold", "a\nb\nc\nd\ne\nf\nnew")
    assert ["-", "old"] in rows and ["+", "new"] in rows
    assert any(op == "~" for op, _ in rows)            # 6 unchanged lines folded down


def test_incident_view_tells_the_whole_story(tmp_path):
    state = dashboard.build_state(medic_dir(tmp_path, [incident()]))
    inc = state["incidents"][0]
    assert [p["phase"] for p in inc["phases"]] == ["open", "triaged", "decided", "remedied", "canary", "deployed", "resolved"]
    assert inc["ended_at"] == 1594.0
    assert [i["name"] for i in inc["investigators"]] == ["dependency", "regression", "localize"]
    loc = inc["investigators"][2]
    assert loc["divergences"][0]["numbers"]["median_before"] == 515
    cands = inc["attempts"][0]["candidates"]
    assert cands[0]["winner"] and cands[0]["root_cause"] and cands[0]["target"] == "tool_handler read_doc · tools.py:47"
    assert cands[1]["verdict"] == "rejected_in_review" and "bypasses" in cands[1]["reason"]
    assert ["+", "return d"] in inc["attempts"][0]["edits"][0]["lines"]
    assert inc["canary"]["deployed"] and inc["canary"]["rollout"]["share"] == 0.5
    assert state["releases"]["c" * 12]["role"] == "culprit" and state["releases"]["f" * 12]["role"] == "fix"


def test_escalated_incident_ends_on_escalated(tmp_path):
    esc = incident(id="inc2", phase="escalated", remedy={}, deploy={},
                   timeline=[{"t": 1000.0, "phase": "open", "msg": "o"}, {"t": 1100.0, "phase": "escalated", "msg": "outage"}])
    inc = dashboard.build_state(medic_dir(tmp_path, [esc]))["incidents"][0]
    assert [p["phase"] for p in inc["phases"]] == ["open", "escalated"] and inc["ended_at"] == 1100.0


def test_traces_use_the_watcher_failure_rule_and_live_rollout_counts_its_traces(tmp_path):
    state = dashboard.build_state(medic_dir(tmp_path, rollout={"incident": "inc1", "candidate": "f" * 40, "share": 0.5}))
    traces = state["production"]["traces"]
    assert [t["failed"] for t in traces] == [False, True, False, False, False]
    assert traces[0]["at"] < traces[-1]["at"]
    assert state["rollout"]["canary_traces"] == 2 and state["rollout"]["canary_window"]["failure_rate"] == 0.0
    assert state["deploy"] is None                      # no deployment repo in this fixture


def test_empty_dir_does_not_crash(tmp_path):
    state = dashboard.build_state(tmp_path)
    assert state["incidents"] == [] and state["production"]["traces"] == [] and state["rollout"] is None


def test_http_serves_page_and_state(tmp_path):
    medic_dir(tmp_path, [incident()])
    handler = type("H", (dashboard.Handler,), {"medic_dir": tmp_path})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/") as r:
            assert "charset=utf-8" in r.headers["Content-Type"] and b"Agent Medic" in r.read()
        with urllib.request.urlopen(base + "/api/state") as r:
            assert json.loads(r.read())["incidents"][0]["id"] == "inc1"
        try:
            urllib.request.urlopen(base + "/nope")
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        server.shutdown()


def test_page_never_injects_data_as_html():
    page = dashboard.PAGE.read_text(encoding="utf-8")
    assert "innerHTML" not in page and "insertAdjacentHTML" not in page
