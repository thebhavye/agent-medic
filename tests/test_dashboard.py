"""Dashboard /api/state shape: one fake incident plus one fake bench result."""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from medic.dashboard import build_state, make_handler


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
