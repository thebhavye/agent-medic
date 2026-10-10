"""Armed mode, review fixes: parallel requests, candidates, precedence, failures, freshness."""

import json
import threading
import time
from types import SimpleNamespace

import medic_runtime as medic
from medic import agent as agent_mod
from medic import first_aid, paths, traffic
from medic.agent import Medic
from medic.incidents import Incident
from medic_runtime import bundle, core
from test_armed import armed, lookup, run_request  # noqa: F401 - fixture and helpers


def test_parallel_failing_requests_leave_readable_engaged_state(armed):  # noqa: F811
    publish, tmp = armed
    publish()
    threads = [threading.Thread(target=run_request, args=({"query": f"x{i}"},)) for i in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    state = json.loads((tmp / "state.json").read_text())                # parses: no torn writes
    assert state["b" * 12]["status"] == "engaged"
    assert not list(tmp.glob("state.json.*.tmp"))                       # no leftovers


def test_targeted_bundle_replaces_the_automatic_engagement(armed, monkeypatch):  # noqa: F811
    publish, tmp = armed
    publish(functions={"lookup": {"source": "def lookup(args):\n    return args['query']\n"}},
            values={"prompt": "healthy prompt"})
    (tmp / "state.json").write_text(json.dumps({"b" * 12: {"status": "engaged", "names": ["lookup", "prompt"],
                                                          "at": time.time(), "good_release": "a" * 12}}))
    medic.reset()
    with medic.request():
        assert medic.value("prompt", "live prompt") == "healthy prompt"   # engaged, nothing targeted yet
    control = tmp / "control.json"
    control.write_text(json.dumps(bundle.sign({"release": "b" * 12, "expires_at": time.time() + 60,
                                               "functions": {"lookup": {"source": "def lookup(args):\n    return 'proved'\n"}}},
                                              None)), encoding="utf-8")
    monkeypatch.setenv("MEDIC_CONTROL", str(control))
    medic.reset()
    assert run_request({"query": "x"}) == "proved"
    with medic.request():
        assert medic.value("prompt", "live prompt") == "live prompt"    # not a leftover from the engagement


def test_unsuccessful_finishes_count_as_failures(armed):  # noqa: F811
    publish, _ = armed
    publish(functions={}, values={"prompt": "healthy prompt"})
    for _ in range(core.TRIP_FAILURES):
        with medic.request():
            medic.value("prompt", "broken prompt")
            medic.failed("hit max_tokens")                              # no answer text at all
    with medic.request():
        assert medic.value("prompt", "broken prompt") == "healthy prompt"


def test_rollout_candidates_run_unarmed(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIC_DIR", tmp_path)
    monkeypatch.setattr(paths, "DEPLOY", tmp_path / "deploy")
    monkeypatch.setattr(paths, "TRAFFIC", tmp_path / "traffic")
    monkeypatch.setattr(paths, "CASES", tmp_path / "cases.jsonl")
    (tmp_path / "cases.jsonl").write_text('{"id": "q", "question": "q?", "expect": []}\n')
    envs = []
    monkeypatch.setattr(traffic.subprocess, "run", lambda cmd, **kw: envs.append(kw["env"]))
    for route in [(tmp_path / "deploy", "s" * 40), (tmp_path / "worktrees" / "canary", "c" * 40)]:
        monkeypatch.setattr(traffic.rollout, "route", lambda route=route: route)
        traffic.run_batch(1, 1)
    stable, candidate = envs
    assert "MEDIC_STANDBY" in stable and "MEDIC_RUNTIME_STATE" in stable
    assert "MEDIC_STANDBY" not in candidate and "MEDIC_RUNTIME_STATE" not in candidate


def make_medic(tmp_path, monkeypatch, live="b" * 40):
    monkeypatch.setattr(paths, "MEDIC_DIR", tmp_path)
    monkeypatch.delenv("MEDIC_RUNTIME_KEY", raising=False)
    m = Medic.__new__(Medic)
    m.log = lambda msg: None
    m.watcher = SimpleNamespace(state={"healthy_release": "a" * 12, "baseline": {"tool_calls_per_run": 3}},
                                recent=lambda release=None: [])
    monkeypatch.setattr(agent_mod.world, "head", lambda: live)
    monkeypatch.setattr(agent_mod.world, "git", lambda *a: "a" * 40)
    return m


def test_standby_is_rebuilt_when_a_new_release_goes_live(tmp_path, monkeypatch):
    built = []
    monkeypatch.setattr(first_aid, "standby", lambda root, good, live, baseline=None: built.append(live) or
                        {"good_release": good[:12], "live_release": live[:12], "issued_at": time.time(),
                         "expires_at": time.time() + 600, "functions": {}, "values": {}, "limits": {}})
    m = make_medic(tmp_path, monkeypatch)
    m._refresh_standby()
    m._refresh_standby()                                                # same live release: kept
    monkeypatch.setattr(agent_mod.world, "head", lambda: "c" * 40)
    m._refresh_standby()                                                # a new release: rebuilt
    assert built == ["b" * 40, "c" * 40]


def test_a_released_standby_is_not_reported_as_healing(tmp_path, monkeypatch):
    m = make_medic(tmp_path, monkeypatch)
    monkeypatch.setattr(agent_mod.incidents, "save", lambda inc: None)
    control = tmp_path / "control"
    control.mkdir()
    (control / "runtime-state.json").write_text(json.dumps({"b" * 12: {"status": "not_code", "at": time.time()}}))
    inc = m.open([agent_mod.Signal("self_healed", "self_healed_rate", 0.0, 0.5)])
    assert inc.first_aid == {}                                          # not claimed as protection
    (control / "runtime-state.json").write_text(json.dumps({"b" * 12: {
        "status": "engaged", "names": ["read_doc"], "at": time.time(), "good_release": "a" * 12}}))
    inc = m.open([agent_mod.Signal("self_healed", "self_healed_rate", 0.0, 0.5)])
    assert inc.first_aid["status"] == "auto" and inc.first_aid["functions"] == ["read_doc"]
