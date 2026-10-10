import pytest

from medic import contract, coordinator, triage
from medic.evidence import TraceEvidence, window
from medic.investigators import dependency, loops
from medic.watcher import compare
from target_agent import kb_service
from target_agent.agent import run_agent
from target_agent.llm import ScriptedProvider


@pytest.fixture
def chaos():
    def set_mode(mode):
        kb_service._state["mode"] = mode
    yield set_mode
    kb_service._state["mode"] = "normal"


def ev(**kw):
    base = dict(trace_id="t", started_at="2026-10-10T00:00:00Z", release="r1", status="OK", cost_usd=0.0,
                tokens=100, tool_calls=3, duration_ms=10, question="Where is the pilot plant?",
                answer="It is in Narvik.")
    return TraceEvidence(**{**base, **kw})


def test_scripted_agent_answers_and_breaks_under_schema_v2(chaos):
    ok = run_agent("Where is the Halcyon pilot plant located?", ScriptedProvider())
    assert ok["status"] == "ok" and "Narvik" in ok["answer"] and ok["tool_errors"] == 0
    chaos("schema_v2")
    broken = run_agent("Where is the Halcyon pilot plant located?", ScriptedProvider())
    assert broken["tool_errors"] >= 1 and "could not" in broken["answer"]


def test_contract_diff_names_the_change(kb_url, chaos):
    before = contract.probe(kb_url, "pilot plant")
    chaos("schema_v2")
    after = contract.probe(kb_url, "pilot plant")
    changes = contract.diff({"search": before["search"], "doc": before["doc"]},
                            {"search": after["search"], "doc": after["doc"]})
    assert "$.search: was list, now object {results}" in changes
    assert "$.doc.body: removed" in changes and "$.doc.content: added (str)" in changes


@pytest.mark.parametrize("mode,verdict", [("normal", "healthy"), ("schema_v2", "contract_changed"),
                                          ("outage", "outage")])
def test_dependency_investigator(kb_url, chaos, mode, verdict):
    baseline = contract.probe(kb_url, "pilot plant")
    chaos(mode)
    finding = dependency.investigate(kb_url, baseline, [{"question": "pilot plant", "tool_errors": []}])
    assert finding["verdict"] == verdict


def test_rate_limit_detected(kb_url, chaos, monkeypatch):
    baseline = contract.probe(kb_url, "pilot plant")
    chaos("rate_limit")
    monkeypatch.setattr(kb_service.random, "random", lambda: 0.1)   # every request limited
    assert dependency.investigate(kb_url, baseline, [])["verdict"] == "rate_limited"


def test_watcher_signals_and_triage():
    healthy = [ev(trace_id=f"h{i}") for i in range(6)]
    broken = [ev(trace_id=f"b{i}", status="ERROR", answer="I could not retrieve the documents",
                 tool_errors=[{"tool": "search_docs", "type": "TypeError", "message": "string indices", "input": ""}])
              for i in range(6)]
    signals = compare(window(healthy), window(broken))
    assert {s.name for s in signals} >= {"tool_errors", "failures"}
    t = triage.triage(broken, [s.__dict__ for s in signals], release_changed=False)
    assert t["dominant"] == ["tool_error", "search_docs", "TypeError"]
    assert t["investigators"] == ["dependency", "regression", "localize"]


def test_loop_investigator_finds_repeats():
    healthy = window([ev(trace_id=f"h{i}") for i in range(6)])
    looping = [ev(trace_id=f"l{i}", tool_calls=6, tool_inputs=[["read_doc", '{"doc_id":"a"}']] * 2
                  + [["search_docs", "q"]], repeated_calls=1) for i in range(6)]
    assert loops.investigate(looping, healthy)["verdict"] == "redundant_calls"


def test_coordinator_trusts_healthy_release_replay():
    # Dependency drift was seen, but the old release still works today: blame the release.
    d = coordinator.decide({}, {"dependency": {"verdict": "contract_changed", "detail": "x", "confidence": 0.9},
                                "regression": {"verdict": "culprit", "culprit": "abc", "detail": "y", "confidence": 0.9}})
    assert d["cause"] == "code_regression" and d["remedy"] == "code_fix" and d["disagreements"]
    # The old release fails today too: the code isn't to blame.
    d = coordinator.decide({}, {"dependency": {"verdict": "contract_changed", "detail": "x", "confidence": 0.9},
                                "regression": {"verdict": "good_release_fails_today", "detail": "y"}})
    assert d["cause"] == "dependency_contract" and d["remedy"] == "adapt_client"
    d = coordinator.decide({}, {"dependency": {"verdict": "outage", "detail": "x"},
                                "regression": {"verdict": "no_code_change", "detail": "y"}})
    assert d["remedy"] == "escalate"


def test_external_fault_class_separates_model_404_from_tool_timeouts():
    from medic.external import fault_class
    model_404 = {"type": "LLM", "message": "404 NOT_FOUND. models/gemini-2.5-flash is no longer available"}
    wrong_model = {"type": "LLM", "message": "404 NOT_FOUND. models/gemini-1.5-flash is not found"}
    search_timeout = {"type": "TOOL", "message": "operation timed out"}
    assert fault_class(model_404) == fault_class(wrong_model) == ("LLM", "404")
    assert fault_class(search_timeout) != fault_class(model_404)


def test_stale_data_is_a_content_change_and_escalates(kb_url, chaos):
    baseline = contract.probe(kb_url, "Where is the Halcyon pilot plant located?")
    chaos("stale_data")
    finding = dependency.investigate(kb_url, baseline, [{"question": "x", "expect": [["Narvik"]], "tool_errors": []}])
    assert finding["verdict"] == "content_changed"
    d = coordinator.decide({}, {"dependency": finding, "regression": {"verdict": "no_code_change", "detail": "y"}})
    assert d["cause"] == "dependency_data" and d["remedy"] == "escalate"
