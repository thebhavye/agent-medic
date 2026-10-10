"""Medic on top of neatlogs: setup, the alert trigger, the MCP investigation and the write-back."""

import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from bisect_agent.neatlogs_api import BASE_URL
from medic import agent as agent_mod
from medic import coordinator, dashboard, neatlogs_link, paths
from medic.agent import Medic


class FakeNeatlogs:
    """Just enough of the public API: groups, detections, rules, webhooks, alert history, feedback."""

    def __init__(self):
        self.groups, self.detections, self.rules, self.webhooks = [], [], [], []
        self.history, self.feedback, self.reject_types = [], [], {"classifier"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        resp = self._handle(request)
        resp.headers["RateLimit-Remaining"] = "100"            # plenty of budget, as the real API reports
        return resp

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/api/v1/public", "")
        body = json.loads(request.content) if request.content else {}
        listing = {"/detection-groups": ("groups", self.groups), "/detections": ("detections", self.detections),
                   "/alert-rules": ("alertRules", self.rules), "/webhooks": ("webhooks", self.webhooks),
                   "/alert-history": ("alertHistory", self.history)}
        if request.method == "GET" and path in listing:
            key, items = listing[path]
            return httpx.Response(200, json={"success": True, "data": {key: items, "page": {"hasMore": False}}})
        if request.method == "POST" and path in listing:
            if path == "/detections" and body.get("type") in self.reject_types:
                return httpx.Response(403, json={"code": "entitlement_required"})
            item = {**body, "id": f"{path.strip('/')}-{len(listing[path][1]) + 1}"}
            listing[path][1].append(item)
            return httpx.Response(201, json={"success": True, "data": item})
        if request.method == "POST" and path.endswith("/feedback"):
            self.feedback.append((path.split("/")[2], body))
            return httpx.Response(201, json={"success": True, "data": {"id": "fb"}})
        return httpx.Response(404, json={"code": "not_found"})


@pytest.fixture
def nl(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIC_DIR", tmp_path)
    fake = FakeNeatlogs()
    api = neatlogs_link.Api()
    api._client = httpx.Client(transport=httpx.MockTransport(fake.handler), base_url=BASE_URL)
    return fake, api


def test_setup_creates_detections_rules_and_webhook_once(nl):
    fake, api = nl
    state = neatlogs_link.setup(api, baseline={"tokens_per_run": 1600}, webhook_url="https://medic.example/hooks/neatlogs",
                                log=lambda m: None)
    assert [g["name"] for g in fake.groups] == ["Agent Medic"]
    assert set(state["detections"]) == {"tool_failed", "no_answer"}          # the classifier needs a plan
    assert "refusal" in state["skipped"]
    assert {"tool_failed", "no_answer", "tool_failure_rate", "tokens"} <= set(state["rules"])
    tokens_rule = next(r for r in fake.rules if r.get("metricName") == "tokens_total")
    assert tokens_rule["metricThresholdValue"] == 2400                          # 1.5x the healthy baseline
    assert all(r["channelMask"]["webhookIds"] == [state["webhook"]] for r in fake.rules)
    assert fake.webhooks[0]["secret"] and "alert.threshold_breach" in fake.webhooks[0]["eventTypes"]
    before = (len(fake.detections), len(fake.rules), len(fake.webhooks))
    neatlogs_link.setup(api, baseline={"tokens_per_run": 1600}, webhook_url="https://medic.example/hooks/neatlogs",
                        log=lambda m: None)
    assert (len(fake.detections), len(fake.rules), len(fake.webhooks)) == before   # idempotent


def iso(offset_s: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + offset_s))


def test_new_alerts_are_ours_unseen_and_not_followups(nl):
    fake, api = nl
    state = neatlogs_link.setup(api, log=lambda m: None)
    ours = state["rules"]["tool_failed"]
    fake.history[:] = [
        {"id": "a1", "ruleId": ours, "ruleName": "Medic: tool call failed (3 in 5 min)", "firedAt": iso(-30),
         "count": 4, "threshold": 3, "sampleTraceIds": ["t1", "t2"], "isFollowup": False},
        {"id": "a2", "ruleId": "someone-elses-rule", "firedAt": iso(-20)},
        {"id": "a3", "ruleId": ours, "firedAt": iso(-10), "isFollowup": True},
    ]
    assert [a["id"] for a in neatlogs_link.new_alerts(api)] == ["a1"]
    assert neatlogs_link.new_alerts(api) == []                                # seen


def test_stale_alerts_never_open_an_incident(nl):
    # The live run-2 bug: alerts from an earlier run, never consumed, opened an incident before
    # any fault. Alerts from before Medic started, or older than a few minutes, are only marked seen.
    fake, api = nl
    state = neatlogs_link.setup(api, log=lambda m: None)
    ours = state["rules"]["tool_failed"]
    started = time.time() - 60
    fake.history[:] = [{"id": "old-run", "ruleId": ours, "firedAt": iso(-300)},          # before this Medic
                       {"id": "ancient", "ruleId": ours, "firedAt": iso(-3600)},
                       {"id": "now", "ruleId": ours, "firedAt": iso(-5)}]
    assert [a["id"] for a in neatlogs_link.new_alerts(api, since=started)] == ["now"]
    assert {"old-run", "ancient", "now"} <= set(neatlogs_link.load()["seen_alerts"])


def test_medic_opens_its_incident_from_the_neatlogs_alert(nl, monkeypatch):
    fake, api = nl
    m = Medic.__new__(Medic)
    m.log = lambda msg: None
    m.watcher = SimpleNamespace(state={"healthy_release": ""}, recent=lambda release=None: [])
    monkeypatch.setattr(agent_mod.world, "head", lambda: "b" * 40)
    monkeypatch.setattr(agent_mod.incidents, "save", lambda inc: None)
    alert = {"id": "a1", "ruleName": "Medic: tool call failed (3 in 5 min)", "detectionName": "Medic: tool call failed",
             "count": 4, "threshold": 3, "windowMinutes": 5, "sampleTraceIds": ["t1", "t2"]}
    inc = m.open([agent_mod.Signal("neatlogs_alert", "neatlogs_alert_hits", 3.0, 4.0)], [alert])
    assert inc.neatlogs["alert"]["ruleName"].startswith("Medic: tool call failed")
    assert inc.neatlogs["sample_trace_ids"] == ["t1", "t2"]
    assert any("neatlogs alert" in e["msg"] for e in inc.timeline)


def test_write_back_goes_onto_the_neatlogs_card_as_versions(nl, monkeypatch):
    fake, api = nl
    neatlogs_link.setup(api, log=lambda m: None)                     # configured
    calls = []
    monkeypatch.setattr(neatlogs_link, "update_card", lambda card, **kw: calls.append(("card", card, kw)) or True)
    monkeypatch.setattr(neatlogs_link, "update_fix", lambda card, status, note="": calls.append(("fix", card, status, note)) or True)
    monkeypatch.setattr(neatlogs_link, "investigate", lambda *a, **k: {"verdict": "none"})
    m = Medic.__new__(Medic)
    m.log = lambda msg: None
    inc = agent_mod.Incident(signals=[], release="b" * 40, healthy_release="a" * 40, trace_ids=["t1"], id="inc1",
                             phase="decided")
    inc.findings = {"neatlogs": {"verdict": "root_cause", "suggestion_id": "card-1"}}
    m._tell_neatlogs(inc, "diagnosed code regression", root_cause="**Proven**")
    inc.phase = "resolved"
    m._tell_neatlogs(inc, "healed", resolved=True)
    first, second, closed = calls
    assert first[0] == "card" and first[2]["root_cause"] == "**Proven**" and "diagnosed" in first[2]["evidence_reasoning"]
    assert "healed" in second[2]["evidence_reasoning"] and "diagnosed" in second[2]["evidence_reasoning"]   # history grows
    assert closed[:3] == ("fix", "card-1", "resolved") and "healed" in closed[3]
    assert inc.neatlogs["suggestion_resolved"] == "card-1" and all(n["card"] for n in inc.neatlogs["notes"])


def test_alerts_during_an_open_incident_are_attached_to_it(nl, monkeypatch):
    monkeypatch.setattr(agent_mod.incidents, "save", lambda inc: None)
    m = Medic.__new__(Medic)
    m.log = lambda msg: None
    inc = agent_mod.Incident(signals=[], release="b" * 40, healthy_release="a" * 40, trace_ids=[], id="inc1")
    m._attach_alerts(inc, [{"id": "a1", "ruleName": "Medic: tool call failed (3 in 5 min)", "count": 3,
                            "sampleTraceIds": ["t1", "t2"]}])
    assert inc.neatlogs["alert"]["ruleName"].startswith("Medic: tool call failed")
    assert inc.neatlogs["sample_trace_ids"] == ["t1", "t2"]
    assert any("neatlogs raised it too" in e["msg"] for e in inc.timeline)


class FakeMcp(BaseHTTPRequestHandler):
    """A Streamable-HTTP MCP server with the triage tools, answering over SSE."""
    calls: list = []
    args: list = []

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeMcp.calls.append((msg.get("method"), (msg.get("params") or {}).get("name"), self.headers.get("Authorization")))
        if "id" not in msg:
            self.send_response(202)
            self.end_headers()
            return
        tool = (msg.get("params") or {}).get("name")
        args = (msg.get("params") or {}).get("arguments") or {}
        FakeMcp.args.append((tool, args))
        payload = {"triage_list": {"total": 2, "triage_cards": [
                       {"triage_id": "s2", "title": "unrelated", "linked_trace_ids": ["zz"], "workflow_name": "other",
                        "created_at": "2026-10-10T00:00:00Z", "thread_id": "th2"},
                       {"triage_id": "s1", "title": "read_doc truncates bodies", "linked_trace_ids": "['t2', 'q9']",
                        "workflow_name": "research-agent", "created_at": "2026-10-10T00:00:00Z", "thread_id": "th1",
                        "confidence": "0.7250000238418579", "root_cause": "read_doc cuts bodies at 300 chars"}]},
                   "triage_get": {"triage_id": "s1", "title": "read_doc truncates document bodies at 300 chars",
                                  "location": "target_agent/tools.py:read_doc", "confidence": 0.8},
                   "triage_get_investigation": {"steps": [{"tool": "get_trace_context"}]},
                   "triage_update_status": {"ok": True}}.get(tool, {})
        result = {"serverInfo": {"name": "neatlogs"}} if msg["method"] == "initialize" else \
            {"content": [{"type": "text", "text": json.dumps(payload)}]}
        body = f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result})}\n\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Mcp-Session-Id", "sess-1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def mcp_server():
    FakeMcp.calls = []
    FakeMcp.args = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMcp)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/mcp"
    server.shutdown()


def test_investigation_comes_from_the_card_that_links_these_traces(mcp_server, monkeypatch):
    mcp = neatlogs_link.Mcp(mcp_server, key="project-key").connect()
    finding = neatlogs_link.investigate(["t1", "t2"], time.time(), mcp=mcp)
    assert finding["verdict"] == "root_cause" and finding["suggestion_id"] == "s1" and finding["matched_traces"] == 1
    assert "read_doc" in finding["location"] and finding["confidence"] == 0.8
    assert ("triage_get", {"triage_id": "s1"}) in FakeMcp.args
    assert ("triage_get_investigation", {"thread_id": "th1"}) in FakeMcp.args
    assert all(auth == "Bearer project-key" for _, _, auth in FakeMcp.calls)


def test_an_unrelated_card_is_never_used_as_evidence(mcp_server):
    mcp = neatlogs_link.Mcp(mcp_server, key="k").connect()
    finding = neatlogs_link.investigate(["nothing-linked"], time.time(), mcp=mcp)   # cards are old and unlinked
    assert finding["verdict"] == "none"


def test_moving_the_neatlogs_card_needs_the_users_email(mcp_server, monkeypatch):
    mcp = neatlogs_link.Mcp(mcp_server, key="k").connect()
    monkeypatch.delenv("NEATLOGS_USER_EMAIL", raising=False)
    assert neatlogs_link.update_fix("s1", "resolved", mcp=mcp) is False
    assert not any(t == "triage_update_status" for t, _ in FakeMcp.args)
    monkeypatch.setenv("NEATLOGS_USER_EMAIL", "team@example.com")
    assert neatlogs_link.update_fix("s1", "resolved", note="Agent Medic: healed", mcp=mcp) is True
    assert ("triage_update_status", {"triage_id": "s1", "user_email": "team@example.com", "status": "resolved",
                                     "resolution_note": "Agent Medic: healed"}) in FakeMcp.args
    assert neatlogs_link.update_card("s1", mcp=mcp, root_cause="**Proven**", evidence_reasoning="") is True
    assert ("triage_update", {"triage_id": "s1", "user_email": "team@example.com", "root_cause": "**Proven**"}) in FakeMcp.args


def test_unreachable_neatlogs_never_stops_the_diagnosis():
    finding = neatlogs_link.investigate(["t1"], time.time(), mcp=neatlogs_link.Mcp("http://127.0.0.1:9/mcp", timeout=2))
    assert finding["verdict"] == "unavailable"


def regression_findings(neatlogs_finding):
    surf = {"kind": "tool_handler", "name": "read_doc"}
    return {"regression": {"verdict": "culprit", "confidence": 0.9, "culprit": "c" * 40, "changed_surfaces": [surf]},
            "localize": {"verdict": "output_changed", "surfaces": [surf]}, "neatlogs": neatlogs_finding}


def test_neatlogs_agreeing_raises_confidence_and_disagreeing_is_recorded():
    agree = coordinator.decide({}, regression_findings({"verdict": "root_cause", "detail": "read_doc truncates",
                                                        "location": "tools.py:read_doc"}))
    assert any("neatlogs' investigation points at the same code: read_doc" in r for r in agree["rationale"])
    assert agree["confidence"] == 0.97
    differ = coordinator.decide({}, regression_findings({"verdict": "root_cause", "detail": "the prompt is too long"}))
    assert differ["cause"] == "code_regression" and any("neatlogs' investigation suggests" in d for d in differ["disagreements"])


def test_webhook_only_rings_the_doorbell(tmp_path):
    handler = type("H", (dashboard.Handler,), {"medic_dir": tmp_path})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/hooks/neatlogs",
                                     data=b'{"event": "alert.threshold_breach", "forged": true}', method="POST")
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 204
        assert (tmp_path / "neatlogs" / "doorbell").exists()
        assert not (tmp_path / "incidents").exists()                         # the body is never acted on
    finally:
        server.shutdown()
