"""Fault localization on the step patterns of real failures seen in the benchmark."""

from medic.evidence import TraceEvidence
from medic.localize import localize


def trace(i, steps, answer="Narvik, 412 MWh", status="OK"):
    return TraceEvidence(trace_id=f"t{i}", started_at="2026-10-10T00:00:00Z", release="r", status=status,
                         cost_usd=0, tokens=100, tool_calls=sum(s["type"] == "TOOL" for s in steps),
                         duration_ms=1, question="q", answer=answer, steps=steps)


def tool(name, args, output_len=600, error_type="", error=""):
    return {"type": "TOOL", "name": name, "status": "ERROR" if error_type else "OK", "arg_keys": args,
            "input": "", "output_len": output_len, "error_type": error_type, "error": error}


def llm(model="gpt-oss:20b", error_type="", error=""):
    return {"type": "LLM", "name": model, "status": "ERROR" if error_type else "OK", "model": model,
            "tokens": 500, "error_type": error_type, "error": error}


HEALTHY = [trace(i, [llm(), tool("search_docs", ["query"], 300), llm(), tool("read_doc", ["doc_id"], 640), llm()])
           for i in range(6)]


def test_schema_rename_points_at_schema_and_handler():
    failing = [trace(i, [llm(), tool("search_docs", ["q"], error_type="KeyError", error="'query'"), llm()],
                     answer="I could not retrieve the documents", status="ERROR") for i in range(6)]
    r = localize(failing, HEALTHY)
    kinds = [d["kind"] for d in r["divergences"]]
    assert r["verdict"] == "step_error" and "arguments_changed" in kinds
    assert {"kind": "tool_schema", "name": "search_docs"} in [{k: s[k] for k in ("kind", "name")} for s in r["suspects"]]
    assert "['q']" in next(d for d in r["divergences"] if d["kind"] == "arguments_changed")["detail"]


def test_truncated_tool_output_is_found_without_any_error():
    failing = [trace(i, [llm(), tool("search_docs", ["query"], 300), llm(), tool("read_doc", ["doc_id"], 300), llm()],
                     answer="The document does not provide that") for i in range(6)]
    r = localize(failing, HEALTHY)
    assert r["verdict"] == "output_changed"
    assert r["suspects"][0] == {**r["suspects"][0], "kind": "tool_handler", "name": "read_doc"}


def test_redundant_calls_point_at_the_prompt():
    doubled = [llm(), tool("search_docs", ["query"], 300), llm(), tool("search_docs", ["query"], 300),
               llm(), tool("read_doc", ["doc_id"], 640), llm(), tool("read_doc", ["doc_id"], 640), llm()]
    failing = [trace(i, doubled) for i in range(6)]
    for e in failing:
        e.repeated_calls = 2
    r = localize(failing, HEALTHY)
    assert r["verdict"] == "extra_calls"
    assert r["suspects"][0]["kind"] == "prompt"


def test_retired_model_is_an_llm_step_error():
    failing = [trace(i, [llm("gemini-2.5-flash", "ClientError", "404 NOT_FOUND no longer available")],
                     answer="", status="ERROR") for i in range(2)]
    healthy = [trace(i, [llm("gemini-2.5-flash"), tool("web_search", ["query"], 900), llm("gemini-2.5-flash")])
               for i in range(3)]
    r = localize(failing, healthy)
    assert r["verdict"] == "step_error" and r["divergences"][0]["step"] == "LLM gemini-2.5-flash"
    assert r["suspects"][0]["kind"] == "model_config"


def test_no_steps_is_reported_not_guessed():
    assert localize([trace(0, [])], HEALTHY)["verdict"] == "insufficient_steps"
