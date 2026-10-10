"""Medic's side of first aid: what to stand in, proving it works, and taking it off."""

import subprocess
import time
from types import SimpleNamespace

import pytest

from medic import agent as agent_mod
from medic import first_aid, paths
from medic.agent import Medic
from medic.incidents import Incident

AGENT = '''import medic_runtime as medic
from app.tools import SCHEMAS

PROMPT = (
    "Answer from the docs. "
    "Cite figures."
)


def answer(q):
    return medic.value("prompt", PROMPT), medic.value("schemas", SCHEMAS)
'''

TOOLS = '''import medic_runtime as medic

SCHEMAS = [{"name": "search", "params": ["query"]}]


@medic.surface("read")
def read(doc):
    return doc["body"]


def helper(x):
    return x
'''


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, message, **files):
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    healthy = commit(root, "v1", **{"app/agent.py": AGENT, "app/tools.py": TOOLS})
    return root, healthy


FILES = ["app/agent.py", "app/tools.py"]


def test_culprit_function_runs_its_parent_version_and_nothing_else_is_undone(repo):
    root, healthy = repo
    benign = commit(root, "docstring", **{"app/tools.py": TOOLS.replace("def helper(x):", "def helper(x):\n    \"\"\"doc\"\"\"")})
    culprit = commit(root, "cap", **{"app/tools.py": (root / "app/tools.py").read_text().replace(
        'return doc["body"]', 'return doc["body"][:300]')})
    p = first_aid.plan(root, culprit, healthy, culprit, files=FILES)
    assert list(p["functions"]) == ["read"] and p["base"] == benign
    assert '[:300]' not in p["functions"]["read"]["source"] and not p["values"]


def test_values_are_found_where_they_are_defined_and_pinned_as_literals(repo):
    root, healthy = repo
    culprit = commit(root, "rename param", **{"app/tools.py": TOOLS.replace('["query"]', '["q"]')})
    p = first_aid.plan(root, culprit, healthy, culprit, files=FILES)
    assert p["values"] == {"schemas": [{"name": "search", "params": ["query"]}]}   # registered in agent.py

    prompt = commit(root, "prompt", **{"app/agent.py": AGENT.replace("Cite figures.", "Double-check everything.")})
    p = first_aid.plan(root, prompt, healthy, prompt, files=FILES)
    assert p["values"] == {"prompt": "Answer from the docs. Cite figures."}       # implicit concatenation


def test_unregistered_change_gets_no_first_aid(repo):
    root, healthy = repo
    culprit = commit(root, "helper", **{"app/tools.py": TOOLS.replace("return x", "return -x")})
    assert first_aid.empty(first_aid.plan(root, culprit, healthy, culprit, files=FILES))


def test_publish_active_and_clear(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIC_DIR", tmp_path)
    monkeypatch.setenv("MEDIC_RUNTIME_KEY", "k")
    body = first_aid.payload("inc1", "b" * 40, {"values": {"prompt": "x"}})
    first_aid.publish(body)
    assert first_aid.active()["incident"] == "inc1" and body["release"] == "b" * 12
    assert first_aid.clear("other") is False and first_aid.active() is not None   # not that incident's
    assert first_aid.clear("inc1") is True and first_aid.active() is None
    first_aid.publish({**body, "expires_at": time.time() - 1})
    assert first_aid.active() is None


@pytest.fixture
def medic(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIC_DIR", tmp_path)
    monkeypatch.delenv("MEDIC_RUNTIME_KEY", raising=False)
    m = Medic.__new__(Medic)
    m.log = lambda msg: None
    m.watcher = SimpleNamespace(state={"canary_baseline": 1.0, "canary": [
        {"release": "b" * 12, "pass_rate": 0.0, "n": 8, "tool_calls_per_run": 3.0}]})
    plan = {"base": "a" * 40, "functions": {"read": {"source": "def read(d):\n    return d\n", "from_release": "a" * 40}},
            "values": {}, "skipped": [], "registered": ["read"]}
    monkeypatch.setattr(first_aid, "plan", lambda *a, **k: plan)
    trials = {"result": {"pass_rate": 1.0, "error_rate": 0.0, "n": 8}}
    monkeypatch.setattr(agent_mod.canary, "evaluate", lambda *a, **k: trials["result"])
    inc = Incident(signals=[], release="b" * 40, healthy_release="a" * 40, trace_ids=[], id="inc1", phase="decided")
    inc.decision = {"cause": "code_regression", "remedy": "code_fix", "culprit": "c" * 40}
    return m, inc, plan, trials


def test_verified_first_aid_goes_live(medic):
    m, inc, _, _ = medic
    m._first_aid(inc)
    assert inc.first_aid["status"] == "active" and inc.first_aid["before"]["pass_rate"] == 0.0
    assert first_aid.active()["functions"]["read"]["from_release"] == "a" * 40
    assert not list((paths.MEDIC_DIR / "control").glob("trial-*"))                # the trial bundle is gone
    assert any(e["phase"] == "first_aid" and "0% -> 100%" in e["msg"] for e in inc.timeline)


def test_first_aid_that_does_not_restore_answers_is_not_applied(medic):
    m, inc, _, trials = medic
    trials["result"] = {"pass_rate": 0.25, "error_rate": 0.0, "n": 8}
    m._first_aid(inc)
    assert inc.first_aid["status"] == "rejected" and first_aid.active() is None


def test_nothing_to_stand_in_is_said_plainly(medic):
    m, inc, plan, _ = medic
    plan.update(functions={}, skipped=[])
    m._first_aid(inc)
    assert inc.first_aid["status"] == "not_possible" and first_aid.active() is None


def test_promoting_the_cure_removes_first_aid(medic, monkeypatch):
    m, inc, _, _ = medic
    m._first_aid(inc)
    monkeypatch.setattr(agent_mod.remedy, "deploy", lambda inc: {"deployed": True, "release": "f" * 40})
    m._promote(inc)
    assert inc.first_aid["status"] == "removed" and first_aid.active() is None


def test_a_failed_trial_never_blocks_the_cure(medic, monkeypatch):
    import subprocess as sp
    m, inc, _, _ = medic

    def timeout(*a, **k):
        raise sp.TimeoutExpired("run_eval", 1800)
    monkeypatch.setattr(agent_mod.canary, "evaluate", timeout)
    m._first_aid(inc)                                       # must not raise: do_decide goes on
    assert inc.first_aid["status"] == "error" and "TimeoutExpired" in inc.first_aid["reason"]
    assert first_aid.active() is None


def test_aided_traces_never_mark_a_release_healthy(tmp_path, monkeypatch):
    from medic import watcher as watcher_mod
    from medic.evidence import TraceEvidence
    monkeypatch.setattr(paths, "MEDIC_DIR", tmp_path)        # no live bundle: the old traces must decide
    monkeypatch.setattr(watcher_mod, "save_state", lambda st: None)

    def ev(i, aided):
        return TraceEvidence(trace_id=f"t{i}", started_at="2026-10-10T00:00:00Z", release="b" * 12, status="OK",
                             cost_usd=0, tokens=100, tool_calls=2, duration_ms=10, question="q", answer="Narvik",
                             first_aid=["function:read_doc"] if aided else [])
    w = watcher_mod.Watcher.__new__(watcher_mod.Watcher)
    w.log = lambda *a: None
    w.state = {"baseline": {"n": 6, "failure_rate": 0.0, "tool_error_rate": 0.0, "non_answer_rate": 0.0,
                            "tokens_per_run": 100, "tool_calls_per_run": 2, "repeated_call_rate": 0.0},
               "healthy_release": "a" * 12, "canary": []}
    evs = [ev(i, aided=(i == 0)) for i in range(watcher_mod.RECENT)]
    monkeypatch.setattr(w, "recent", lambda release=None: evs, raising=False)
    w.check()
    assert w.state["healthy_release"] == "a" * 12           # one aided trace in the window: not proof
    evs[:] = [ev(i, aided=False) for i in range(watcher_mod.RECENT)]
    w.check()
    assert w.state["healthy_release"] == "b" * 12
