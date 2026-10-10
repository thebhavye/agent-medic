"""Token aggregation for unit economics on a fake MEDIC_DIR."""

import json
from pathlib import Path

from scripts.unit_economics import (
    collect_incidents,
    eval_files_for,
    load_traces,
    measure_row,
    median,
    neatlogs_calls,
    p90,
    sum_eval_tokens,
    sum_fixer_usage,
    token_cost_usd,
)


def incident(path: Path, incident_id: str, **kw):
    data = {"id": incident_id, "opened_at": 1000.0, "phase": "resolved",
            "signals": [], "release": "aaa", "healthy_release": "aaa", "trace_ids": ["t1"],
            "decision": {"cause": "code_regression"}, "remedy": {"attempts": kw.pop("attempts", [])},
            "timeline": [{"t": 1000.0, "phase": "open", "msg": "o"},
                         {"t": 1060.0, "phase": "triaged", "msg": "t"},
                         {"t": 1300.0, "phase": "resolved", "msg": "r"}],
            **kw}
    path.write_text(json.dumps(data), encoding="utf-8")


def test_token_aggregation(tmp_path):
    medic = tmp_path / "medic"
    (medic / "incidents").mkdir(parents=True)
    results = medic / "deploy" / ".bisect" / "results"
    results.mkdir(parents=True)
    attempts = [{"usage": {"model": "m", "input_tokens": 1000, "output_tokens": 500}},
                {"usage": {"model": "m", "input_tokens": 2000, "output_tokens": 100}}]
    incident(medic / "incidents" / "a.json", "a", attempts=attempts)
    incident(medic / "incidents" / "b.json", "b", phase="escalated", attempts=[])
    (medic / "state.json").write_text(json.dumps({
        "traces": [{"trace_id": "t1", "tool_calls": 3}]}), encoding="utf-8")
    (results / "verify-a-1.json").write_text(json.dumps({
        "session": "s", "cases": [
            {"id": "q1", "input_tokens": 100, "output_tokens": 20},
            {"id": "q2", "input_tokens": 300, "output_tokens": 40}]}), encoding="utf-8")
    (results / "zz.json").write_text(json.dumps({
        "session": "s", "cases": [{"id": "q1", "input_tokens": 10, "output_tokens": 1}]}),
        encoding="utf-8")

    assert sum_fixer_usage(attempts) == (3000, 600)
    assert sum_fixer_usage([]) == (0, 0)
    assert sum_eval_tokens([results / "verify-a-1.json"]) == (1, 2, 400, 60)
    assert token_cost_usd(1_000_000, 0, {"in_per_m": 4.0, "out_per_m": 20.0}) == 4.0
    assert median([1.0, 2.0, 3.0]) == 2.0 and p90([1.0]) == 1.0

    rows = collect_incidents(medic)
    assert len(rows) == 2  # resolved + escalated, nothing else in the fixture
    live_traces = load_traces(medic / "state.json")
    by_id = {r["data"]["id"]: r for r in rows}

    files, _note = eval_files_for(medic, by_id["a"])
    assert [p.name for p in files] == ["verify-a-1.json"]  # baseline stays unattributed
    row = measure_row(medic, by_id["a"], live_traces)
    assert (row["fixer_input_tokens"], row["fixer_output_tokens"]) == (3000, 600)
    assert (row["eval_runs"], row["eval_cases"]) == (1, 2)
    assert (row["eval_input_tokens"], row["eval_output_tokens"]) == (400, 60)
    assert row["detect_s"] == 60.0 and row["resolve_s"] == 300.0
    # 1 span list + 3 tool payloads + 1 agent payload.
    assert row["neatlogs_api_calls_estimate"] == 5
    assert neatlogs_calls(["missing"], live_traces) == 0

    escalated = measure_row(medic, by_id["b"], live_traces)
    assert escalated["fix_attempts"] == 0 and escalated["eval_runs"] == 0
