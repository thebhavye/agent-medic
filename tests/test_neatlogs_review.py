"""PR #10 review fixes: alert checkpoints, channel masks, cards, samples, gate, webhook limits."""

import json
import socket
import threading
import time
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from medic import agent as agent_mod
from medic import dashboard, neatlogs_link, tournament
from medic.agent import Medic
from test_neatlogs_link import FakeMcp, iso, mcp_server, nl  # noqa: F401 - fixtures and helpers


def test_alerts_fired_during_a_restart_are_still_handled(nl):  # noqa: F811
    fake, api = nl
    state = neatlogs_link.setup(api, log=lambda m: None)
    ours = state["rules"]["tool_failed"]
    neatlogs_link.new_alerts(api)                                     # the old Medic's last read
    fake.history[:] = [{"id": "while-down", "ruleId": ours, "firedAt": iso(-2)}]
    assert [a["id"] for a in neatlogs_link.new_alerts(api)] == ["while-down"]   # the new Medic, no `since`


def test_a_fresh_world_ignores_earlier_worlds_alerts_but_then_follows_the_checkpoint(nl):  # noqa: F811
    fake, api = nl
    state = neatlogs_link.setup(api, log=lambda m: None)
    ours = state["rules"]["tool_failed"]
    start = time.time()
    fake.history[:] = [{"id": "last-world", "ruleId": ours, "firedAt": iso(-120)}]
    assert neatlogs_link.new_alerts(api, since=start) == []
    fake.history.append({"id": "this-world", "ruleId": ours, "firedAt": iso(1)})
    assert [a["id"] for a in neatlogs_link.new_alerts(api, since=start)] == ["this-world"]


def test_follow_ups_count_but_one_rule_is_one_alert_per_read(nl):  # noqa: F811
    fake, api = nl
    state = neatlogs_link.setup(api, log=lambda m: None)
    ours = state["rules"]["tool_failed"]
    fake.history[:] = [{"id": "f1", "ruleId": ours, "firedAt": iso(-20), "isFollowup": True},
                       {"id": "f2", "ruleId": ours, "firedAt": iso(-10), "isFollowup": True}]
    assert [a["id"] for a in neatlogs_link.new_alerts(api)] == ["f1"]                  # one alert, merged


def test_adding_the_webhook_keeps_existing_channels(nl):  # noqa: F811
    fake, api = nl
    neatlogs_link.setup(api, log=lambda m: None)                      # first run: no public URL
    for r in fake.rules:
        r["channelMask"] = {"email": True, "slack": True, "webhookIds": ["team-hook"]}
    state = neatlogs_link.setup(api, webhook_url="https://medic.example/hooks/neatlogs", log=lambda m: None)
    assert not any(k.startswith("rule-webhook") for k in state["skipped"])        # the updates went through
    for r in fake.rules:                                              # as the server now stores them
        assert r["channelMask"]["email"] and r["channelMask"]["slack"]
        assert set(r["channelMask"]["webhookIds"]) == {"team-hook", state["webhook"]}


def test_a_fresh_worlds_floor_is_hard(nl):  # noqa: F811
    fake, api = nl
    state = neatlogs_link.setup(api, log=lambda m: None)
    ours = state["rules"]["tool_failed"]
    fake.history[:] = [{"id": "just-before", "ruleId": ours, "firedAt": iso(-10)}]
    assert neatlogs_link.new_alerts(api, since=time.time() - 2) == []   # no overlap before a fresh world


def test_repeats_of_a_rule_keep_every_sample(nl):  # noqa: F811
    fake, api = nl
    state = neatlogs_link.setup(api, log=lambda m: None)
    ours = state["rules"]["tool_failed"]
    fake.history[:] = [{"id": "f1", "ruleId": ours, "firedAt": iso(-20), "count": 3, "sampleTraceIds": ["t1", "t2"]},
                       {"id": "f2", "ruleId": ours, "firedAt": iso(-10), "count": 5, "sampleTraceIds": ["t2", "t3"]}]
    [alert] = neatlogs_link.new_alerts(api)
    assert alert["sampleTraceIds"] == ["t1", "t2", "t3"] and alert["count"] == 5 and alert["repeats"] == 2


def test_only_a_card_linking_these_traces_is_used(mcp_server):  # noqa: F811
    mcp = neatlogs_link.Mcp(mcp_server, key="k").connect()
    # A fresh card for the same workflow but not linking these traces: another failure's card.
    opened = neatlogs_link._iso_ts("2026-10-10T00:00:00Z")            # the card is from the same moment
    assert neatlogs_link.investigate(["unlinked"], opened, mcp=mcp)["verdict"] == "none"


def test_alert_samples_are_the_incidents_evidence(monkeypatch):
    m = Medic.__new__(Medic)
    asked = []
    m.watcher = SimpleNamespace(evidence_for=lambda ids: asked.append(ids) or [])
    inc = agent_mod.Incident(signals=[], release="b" * 40, healthy_release="a" * 40, trace_ids=["w1", "w2"], id="i")
    inc.neatlogs = {"sample_trace_ids": ["s1", "w1"]}
    m._evidence(inc)
    assert asked == [["s1", "w1", "w2"]]                              # samples first, no duplicates


def test_pending_card_sections_wait_for_a_late_card(monkeypatch):
    m = Medic.__new__(Medic)
    m.log = lambda msg: None
    monkeypatch.setattr(m, "_neatlogs", lambda: object(), raising=False)
    card = {"id": None}
    monkeypatch.setattr(m, "_late_neatlogs_card", lambda inc: None, raising=False)
    monkeypatch.setattr(m, "_card_id", lambda inc: card["id"], raising=False)
    written = []
    monkeypatch.setattr(neatlogs_link, "update_card", lambda c, **kw: written.append(kw) or True)
    monkeypatch.setattr(neatlogs_link, "update_fix", lambda *a, **k: True)
    inc = agent_mod.Incident(signals=[], release="b" * 40, healthy_release="a" * 40, trace_ids=[], id="i")
    m._tell_neatlogs(inc, "diagnosed", root_cause="**Proven**")          # no card yet
    m._tell_neatlogs(inc, "promoted", recommended_action_md="**Shipped**")
    assert written == [] and set(inc.neatlogs["pending"]) == {"root_cause", "recommended_action_md"}
    card["id"] = "late-card"
    m._tell_neatlogs(inc, "healed", resolved=True)
    assert written[0]["root_cause"] == "**Proven**" and written[0]["recommended_action_md"] == "**Shipped**"
    assert inc.neatlogs["pending"] == {}


def test_suppressed_alerts_are_recorded_on_the_escalated_incident(monkeypatch):
    m = Medic.__new__(Medic)
    m.log = lambda msg: None
    escalated = agent_mod.Incident(signals=[{"name": "neatlogs_alert:Medic: tool call failed (3 in 5 min)"}],
                                   release="b" * 40, healthy_release="a" * 40, trace_ids=[], id="esc", phase="escalated")
    monkeypatch.setattr(agent_mod.incidents, "load_all", lambda: [escalated])
    monkeypatch.setattr(agent_mod.incidents, "save", lambda inc: None)
    same = [agent_mod.Signal("neatlogs_alert:Medic: tool call failed (3 in 5 min)", "neatlogs_alert_hits", 3, 3)]
    other = [agent_mod.Signal("neatlogs_alert:Medic: agent refused (3 in 5 min)", "neatlogs_alert_hits", 3, 3)]
    assert m._recently_escalated(same) is escalated
    assert m._recently_escalated(other) is None                        # another rule is not suppressed


def test_a_narrow_fix_cannot_also_change_other_registrations(tmp_path):
    (tmp_path / "target_agent").mkdir()
    (tmp_path / "target_agent" / "tools.py").write_text(
        "def search_docs(query):\n    return query\n" + "\n" * 20 +
        'TOOL_HANDLERS = {\n    "search_docs": lambda args: search_docs(args["query"]),\n}\n')
    target = {"kind": "tool_handler", "name": "search_docs", "file": "target_agent/tools.py", "start": 1, "end": 2}
    sneaky = [{"path": "target_agent/tools.py", "find": '    "search_docs": lambda args: search_docs(args["query"]),',
               "replace": '    "search_docs": lambda args: search_docs(args["q"]),\n'
                          '    "read_doc": lambda args: "",'}]
    assert any("outside the target" in p for p in tournament.review(sneaky, tmp_path, target))


@pytest.fixture
def hook_server(tmp_path):
    handler = type("H", (dashboard.Handler,), {"medic_dir": tmp_path, "timeout": 1})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address, tmp_path
    server.shutdown()


@pytest.mark.parametrize("length,status", [("-1", b"411"), ("abc", b"411"), (str((1 << 20) + 1), b"413")])
def test_webhook_refuses_bad_lengths(hook_server, length, status):
    (host, port), tmp = hook_server
    with socket.create_connection((host, port), timeout=5) as s:
        s.sendall(f"POST /hooks/neatlogs HTTP/1.1\r\nHost: x\r\nContent-Length: {length}\r\n\r\n".encode())
        assert status in s.recv(200)
    assert not (tmp / "neatlogs" / "doorbell").exists()


def test_webhook_drops_a_stalled_sender(hook_server):
    (host, port), tmp = hook_server
    with socket.create_connection((host, port), timeout=5) as s:
        s.sendall(b"POST /hooks/neatlogs HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\npartial")
        started = time.time()
        assert s.recv(200) == b""                                       # the server hung up
        assert time.time() - started < 4
