"""Triage: group the failing traces by symptom and choose who investigates.

Symptoms come straight from span data: which tool raised what, whether the agent gave a
non-answer, whether it repeated identical tool calls, how many calls and tokens it used.
"""

from collections import Counter

from medic.evidence import TraceEvidence


def symptom(e: TraceEvidence) -> tuple:
    if e.tool_errors:
        first = e.tool_errors[0]
        return ("tool_error", first["tool"], first["type"])
    if e.non_answer:
        return ("non_answer",)
    if e.repeated_calls:
        return ("repeated_calls",)
    return ("healthy",)


def triage(evs: list[TraceEvidence], signals: list[dict], release_changed: bool) -> dict:
    counts = Counter(symptom(e) for e in evs)
    unhealthy = [(s, n) for s, n in counts.most_common() if s[0] != "healthy"]
    dominant = unhealthy[0][0] if unhealthy else ("healthy",)
    names = {s["name"] for s in signals}

    investigators = []
    if dominant[0] in ("tool_error", "non_answer") or "quality" in names:
        investigators.append("dependency")
    if names & {"loops", "tool_calls", "cost"} or dominant[0] == "repeated_calls":
        investigators.append("loops")
    # Always ask whether the code changed; with no new release it answers "no" instantly.
    investigators.append("regression")

    examples = []
    for e in evs:
        if symptom(e) == dominant:
            examples.append({"trace_id": e.trace_id, "question": e.question, "answer": e.answer[:300],
                             "tool_errors": e.tool_errors[:2], "tool_calls": e.tool_calls,
                             "repeated_calls": e.repeated_calls})
    summary = {
        "tool_error": lambda: f"{counts[dominant]}/{len(evs)} traces fail in {dominant[1]} with {dominant[2]}",
        "non_answer": lambda: f"{counts[dominant]}/{len(evs)} traces end in a non-answer with no tool error",
        "repeated_calls": lambda: f"{counts[dominant]}/{len(evs)} traces repeat identical tool calls",
        "healthy": lambda: "traces complete normally but use more resources",
    }[dominant[0]]()
    return {
        "summary": summary,
        "symptoms": [{"symptom": list(s), "traces": n} for s, n in counts.most_common()],
        "dominant": list(dominant),
        "release_changed": release_changed,
        "investigators": investigators,
        "examples": examples[:4],
    }
