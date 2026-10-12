"""Canary scheduling and regression detection; the eval itself is stubbed out."""

import json
import time

from medic import canary, paths, world


def test_due_respects_interval(monkeypatch):
    monkeypatch.setattr(canary, "INTERVAL_S", 60)
    assert canary.due({}) is True
    assert canary.due({"canary": []}) is True
    now = time.time()
    assert canary.due({"canary": [{"at": now}]}) is False
    assert canary.due({"canary": [{"at": now - 61}]}) is True


def test_regressed_needs_baseline_and_drop():
    assert canary.regressed({}) is None
    assert canary.regressed({"canary_baseline": 1.0, "canary": []}) is None
    # A run without a pass rate (eval crashed) is not a regression.
    assert canary.regressed({"canary_baseline": 1.0, "canary": [{"at": 1.0}]}) is None
    ok = {"canary_baseline": 1.0, "canary": [{"at": 1.0, "release": "a", "pass_rate": 0.9}] * 2}
    assert canary.regressed(ok) is None  # 0.10 drop < DROP (0.25)
    bad = {"canary_baseline": 1.0, "canary": [{"at": 1.0, "release": "a", "pass_rate": 0.75},
                                              {"at": 2.0, "release": "a", "pass_rate": 0.5}]}
    signal = canary.regressed(bad)  # exactly DROP counts; the better of the two runs is reported
    assert signal == {"name": "quality", "metric": "canary_pass_rate",
                      "baseline": 1.0, "current": 0.75}


def test_one_low_run_is_confirmed_before_it_counts():
    # A healthy but flaky app: one unlucky run drops below the bar.
    state = {"canary_baseline": 0.83, "canary": [{"at": 1.0, "release": "a", "pass_rate": 0.83},
                                                 {"at": 2.0, "release": "a", "pass_rate": 0.5}]}
    assert canary.regressed(state) is None
    assert canary.needs_confirm(state) is True
    state["canary"].append({"at": 3.0, "release": "a", "pass_rate": 0.83})   # the re-run passes
    assert canary.regressed(state) is None and canary.needs_confirm(state) is False
    state["canary"] += [{"at": 4.0, "release": "a", "pass_rate": 0.33}, {"at": 5.0, "release": "a", "pass_rate": 0.5}]
    assert canary.regressed(state)["current"] == 0.5 and canary.needs_confirm(state) is False


def test_low_runs_on_different_releases_do_not_confirm_each_other():
    state = {"canary_baseline": 1.0, "canary": [{"at": 1.0, "release": "a", "pass_rate": 0.5},
                                                {"at": 2.0, "release": "b", "pass_rate": 0.5}]}
    assert canary.regressed(state) is None and canary.needs_confirm(state) is True


def good_case(case_id="q1"):
    return {"id": case_id, "passed": True, "status": "ok", "tool_errors": 0,
            "cost_usd": 0.0, "input_tokens": 100, "output_tokens": 20, "tool_calls": 2}


def stub_run(monkeypatch, tmp_path, cases):
    """Replace the eval subprocess with one that writes a canned result file."""
    monkeypatch.setattr(world, "head", lambda: "abc123456789")
    monkeypatch.setattr(paths, "MEDIC_DIR", tmp_path)
    monkeypatch.setattr(paths, "CASES", tmp_path / "cases.jsonl")

    def fake_run(cmd, **kw):
        out = cmd[cmd.index("--out") + 1]
        with open(out, "w", encoding="utf-8") as handle:
            json.dump({"session": "s", "cases": cases}, handle)
        return None

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)


def test_run_records_pass_rate_and_baseline(tmp_path, monkeypatch):
    stub_run(monkeypatch, tmp_path, [good_case("q1"), good_case("q2")])
    state: dict = {}
    record = canary.run(state, log=lambda *a: None)
    assert record["pass_rate"] == 1.0 and record["n"] == 2
    assert record["release"] == "abc123456789"[:12]
    assert state["canary_baseline"] == 1.0
    assert len(state["canary"]) == 1


def bad_case(case_id):
    return {**good_case(case_id), "passed": False, "question": f"question {case_id}?",
            "answer": "Halcyon builds storage. " * 20, "expect": [["Narvik"], ["412"]]}


def test_run_records_failed_questions_and_a_fractional_rate(tmp_path, monkeypatch):
    stub_run(monkeypatch, tmp_path, [good_case("q1"), good_case("q2"), good_case("q3"), bad_case("q4")])
    state: dict = {}
    record = canary.run(state, log=lambda *a: None)
    assert record["pass_rate"] == 0.75 and record["n"] == 4
    assert [f["id"] for f in record["failed"]] == ["q4"]
    failed = record["failed"][0]
    assert failed["question"] == "question q4?" and failed["expect"] == [["Narvik"], ["412"]]
    assert len(failed["answer"]) == 300                       # long answers are cut for the state file


def test_existing_baseline_is_never_lowered(tmp_path, monkeypatch):
    stub_run(monkeypatch, tmp_path, [good_case("q1"), good_case("q2"), good_case("q3"), bad_case("q4")])
    state: dict = {"canary_baseline": 1.0}
    canary.run(state, log=lambda *a: None)
    assert state["canary_baseline"] == 1.0                    # a 0.75 run must not become the new reference


def test_low_first_run_does_not_become_the_baseline(tmp_path, monkeypatch):
    stub_run(monkeypatch, tmp_path, [good_case("q1"), bad_case("q2")])
    state: dict = {}
    record = canary.run(state, log=lambda *a: None)
    assert record["pass_rate"] == 0.5
    assert state.get("canary_baseline") is None               # an unhealthy run is no reference for "healthy"


def test_run_without_results_records_error(tmp_path, monkeypatch):
    monkeypatch.setattr(world, "head", lambda: "abc123456789")
    monkeypatch.setattr(paths, "MEDIC_DIR", tmp_path)
    monkeypatch.setattr(paths, "CASES", tmp_path / "cases.jsonl")
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: None)
    state: dict = {}
    record = canary.run(state, log=lambda *a: None)
    assert "pass_rate" not in record and "error" in record
    assert state.get("canary_baseline") is None
