"""Watcher rules and polling against a fake reader and a temp deploy repo."""

import subprocess

import pytest

from medic import contract, paths, watcher
from medic.evidence import TraceEvidence
from medic.watcher import Watcher, compare


@pytest.mark.parametrize("metric,threshold", [
    ("tool_error_rate", 0.25), ("failure_rate", 0.25), ("repeated_call_rate", 0.5)])
def test_abs_rules_fire_at_threshold(metric, threshold):
    assert [s.name for s in compare({metric: 0.0}, {metric: threshold})]
    assert compare({metric: 0.1}, {metric: 0.1 + threshold - 0.01}) == []


@pytest.mark.parametrize("metric", ["tool_calls_per_run", "tokens_per_run"])
def test_ratio_rules_fire_at_threshold(metric):
    assert [s.name for s in compare({metric: 100.0}, {metric: 150.0})]
    assert compare({metric: 100.0}, {metric: 149.9}) == []


def test_zero_baseline_never_divides():
    assert compare({"tokens_per_run": 0}, {"tokens_per_run": 10000.0}) == []
    assert compare({"tool_calls_per_run": 0}, {"tool_calls_per_run": 50.0}) == []
    assert [s.name for s in compare({"failure_rate": 0.0}, {"failure_rate": 1.0})] == ["failures"]


class Reader:
    def __init__(self, traces, fail_on=()):
        self._traces = traces
        self._fail_on = set(fail_on)

    def list_traces(self, **kw):
        return self._traces

    def spans(self, trace_id):
        if trace_id in self._fail_on:
            raise RuntimeError("neatlogs hiccup")
        return []

    def payload(self, trace_id, span_id, fields=""):
        raise AssertionError("no spans, so no payloads")


def git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "seed.txt").write_text("seed")
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True,
                          capture_output=True, text=True).stdout.strip()


def setup(tmp_path, monkeypatch):
    sha = git_repo(tmp_path / "deploy")
    monkeypatch.setattr(paths, "DEPLOY", tmp_path / "deploy")
    monkeypatch.setattr(paths, "STATE", tmp_path / "medic" / "state.json")
    monkeypatch.setattr(paths, "EVIDENCE", tmp_path / "medic" / "evidence")
    return sha


def trace(trace_id, sha, minute, tags=None, finalized=True):
    return {"traceId": trace_id, "startedAt": f"2026-10-10T00:{minute:02d}:00Z",
            "tags": tags if tags is not None else ["env:prod", f"release:{sha}"],
            "status": "OK", "totalCostUsd": 0.0, "totalTokens": 100, "toolCallsCount": 1,
            "durationMs": 10.0, "finalizationStatus": "finalized" if finalized else "running"}


def test_poll_takes_only_finalized_prod_traces_from_known_releases(tmp_path, monkeypatch):
    sha = setup(tmp_path, monkeypatch)
    reader = Reader([
        trace("good", sha, 1),
        trace("nonprod", sha, 2, tags=[f"release:{sha}"]),
        trace("running", sha, 3, finalized=False),
        trace("foreign", "0" * 40, 4),
    ])
    watcher_ = Watcher(reader, log=lambda *a: None)
    assert watcher_.poll() == 1
    assert [t["trace_id"] for t in watcher_.state["traces"]] == ["good"]
    assert watcher_.poll() == 0  # already seen


def test_poll_caps_reads_and_survives_a_bad_trace(tmp_path, monkeypatch):
    sha = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(watcher, "MAX_NEW", 2)
    traces = [trace(f"t{i}", sha, i) for i in range(1, 5)]
    reader = Reader(traces, fail_on=("t4",))
    watcher_ = Watcher(reader, log=lambda *a: None)
    assert watcher_.poll() == 1  # newest two read, one of them unreadable
    assert [t["trace_id"] for t in watcher_.state["traces"]] == ["t3"]
    assert sorted(watcher_.state["skipped"]) == ["t1", "t2"]


def ev(**kw):
    base = dict(trace_id="t", started_at="2026-10-10T00:00:00Z", release="r1", status="OK",
                cost_usd=0.0, tokens=100, tool_calls=3, duration_ms=10,
                question="Where is the pilot plant?", answer="It is in Narvik.")
    return TraceEvidence(**{**base, **kw})


def seed(monkeypatch, tmp_path, traces):
    setup(tmp_path, monkeypatch)
    monkeypatch.setattr(contract, "snapshot", lambda *a: {"probe": "fake"})
    watcher_ = Watcher(Reader([]), log=lambda *a: None)
    watcher_.state["traces"] = [e.to_dict() for e in traces]
    return watcher_


def test_check_freezes_baseline_only_from_healthy(tmp_path, monkeypatch):
    watcher_ = seed(monkeypatch, tmp_path, [ev(trace_id=f"h{i}") for i in range(6)])
    signals, current = watcher_.check()
    assert signals == [] and current["n"] == 6
    assert watcher_.state["baseline"]["failure_rate"] == 0.0
    assert watcher_.state["healthy_release"] == "r1"


def test_check_ignores_unhealthy_window_for_baseline(tmp_path, monkeypatch):
    watcher_ = seed(monkeypatch, tmp_path, [ev(trace_id=f"b{i}", status="ERROR") for i in range(6)])
    signals, current = watcher_.check()
    assert signals == [] and watcher_.state["baseline"] is None


def test_check_returns_signals_after_baseline(tmp_path, monkeypatch):
    watcher_ = seed(monkeypatch, tmp_path, [ev(trace_id=f"h{i}") for i in range(6)])
    watcher_.check()
    watcher_.state["traces"] = [ev(trace_id=f"b{i}", status="ERROR",
                                    answer="I could not find it.",
                                    tool_errors=[{"tool": "search_docs"}]).to_dict() for i in range(6)]
    signals, _current = watcher_.check()
    assert {s.name for s in signals} >= {"failures", "tool_errors"}


def test_check_needs_a_full_window(tmp_path, monkeypatch):
    watcher_ = seed(monkeypatch, tmp_path, [ev()])
    assert watcher_.check() == ([], {})
