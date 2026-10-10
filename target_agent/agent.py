"""The research agent: a manual tool-use loop over the Halcyon knowledge base."""

import json
import os
import time

import neatlogs

import medic_runtime as medic
from target_agent import llm
from target_agent.tools import TOOL_HANDLERS, TOOL_SCHEMAS

MODEL = llm.model_for("target")
EFFORT = os.environ.get("TARGET_EFFORT", "low")
MAX_TURNS = 12

SYSTEM = (
    "You are a research assistant for the Halcyon Grid knowledge base. Answer only from "
    "facts in the knowledge base. Use search_docs to find relevant documents, then read_doc "
    "to read them in full before answering, because search snippets omit the details. "
    "Finish with a concise answer that states the specific names and figures asked for."
)


def _traced(name, handler):
    # One TOOL span per call, named after the tool, so Medic can read each call's input,
    # output and error back from neatlogs.
    @neatlogs.span(kind="TOOL", name=name, tool_name=name)
    def run(args: dict) -> str:
        result = handler(args)
        return result if isinstance(result, str) else json.dumps(result)
    return run


TRACED_TOOLS = {name: _traced(name, handler) for name, handler in TOOL_HANDLERS.items()}


def execute_tool(name: str, args: dict) -> str:
    # Through Medic's runtime: breakers for a dead dependency, and a guard against loops.
    return medic.guard_tool(name, args, lambda: TRACED_TOOLS[name](args))


@neatlogs.span(kind="AGENT", name="research_agent")
def run_agent(question: str, provider) -> dict:
    with medic.request():           # one request: the loop guard's scope
        return _answer(question, provider)


def _answer(question: str, provider) -> dict:
    transcript = [{"role": "user", "content": question}]
    stats = {"turns": 0, "tool_calls": 0, "tool_errors": 0, "input_tokens": 0, "output_tokens": 0}
    started = time.monotonic()

    def finish(status: str, answer: str = "", error: str = "") -> dict:
        return {
            "status": status,
            "answer": answer,
            "error": error,
            **stats,
            "cost_usd": llm.cost_usd(MODEL, stats["input_tokens"], stats["output_tokens"]),
            "duration_s": round(time.monotonic() - started, 2),
        }

    while stats["turns"] < MAX_TURNS:
        stats["turns"] += 1
        # The prompt and tool schemas are read through the runtime, so during an incident Medic
        # can pin their last healthy versions; an unavailable model falls back.
        turn = medic.call_model(lambda model: provider.chat(
            model, medic.value("system_prompt", SYSTEM), transcript, medic.value("tool_schemas", TOOL_SCHEMAS),
            effort=EFFORT), MODEL)
        stats["input_tokens"] += turn.input_tokens
        stats["output_tokens"] += turn.output_tokens

        if turn.stop == "refusal":
            return finish("refused", error="model declined the request")
        if turn.stop == "max_tokens":
            return finish("error", error="hit max_tokens")
        if not turn.tool_calls:
            return finish("ok", answer=medic.answer(turn.text))   # a non-answer counts as a failed request

        transcript.append({"role": "assistant", "text": turn.text, "tool_calls": turn.tool_calls, "raw": turn.raw})
        for call in turn.tool_calls:
            stats["tool_calls"] += 1
            try:
                content, is_error = execute_tool(call["name"], call["args"]), False
            except Exception as exc:  # tool failures go back to the model, as a real agent would
                stats["tool_errors"] += 1
                content, is_error = f"{type(exc).__name__}: {exc}", True
            transcript.append({"role": "tool", "id": call["id"], "name": call["name"],
                               "content": content, "is_error": is_error})

    return finish("error", error=f"no answer after {MAX_TURNS} turns")
