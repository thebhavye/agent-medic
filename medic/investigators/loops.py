"""Loop investigator: is the agent repeating work?

Reads each trace's tool calls from the spans and finds calls identical to an earlier call in
the same trace, then compares calls per run with the healthy baseline.
"""

from collections import Counter

from medic.evidence import TraceEvidence


def investigate(evs: list[TraceEvidence], baseline: dict) -> dict:
    repeated = Counter()
    for e in evs:
        seen = Counter(tuple(c) for c in e.tool_inputs)
        for (tool, _), n in seen.items():
            if n > 1:
                repeated[tool] += n - 1
    calls = sum(e.tool_calls for e in evs) / max(len(evs), 1)
    base_calls = baseline.get("tool_calls_per_run", 0) or 0
    ratio = calls / base_calls if base_calls else 0
    looping = [e for e in evs if e.repeated_calls]
    finding = {"investigator": "loops", "tool_calls_per_run": calls, "baseline_tool_calls_per_run": base_calls,
               "repeated_by_tool": dict(repeated), "traces_with_repeats": len(looping)}
    if looping and ratio >= 1.4:
        top = repeated.most_common(1)[0][0] if repeated else "a tool"
        return {**finding, "verdict": "redundant_calls", "confidence": 0.8,
                "detail": f"{len(looping)}/{len(evs)} traces repeat identical calls (mostly {top}); "
                          f"{calls:.1f} tool calls per run vs {base_calls:.1f} at baseline"}
    if ratio >= 1.5:
        return {**finding, "verdict": "more_calls", "confidence": 0.6,
                "detail": f"{calls:.1f} tool calls per run vs {base_calls:.1f} at baseline, without exact repeats"}
    return {**finding, "verdict": "no_loop", "confidence": 0.7, "detail": "no repeated or extra tool calls"}
