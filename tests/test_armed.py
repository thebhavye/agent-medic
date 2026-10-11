"""Armed mode: the runtime engages the last known good by itself, without waiting for Medic."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import medic_runtime as medic
from medic_runtime import bundle, core

GOOD_SOURCE = "def lookup(args):\n    return args['query']\n"
SCHEMA_GOOD = {"name": "search", "params": ["query"]}


@medic.surface("lookup")
def lookup(args):
    return args["q"]                     # the broken release renamed the parameter


@pytest.fixture
def armed(tmp_path, monkeypatch):
    standby = tmp_path / "standby.json"
    monkeypatch.setenv("MEDIC_STANDBY", str(standby))
    monkeypatch.setenv("MEDIC_RUNTIME_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("MEDIC_RELEASE", "b" * 12)
    monkeypatch.delenv("MEDIC_CONTROL", raising=False)
    monkeypatch.delenv("MEDIC_RUNTIME_KEY", raising=False)
    medic.reset()

    def publish(good="a" * 12, functions=None, values=None):
        body = {"good_release": good, "issued_at": time.time(), "expires_at": time.time() + 600,
                "functions": functions if functions is not None else {"lookup": {"source": GOOD_SOURCE,
                                                                                 "from_release": "a" * 40}},
                "values": values or {}}
        standby.write_text(json.dumps(bundle.sign(body, None)), encoding="utf-8")
        medic.reset()
    yield publish, tmp_path
    medic.reset()


def run_request(args):
    """One agent request: the tool's error goes back to the model, as the research agent does."""
    with medic.request():
        try:
            return medic.guard_tool("search", args, lambda: lookup(args))
        except Exception:  # noqa: BLE001
            return None


def test_code_failures_engage_the_last_known_good_by_itself(armed):
    publish, _ = armed
    publish()
    results = [run_request({"query": f"x{i}"}) for i in range(core.TRIP_FAILURES)]
    assert results == [None] * core.TRIP_FAILURES                  # these users saw the bug
    assert run_request({"query": "after"}) == "after"              # healed: no Medic round trip
    assert any(i["kind"] == "auto" and i["name"] == "engaged" for i in medic.interventions())


def test_engagement_is_shared_with_other_processes_of_the_release(armed):
    publish, tmp = armed
    publish()
    for i in range(core.TRIP_FAILURES):
        run_request({"query": f"x{i}"})
    medic.reset()                                                  # a fresh worker process
    state = json.loads((tmp / "state.json").read_text())
    assert state["b" * 12]["names"] == ["lookup"]
    assert run_request({"query": "fresh worker"}) == "fresh worker"


def test_the_good_release_itself_is_never_patched(armed, monkeypatch):
    publish, _ = armed
    publish(good="b" * 12)                                         # this process IS the good release
    for i in range(core.TRIP_FAILURES + 2):
        assert run_request({"query": f"x{i}"}) is None
    assert medic.interventions() == []


def test_nothing_changed_since_good_means_not_a_code_problem(armed):
    publish, _ = armed
    publish(functions={"lookup": {"source": "def lookup(args):\n    return args['q']\n"}})   # same as live
    for i in range(core.TRIP_FAILURES + 2):
        run_request({"query": f"x{i}"})
    assert not any(i["kind"] == "auto" for i in medic.interventions())


def test_dependency_outages_do_not_swap_code(armed):
    publish, _ = armed
    publish()

    def down():
        raise ConnectionError("knowledge base unreachable")
    for _ in range(core.TRIP_FAILURES + 2):
        with medic.request():
            try:
                medic.guard_tool("fetch", {}, down)
            except Exception:  # noqa: BLE001
                pass
    assert not any(i["kind"] == "auto" for i in medic.interventions())


def test_a_standby_that_does_not_help_is_released(armed):
    publish, tmp = armed
    publish(functions={"lookup": {"source": "def lookup(args):\n    raise KeyError('still broken')\n"}})
    for i in range(core.TRIP_FAILURES):
        run_request({"query": f"x{i}"})                            # engages
    for i in range(core.TRIP_WINDOW):
        run_request({"query": f"y{i}"})                            # still failing with the standby on
    state = json.loads((tmp / "state.json").read_text())
    assert state["b" * 12]["status"] == "not_code"
    assert any(i["name"] == "released" for i in medic.interventions())


def test_non_answers_count_as_failures(armed):
    publish, _ = armed
    publish(functions={}, values={"prompt": "healthy prompt"})
    for _ in range(core.TRIP_FAILURES):
        with medic.request():
            medic.value("prompt", "broken prompt")
            medic.answer("I could not find that in the knowledge base.")
    with medic.request():
        assert medic.value("prompt", "broken prompt") == "healthy prompt"


def test_blowing_the_tool_call_budget_engages_the_healthy_prompt(armed, tmp_path):
    publish, _ = armed
    publish(functions={}, values={"prompt": "healthy prompt"})
    standby = json.loads((tmp_path / "standby.json").read_text())
    standby["payload"]["limits"] = {"tool_calls_per_request": 4}
    (tmp_path / "standby.json").write_text(json.dumps(standby))
    medic.reset()
    for i in range(core.TRIP_FAILURES):
        with medic.request():
            medic.value("prompt", "re-verify every fact")
            for j in range(6):                                 # answers fine, but twice the calls
                medic.guard_tool("search", {"q": f"{i}-{j}"}, lambda: "ok")
    with medic.request():
        assert medic.value("prompt", "re-verify every fact") == "healthy prompt"


def test_targeted_first_aid_wins_over_the_standby(armed, monkeypatch, tmp_path):
    publish, _ = armed
    publish()
    control = tmp_path / "control.json"
    control.write_text(json.dumps(bundle.sign({"release": "b" * 12, "expires_at": time.time() + 60,
                                               "functions": {"lookup": {"source": "def lookup(args):\n    return 'targeted'\n"}}},
                                              None)), encoding="utf-8")
    monkeypatch.setenv("MEDIC_CONTROL", str(control))
    medic.reset()
    assert run_request({"query": "x"}) == "targeted"


def test_remote_bundle_redirects_are_refused(monkeypatch):
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://example.com/steal")
            self.end_headers()

        def log_message(self, *a):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("MEDIC_RUNTIME_KEY", "k")
        monkeypatch.setenv("MEDIC_CONTROL_TOKEN", "t")
        monkeypatch.setenv("MEDIC_RELEASE", "b" * 12)
        monkeypatch.setenv("MEDIC_CONTROL", f"http://127.0.0.1:{server.server_address[1]}/b")
        medic.reset()
        assert medic.value("prompt", "live") == "live"             # not followed, nothing applied
    finally:
        server.shutdown()
        medic.reset()


def test_short_lived_workers_are_judged_together(armed):
    # Live on the first real project: each batch of questions ran in a fresh process, so no
    # process ever saw three failures and the armed runtime never engaged.
    publish, tmp = armed
    publish()
    for i in range(core.TRIP_FAILURES):
        medic.reset()                                              # a new worker for every request
        assert run_request({"query": f"x{i}"}) is None
    medic.reset()
    assert run_request({"query": "next worker"}) == "next worker"  # healed across workers
    assert json.loads((tmp / "state.json").read_text())["b" * 12]["status"] == "engaged"


def test_old_shared_outcomes_do_not_trip_it(armed, monkeypatch):
    publish, tmp = armed
    publish()
    for i in range(core.TRIP_FAILURES - 1):
        medic.reset()
        run_request({"query": f"x{i}"})
    later = time.time() + core.OUTCOME_TTL_S + 1                   # those failures are history now
    monkeypatch.setattr(core.time, "time", lambda: later)
    medic.reset()
    assert run_request({"query": "one more"}) is None              # one fresh failure: not a pattern
    assert "status" not in (json.loads((tmp / "state.json").read_text()).get("b" * 12) or {})
