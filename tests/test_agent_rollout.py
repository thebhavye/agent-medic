"""The deploy step never promotes a fix that production hasn't judged healthy."""

from types import SimpleNamespace

import pytest

from medic import agent as agent_mod
from medic.agent import Medic
from medic.incidents import Incident


@pytest.fixture
def medic(monkeypatch):
    calls = {"start": [], "finish": 0, "promote": 0}
    state = {"active": None, "verdict": None}
    monkeypatch.setattr(agent_mod, "ROLLOUT", "canary")
    monkeypatch.setattr(agent_mod.rollout, "current", lambda: state["active"])

    def start(inc_id, branch):
        calls["start"].append(inc_id)
        state["active"] = {"incident": inc_id, "candidate": "c" * 40, "share": 0.5, "started": 1.0}
        return state["active"]

    def finish():
        calls["finish"] += 1
        state["active"] = None

    monkeypatch.setattr(agent_mod.rollout, "start", start)
    monkeypatch.setattr(agent_mod.rollout, "finish", finish)
    monkeypatch.setattr(agent_mod.rollout, "judge", lambda watcher, compare: state["verdict"])
    state["answers"] = {"pass_rate": 1.0, "n": 8}
    monkeypatch.setattr(agent_mod.canary, "run", lambda st, log, code_dir=None, sha=None: state["answers"])
    monkeypatch.setattr(agent_mod, "save_state", lambda st: None)
    m = Medic.__new__(Medic)
    m.auto_deploy, m.log = True, (lambda msg: None)
    m.watcher = SimpleNamespace(state={"canary_baseline": 1.0})
    monkeypatch.setattr(m, "_promote", lambda inc: calls.__setitem__("promote", calls["promote"] + 1), raising=False)
    return m, calls, state


def incident(i="inc1"):
    inc = Incident(signals=[], release="r", healthy_release="h", trace_ids=[], id=i, phase="remedied")
    inc.remedy = {"branch": "medic/fix", "attempts": [{"n": 1, "accepted": True}]}
    return inc


def test_inconclusive_canary_is_rolled_back_not_promoted(medic):
    m, calls, state = medic
    inc = incident()
    m.do_deploy(inc)
    assert inc.phase == "canary" and calls["start"] == ["inc1"]
    state["verdict"] = {"verdict": "inconclusive", "detail": "only 1 canary trace", "state": state["active"]}
    m.do_canary(inc)
    assert calls["promote"] == 0 and calls["finish"] == 1 and inc.phase == "escalated"


def test_only_healthy_promotes(medic):
    m, calls, state = medic
    inc = incident()
    m.do_deploy(inc)
    state["verdict"] = {"verdict": "healthy", "detail": "4 canary traces at 0%", "state": state["active"]}
    m.do_canary(inc)
    assert calls["promote"] == 1


def test_existing_rollout_for_this_incident_is_resumed_not_promoted(medic):
    m, calls, state = medic
    state["active"] = {"incident": "inc1", "candidate": "c" * 40, "share": 0.5, "started": 1.0}
    inc = incident()                       # crashed after start(), before the phase was saved
    m.do_deploy(inc)
    assert inc.phase == "canary" and calls["promote"] == 0 and calls["start"] == []


def test_another_incidents_rollout_makes_this_one_wait(medic):
    m, calls, state = medic
    state["active"] = {"incident": "other", "candidate": "c" * 40, "share": 0.5, "started": 1.0}
    inc = incident()
    m.do_deploy(inc)
    assert inc.phase == "remedied" and calls["promote"] == 0 and calls["start"] == []


def test_canary_phase_without_its_rollout_goes_back_to_deploy(medic):
    m, calls, state = medic
    inc = incident()
    inc.phase = "canary"
    m.do_canary(inc)
    assert inc.phase == "remedied" and calls["promote"] == 0


def test_clean_traces_with_wrong_answers_roll_back(medic):
    m, calls, state = medic
    inc = incident()
    m.do_deploy(inc)
    state["verdict"] = {"verdict": "healthy", "detail": "4 canary traces at 0%", "state": state["active"]}
    state["answers"] = {"pass_rate": 0.25, "n": 8}
    m.do_canary(inc)
    assert calls["promote"] == 0 and calls["finish"] == 1
    assert inc.phase == "decided" and "known-answer eval" in inc.remedy["attempts"][-1]["feedback"]
