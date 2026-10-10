"""Turn a neatlogs trace into the evidence Medic reasons over.

For each production trace Medic reads its spans and the payloads that matter: every tool
call's input and error, and the agent's question and answer. Evidence is cached on disk, so
each trace is fetched once.
"""

import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from medic import paths

NON_ANSWER = re.compile(r"could not|couldn't|unable to|does not (provide|contain|have|include)|"
                        r"no information|not available|no documents|cannot find|can't find", re.I)


@dataclass
class TraceEvidence:
    trace_id: str
    started_at: str
    release: str
    status: str
    cost_usd: float
    tokens: int
    tool_calls: int
    duration_ms: float
    question: str = ""
    answer: str = ""
    tool_errors: list = field(default_factory=list)    # [{"tool", "type", "message", "input"}]
    tool_inputs: list = field(default_factory=list)    # [[tool, input]] for every call
    repeated_calls: int = 0                            # calls identical to an earlier call in the trace

    @property
    def non_answer(self) -> bool:
        return not self.answer or bool(NON_ANSWER.search(self.answer))

    @property
    def failed(self) -> bool:
        return self.status.upper() == "ERROR" or self.non_answer

    def to_dict(self) -> dict:
        return asdict(self)


def _release(tags: list[str]) -> str:
    return next((t.split(":", 1)[1] for t in tags if t.startswith("release:")), "")


def collect(reader, trace: dict) -> TraceEvidence:
    cache = paths.EVIDENCE / f"{trace['traceId']}.json"
    if cache.exists():
        return TraceEvidence(**json.loads(cache.read_text(encoding="utf-8")))

    ev = TraceEvidence(trace_id=trace["traceId"], started_at=trace["startedAt"], release=_release(trace["tags"]),
                       status=trace["status"], cost_usd=trace["totalCostUsd"], tokens=trace["totalTokens"],
                       tool_calls=trace["toolCallsCount"], duration_ms=trace["durationMs"])
    spans = sorted(reader.spans(trace["traceId"]), key=lambda s: s["startedAt"])
    wanted = [s for s in spans if s["spanType"] in ("TOOL", "AGENT")]
    # Payloads are one request each; fetch them in parallel (span order is kept).
    with ThreadPoolExecutor(max_workers=4) as pool:
        payloads = list(pool.map(
            lambda s: reader.payload(trace["traceId"], s["spanId"],
                                     fields="error,input" if s["spanType"] == "TOOL" else "input,output"), wanted))
    for span, p in zip(wanted, payloads):
        if span["spanType"] == "TOOL":
            ev.tool_inputs.append([span["spanName"], p.get("input") or ""])
            if span["status"].upper() == "ERROR":
                err = _json(p.get("error")) or {}
                ev.tool_errors.append({"tool": span["spanName"], "type": err.get("type", "Error"),
                                       "message": err.get("message", p.get("error") or ""),
                                       "input": p.get("input") or ""})
        else:
            ev.question = (_json(p.get("input")) or {}).get("question", "")
            ev.answer = (_json(p.get("output")) or {}).get("answer", "")
    counts = Counter(tuple(c) for c in ev.tool_inputs)
    ev.repeated_calls = sum(n - 1 for n in counts.values())

    if trace.get("finalizationStatus") == "finalized":
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(ev.to_dict()), encoding="utf-8")
    return ev


def _json(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def window(evs: list[TraceEvidence]) -> dict:
    """Aggregate metrics for a window of traces."""
    n = len(evs)
    if not n:
        return {"n": 0}
    return {
        "n": n,
        "failure_rate": sum(e.failed for e in evs) / n,
        "tool_error_rate": sum(bool(e.tool_errors) for e in evs) / n,
        "non_answer_rate": sum(e.non_answer for e in evs) / n,
        "cost_per_run": sum(e.cost_usd for e in evs) / n,
        "tokens_per_run": sum(e.tokens for e in evs) / n,
        "tool_calls_per_run": sum(e.tool_calls for e in evs) / n,
        "repeated_call_rate": sum(e.repeated_calls > 0 for e in evs) / n,
        "releases": sorted({e.release for e in evs}),
    }
