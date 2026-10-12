"""An escalation still serves users and says why: a crashed investigator, with the evidence so far."""

from medic import agent as agent_mod
from medic.agent import Medic


def _medic(monkeypatch, decision):
    m = Medic.__new__(Medic)
    m.log = lambda msg: None
    m.first_aid_calls = []
    monkeypatch.setattr(agent_mod.coordinator, "decide", lambda triage, findings: decision)
    monkeypatch.setattr(m, "_tell_neatlogs", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(m, "_first_aid", lambda inc: m.first_aid_calls.append(inc.id), raising=False)
    return m


ESCALATE = {"cause": "unexplained", "confidence": 0.3, "remedy": "escalate", "disagreements": [], "rationale": []}


def test_a_new_release_that_escalates_still_gets_first_aid_and_the_page_says_why(monkeypatch):
    m = _medic(monkeypatch, ESCALATE)
    inc = agent_mod.Incident(signals=[{"name": "quality"}], release="b" * 40, healthy_release="a" * 40,
                             trace_ids=[], id="esc")
    inc.findings = {"regression": {"verdict": "error", "detail": "TimeoutExpired: entire graph commit"},
                    "dependency": {"verdict": "healthy", "detail": "the API answers normally"}}
    m.do_decide(inc)
    assert inc.phase == "escalated" and m.first_aid_calls == ["esc"]
    page = inc.timeline[-1]["msg"]
    assert "regression investigator failed (TimeoutExpired: entire graph commit)" in page


def test_no_new_release_means_nothing_to_stand_in(monkeypatch):
    m = _medic(monkeypatch, ESCALATE)
    inc = agent_mod.Incident(signals=[{"name": "quality"}], release="a" * 40, healthy_release="a" * 40,
                             trace_ids=[], id="same")
    m.do_decide(inc)
    assert inc.phase == "escalated" and m.first_aid_calls == []
    assert "has no safe automatic fix" in inc.timeline[-1]["msg"]
