"""Evidence collection from canned neatlogs spans: no network, no models."""

import json

import pytest

from medic import evidence, paths
from medic.evidence import TraceEvidence, window


class Reader:
    """Canned spans and payloads in the shapes evidence.collect expects."""

    def __init__(self, spans, payloads, fail_on=()):
        self._spans = spans
        self._payloads = payloads
        self._fail_on = set(fail_on)
        self.payload_calls = 0

    def spans(self, trace_id):
        if trace_id in self._fail_on:
            raise RuntimeError("neatlogs hiccup")
        return self._spans

    def payload(self, trace_id, span_id, fields=""):
        self.payload_calls += 1
        return self._payloads[span_id]


def trace(trace_id="t1", finalized=True):
    return {"traceId": trace_id, "startedAt": "2026-10-10T00:00:00Z", "tags": ["release:r1"],
            "status": "OK", "totalCostUsd": 0.0, "totalTokens": 100, "toolCallsCount": 3,
            "durationMs": 10.0,
            "finalizationStatus": "finalized" if finalized else "running"}


def reader():
    tool_ok = {"spanId": "s1", "spanType": "TOOL", "spanName": "read_doc",
               "status": "OK", "startedAt": "2026-10-10T00:00:01Z"}
    tool_bad = {"spanId": "s2", "spanType": "TOOL", "spanName": "read_doc",
                "status": "ERROR", "startedAt": "2026-10-10T00:00:02Z"}
    agent = {"spanId": "s3", "spanType": "AGENT", "spanName": "agent",
             "status": "OK", "startedAt": "2026-10-10T00:00:03Z"}
    payloads = {
        "s1": {"input": json.dumps({"args": {"query": "pilot plant"}}), "output": "docs"},
        "s2": {"input": json.dumps({"args": {"query": "pilot plant"}}),
               "error": json.dumps({"type": "TypeError", "message": "string indices must be integers"})},
        "s3": {"input": json.dumps({"question": "Where is the pilot plant?"}),
               "output": json.dumps({"answer": "It is in Narvik."})},
    }
    return Reader([tool_ok, tool_bad, agent], payloads)


def test_collect_builds_errors_qa_and_repeats(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "EVIDENCE", tmp_path / "evidence")
    ev = evidence.collect(reader(), trace())
    assert ev.tool_errors == [{"tool": "read_doc", "type": "TypeError",
                               "message": "string indices must be integers",
                               "input": json.dumps({"args": {"query": "pilot plant"}})}]
    assert ev.question == "Where is the pilot plant?"
    assert ev.answer == "It is in Narvik."
    # Both tool calls used the identical input.
    assert ev.repeated_calls == 1
    assert not ev.failed


def test_collect_caches_only_finalized(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "EVIDENCE", tmp_path / "evidence")
    evidence.collect(reader(), trace("t1"))
    assert (tmp_path / "evidence" / "t1.json").exists()
    evidence.collect(reader(), trace("t2", finalized=False))
    assert not (tmp_path / "evidence" / "t2.json").exists()
    # A cached trace never touches the reader again.
    dead = Reader([], {}, fail_on=("t1",))
    cached = evidence.collect(dead, trace("t1"))
    assert cached.answer == "It is in Narvik."


def ev(**kw):
    base = dict(trace_id="t", started_at="2026-10-10T00:00:00Z", release="r1", status="OK",
                cost_usd=0.0, tokens=100, tool_calls=3, duration_ms=10,
                question="Where is the pilot plant?", answer="It is in Narvik.")
    return TraceEvidence(**{**base, **kw})


@pytest.mark.parametrize("answer", ["I could not find it.", "No information available.", ""])
def test_non_answer_phrases_fail(answer):
    assert ev(answer=answer).non_answer
    assert ev(answer=answer).failed


def test_error_status_fails_despite_answer():
    assert ev(status="ERROR").failed


def test_healthy_trace_passes():
    assert not ev().failed and not ev().non_answer


def test_window_metrics():
    w = window([ev(), ev(status="ERROR", tool_errors=[{"tool": "x"}]),
                ev(answer="I could not find it.")])
    assert w["n"] == 3
    assert w["failure_rate"] == pytest.approx(2 / 3)
    assert w["tool_error_rate"] == pytest.approx(1 / 3)
    assert w["non_answer_rate"] == pytest.approx(1 / 3)
    assert w["tokens_per_run"] == 100.0 and w["tool_calls_per_run"] == 3.0
    assert window([]) == {"n": 0}


def test_window_averages_across_traces_and_counts_loops():
    w = window([ev(tokens=100, tool_calls=2), ev(tokens=300, tool_calls=4, repeated_calls=2),
                ev(tokens=500, tool_calls=9, repeated_calls=1), ev(tokens=700, tool_calls=1)])
    assert w["tokens_per_run"] == pytest.approx(400.0)        # not the first trace's 100
    assert w["tool_calls_per_run"] == pytest.approx(4.0)
    assert w["repeated_call_rate"] == pytest.approx(0.5)      # share of traces that repeated a call
