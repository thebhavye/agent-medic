"""Unit economics on a fake MEDIC_DIR: aggregation, attribution, honesty."""

import json
from pathlib import Path

import pytest

from scripts.unit_economics import (
    collect_incidents,
    eval_files_for,
    fmt_seconds,
    load_evidence_cache,
    load_traces,
    main,
    measure_row,
    median,
    median_incident_cost,
    median_or_none,
    neatlogs_calls,
    p90,
    p90_or_none,
    render_report,
    sum_eval_tokens,
    sum_fixer_usage,
    timing,
    token_cost_usd,
    unattributed_shared_evals,
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


def eval_file(path: Path, cases):
    path.write_text(json.dumps({"session": "s", "cases": cases}), encoding="utf-8")


def case(case_id="q1", in_tok=100, out_tok=20):
    return {"id": case_id, "input_tokens": in_tok, "output_tokens": out_tok}


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
    eval_file(results / "verify-a-1.json", [case("q1"), case("q2", 300, 40)])
    eval_file(results / "zz.json", [case("q1", 10, 1)])

    assert sum_fixer_usage(attempts) == (3000, 600)
    assert sum_fixer_usage([]) == (0, 0)
    assert sum_eval_tokens([results / "verify-a-1.json"]) == (1, 2, 400, 60)
    assert token_cost_usd(1_000_000, 0, {"in_per_m": 4.0, "out_per_m": 20.0}) == 4.0
    assert median([1.0, 2.0, 3.0]) == 2.0 and p90([1.0]) == 1.0
    assert median_or_none([]) is None and p90_or_none([None]) is None
    assert fmt_seconds(None) == "Unknown" and fmt_seconds(5.4) == "5s"

    rows = collect_incidents(medic)
    assert len(rows) == 2  # resolved + escalated, nothing else in the fixture
    live_traces = load_traces(medic / "state.json")
    by_id = {r["data"]["id"]: r for r in rows}

    files, _note = eval_files_for(medic, by_id["a"])
    assert [p.name for p in files] == ["verify-a-1.json"]  # zz stays unattributed
    row = measure_row(medic, by_id["a"], live_traces, {})
    assert (row["fixer_input_tokens"], row["fixer_output_tokens"]) == (3000, 600)
    assert (row["eval_runs"], row["eval_cases"]) == (1, 2)
    assert (row["eval_input_tokens"], row["eval_output_tokens"]) == (400, 60)
    # Live failure start is unknown: no detection time, resolve measured from open.
    assert row["detect_s"] is None
    assert row["resolve_from_open_s"] == 300.0
    assert row["resolve_from_fault_s"] is None
    # 1 span list + 3 tool payloads + 1 agent payload.
    assert row["neatlogs_api_calls_estimate"] == 5
    assert neatlogs_calls(["missing"], {}) is None  # unknown, not zero
    assert neatlogs_calls([], {}) == 0  # nothing attributable: genuine zero

    escalated = measure_row(medic, by_id["b"], live_traces, {})
    assert escalated["fix_attempts"] == 0 and escalated["eval_runs"] == 0


def row_for(fixer_in, fixer_out, eval_in, eval_out):
    return {"fixer_input_tokens": fixer_in, "fixer_output_tokens": fixer_out,
            "eval_input_tokens": eval_in, "eval_output_tokens": eval_out}


def test_median_cost_uses_independent_streams():
    # Fixer median comes from row B, eval median from row A: no single incident
    # holds all four medians.
    rows = [row_for(100, 10, 50000, 5000),
            row_for(1500, 150, 100, 10),
            row_for(90000, 9000, 200, 20)]
    fixer = {"in_per_m": 4.0, "out_per_m": 20.0}
    eval_price = {"in_per_m": 0.1, "out_per_m": 0.5}
    expected = token_cost_usd(1500, 150, fixer) + token_cost_usd(200, 20, eval_price)
    assert median_incident_cost(rows, fixer, eval_price) == expected


def test_live_attribution_uses_investigated_commits(tmp_path):
    medic = tmp_path / "medic"
    (medic / "incidents").mkdir(parents=True)
    results = medic / "deploy" / ".bisect" / "results"
    results.mkdir(parents=True)
    good = "0" * 40
    incident(medic / "incidents" / "a.json", "a", findings={"regression": {
        "verdict": "no_code_change", "good": good, "bad": good}})
    (medic / "state.json").write_text(json.dumps({"traces": []}), encoding="utf-8")
    eval_file(results / "verify-a-1.json", [case("q1")])
    eval_file(results / (good[:12] + ".json"), [case("q2", 50, 5)])
    eval_file(results / "other.json", [case("q3", 1, 1)])

    by_id = {r["data"]["id"]: r for r in collect_incidents(medic)}
    files, _note = eval_files_for(medic, by_id["a"])
    assert sorted(p.name for p in files) == [good[:12] + ".json", "verify-a-1.json"]
    row = measure_row(medic, by_id["a"], {}, {})
    assert (row["eval_runs"], row["eval_cases"]) == (2, 2)
    assert (row["eval_input_tokens"], row["eval_output_tokens"]) == (150, 25)
    # The other file is counted nowhere.
    assert [p.name for p in unattributed_shared_evals(medic, collect_incidents(medic))] == ["other.json"]


def test_timing_known_and_unknown():
    data = {"opened_at": 1000.0,
            "timeline": [{"t": 1000.0, "phase": "open"},
                         {"t": 1300.0, "phase": "resolved"}]}
    assert timing(data, {"fault_at": 900.0, "detect_s": 48, "resolve_s": 400}) == (48, 300.0, 400)
    assert timing(data, None) == (None, 300.0, None)
    assert timing({"opened_at": None, "timeline": []}, None) == (None, None, None)


def test_traces_recovered_from_cache(tmp_path):
    medic = tmp_path / "medic"
    (medic / "incidents").mkdir(parents=True)
    (medic / "evidence").mkdir(parents=True)
    incident(medic / "incidents" / "a.json", "a", trace_ids=["t1", "t2", "t3"])
    (medic / "state.json").write_text(json.dumps({
        "traces": [{"trace_id": "t1", "tool_calls": 3}]}), encoding="utf-8")
    (medic / "evidence" / "t2.json").write_text(json.dumps(
        {"trace_id": "t2", "tool_calls": 5}), encoding="utf-8")
    by_id = {r["data"]["id"]: r for r in collect_incidents(medic)}
    cache = load_evidence_cache(medic / "evidence")
    assert set(cache) == {"t2"}
    # t3 is in neither state nor cache: unknown, not zero.
    assert measure_row(medic, by_id["a"], load_traces(medic / "state.json"), cache)[
        "neatlogs_api_calls_estimate"] is None
    by_id["a"]["data"]["trace_ids"] = ["t1", "t2"]
    assert measure_row(medic, by_id["a"], load_traces(medic / "state.json"), cache)[
        "neatlogs_api_calls_estimate"] == (1 + 3 + 1) + (1 + 5 + 1)


def test_empty_datasets_write_consistent_empty_outputs(tmp_path):
    out = tmp_path / "out"
    assert main(["--medic-dir", str(tmp_path / "missing"), "--out-dir", str(out)]) == 0
    assert main(["--medic-dir", str(tmp_path), "--out-dir", str(out)]) == 0
    medic = tmp_path / "medic"
    (medic / "incidents").mkdir(parents=True)
    incident(medic / "incidents" / "open.json", "o", phase="open")
    assert main(["--medic-dir", str(medic), "--out-dir", str(out)]) == 0
    header = (out / "measured.csv").read_text(encoding="utf-8").strip().splitlines()
    assert len(header) == 1 and header[0].startswith("source,incident")
    report = (out / "unit_economics.md").read_text(encoding="utf-8")
    assert "no terminal incidents" in report


@pytest.mark.parametrize("value", ["0", "-5", "nan", "inf", "-inf"])
def test_canary_minutes_rejected(tmp_path, value):
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as exc:
        main(["--medic-dir", str(tmp_path), "--out-dir", str(out),
              "--canary-minutes", value])
    assert exc.value.code == 2
    assert not out.exists()  # nothing written on validation failure


def test_canary_minutes_valid(tmp_path):
    out = tmp_path / "out"
    assert main(["--medic-dir", str(tmp_path), "--out-dir", str(out),
                 "--canary-minutes", "5"]) == 0
    assert (out / "measured.csv").exists()


def test_report_renders_unknown_timings(tmp_path):
    rows = [dict(row_for(100, 10, 200, 20), source="live", incident="a",
                 scenario_or_target="", cause="code_regression", outcome="resolved",
                 detect_s=None, resolve_from_open_s=300.0, resolve_from_fault_s=None,
                 fix_attempts=1, eval_runs=1, eval_cases=1)]
    text, _summary = render_report(tmp_path, rows, [], 10)
    assert "Unknown" in text
    assert "resolve from incident open: median 300s" in text
