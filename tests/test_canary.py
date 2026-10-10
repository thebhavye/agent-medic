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
    ok = {"canary_baseline": 1.0, "canary": [{"at": 1.0, "pass_rate": 0.9}]}
    assert canary.regressed(ok) is None  # 0.10 drop < DROP (0.25)
    bad = {"canary_baseline": 1.0, "canary": [{"at": 1.0, "pass_rate": 0.75}]}
    signal = canary.regressed(bad)  # exactly DROP counts
    assert signal == {"name": "quality", "metric": "canary_pass_rate",
                      "baseline": 1.0, "current": 0.75}


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
