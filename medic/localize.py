"""Fault localization: where, step by step, do failing traces stop looking like healthy ones?

Each trace's evidence lists its LLM and tool steps in order (from neatlogs spans and
payloads). This module profiles every step of the healthy traces (how often it runs, with
which argument names, how much output it returns, how often it errors) and compares the
failing traces against that profile. Every divergence it finds names the parts of the agent
that could cause it, so the fixer is pointed at a tool handler, a schema, a prompt or the
model config, not at "the code".

Divergences, strongest evidence first:
    step_error           a step that was healthy now raises (which step, which error)
    arguments_changed    a tool is called with different argument names than before
    output_changed       a tool returns much less (or more) than it used to
    extra_calls          a step runs far more often per trace (repeats, loops)
    missing_calls        a step the healthy path always took is now skipped
    bad_answer           the agent ends with a non-answer even though no step failed
"""

from collections import Counter
from dataclasses import asdict, dataclass, field
from statistics import median

from medic.evidence import TraceEvidence

OUTPUT_SHRINK, OUTPUT_GROW = 0.6, 1.7
MORE_CALLS, FEWER_CALLS = 1.5, 0.5


@dataclass
class Divergence:
    kind: str
    step: str                 # "TOOL read_doc", "LLM gpt-oss:20b", "AGENT"
    detail: str
    score: float              # share of failing traces showing it, weighted by kind
    suspects: list            # [{"kind": "tool_handler", "name": "read_doc"}, ...]
    traces: list = field(default_factory=list)
    numbers: dict = field(default_factory=dict)


# How strongly each kind of divergence points at a cause.
WEIGHT = {"step_error": 1.0, "arguments_changed": 0.95, "output_changed": 0.9,
          "extra_calls": 0.8, "missing_calls": 0.75, "bad_answer": 0.5}


def _key(step: dict) -> str:
    return f"{step['type']} {step['name']}"


def profile(evs: list[TraceEvidence]) -> dict:
    """Per step: calls per trace, argument-name sets, output sizes, error rate."""
    out: dict = {}
    n = max(len(evs), 1)
    for e in evs:
        for s in e.steps:
            p = out.setdefault(_key(s), {"type": s["type"], "name": s["name"], "calls": 0, "errors": 0,
                                         "arg_keys": Counter(), "output_lens": [], "traces": set()})
            p["calls"] += 1
            p["traces"].add(e.trace_id)
            p["errors"] += s["status"].upper() == "ERROR"
            if s["type"] == "TOOL":
                p["arg_keys"][tuple(s.get("arg_keys") or [])] += 1
                if s["status"].upper() != "ERROR":
                    p["output_lens"].append(s.get("output_len", 0))
    for p in out.values():
        p["calls_per_trace"] = p["calls"] / n
        p["error_rate"] = p["errors"] / max(p["calls"], 1)
        p["median_output"] = median(p["output_lens"]) if p["output_lens"] else None
        p["usual_args"] = list(p["arg_keys"].most_common(1)[0][0]) if p["arg_keys"] else []
    return out


def _suspects_for(step_type: str, name: str, kind: str) -> list[dict]:
    if step_type == "LLM":
        return [{"kind": "model_config", "name": name}, {"kind": "prompt", "name": "system"}]
    if kind == "arguments_changed":
        return [{"kind": "tool_schema", "name": name}, {"kind": "tool_handler", "name": name}]
    if kind in ("extra_calls", "missing_calls"):
        return [{"kind": "prompt", "name": "system"}, {"kind": "loop_limit", "name": "turns"},
                {"kind": "tool_schema", "name": name}]
    return [{"kind": "tool_handler", "name": name}, {"kind": "client", "name": name},
            {"kind": "tool_schema", "name": name}]


def localize(failing: list[TraceEvidence], healthy: list[TraceEvidence]) -> dict:
    failing = [e for e in failing if e.steps]
    healthy = [e for e in healthy if e.steps]
    if not failing or not healthy:
        return {"verdict": "insufficient_steps", "confidence": 0.0, "divergences": [],
                "detail": "no step-level evidence for the failing or the healthy traces"}
    hp, fp = profile(healthy), profile(failing)
    nf = len(failing)
    found: list[Divergence] = []

    # 1. The first step that errors in each failing trace.
    firsts = Counter()
    examples: dict = {}
    for e in failing:
        bad = next((s for s in e.steps if s["status"].upper() == "ERROR"), None)
        if bad:
            k = (_key(bad), bad.get("error_type", ""))
            firsts[k] += 1
            examples.setdefault(k, (bad, []))[1].append(e.trace_id)
    for (step, etype), count in firsts.most_common(2):
        bad, ids = examples[(step, etype)]
        was = hp.get(step, {}).get("error_rate", 0.0)
        found.append(Divergence(
            "step_error", step,
            f"{step} fails in {count}/{nf} traces with {etype or 'an error'}: {bad.get('error', '')[:160]}"
            f" (error rate when healthy: {was:.0%})",
            WEIGHT["step_error"] * count / nf, _suspects_for(bad["type"], bad["name"], "step_error"),
            ids[:5], {"failing": count, "error_rate_healthy": was}))

    for step, f in fp.items():
        h = hp.get(step)
        if f["type"] != "TOOL" or not h:
            continue
        # 2. Argument names changed.
        if f["usual_args"] != h["usual_args"] and f["arg_keys"]:
            share = sum(v for k, v in f["arg_keys"].items() if list(k) != h["usual_args"]) / max(f["calls"], 1)
            found.append(Divergence(
                "arguments_changed", step,
                f"{f['name']} is called with {f['usual_args']} where healthy traces used {h['usual_args']}",
                WEIGHT["arguments_changed"] * share, _suspects_for("TOOL", f["name"], "arguments_changed"),
                sorted(f["traces"])[:5], {"now": f["usual_args"], "before": h["usual_args"]}))
        # 3. Output size shifted.
        if f["median_output"] is not None and h["median_output"]:
            ratio = f["median_output"] / h["median_output"]
            if (ratio <= OUTPUT_SHRINK or ratio >= OUTPUT_GROW) and h["median_output"] >= 40:
                found.append(Divergence(
                    "output_changed", step,
                    f"{f['name']} returns {f['median_output']:.0f} characters (median) where healthy traces got "
                    f"{h['median_output']:.0f} ({ratio:.2f}x)",
                    WEIGHT["output_changed"] * min(1.0, abs(1 - ratio) * 1.5),
                    _suspects_for("TOOL", f["name"], "output_changed"), sorted(f["traces"])[:5],
                    {"median_now": f["median_output"], "median_before": h["median_output"], "ratio": ratio}))

    # 4/5. Steps run much more or much less often. Only traces that didn't error count: a
    # crash explains every step missing after it, and retries after an error aren't a loop.
    clean = [e for e in failing if not any(s["status"].upper() == "ERROR" for s in e.steps)]
    cp = profile(clean) if clean else {}
    for step in (set(hp) | set(cp)) if clean else ():
        h, f = hp.get(step), cp.get(step)
        before = h["calls_per_trace"] if h else 0.0
        now = f["calls_per_trace"] if f else 0.0
        stype, name = (h or f)["type"], (h or f)["name"]
        if stype == "LLM":
            continue  # model turns follow tool calls; counting both would double-report one cause
        if before and now / before >= MORE_CALLS:
            repeats = sum(e.repeated_calls > 0 for e in clean) / len(clean)
            found.append(Divergence(
                "extra_calls", step,
                f"{name} runs {now:.1f}x per trace where healthy traces ran it {before:.1f}x"
                + (f"; {repeats:.0%} of failing traces repeat identical calls" if repeats else ""),
                WEIGHT["extra_calls"] * min(1.0, (now / before - 1)), _suspects_for(stype, name, "extra_calls"),
                sorted(f["traces"])[:5] if f else [], {"per_trace_now": now, "per_trace_before": before}))
        elif before >= 0.5 and now / before <= FEWER_CALLS:
            found.append(Divergence(
                "missing_calls", step,
                f"{name} runs {now:.1f}x per trace where healthy traces ran it {before:.1f}x",
                WEIGHT["missing_calls"] * (1 - now / before), _suspects_for(stype, name, "missing_calls"),
                sorted(f["traces"])[:5] if f else [], {"per_trace_now": now, "per_trace_before": before}))

    # 6. Bad answers with no failing step.
    silent = [e for e in failing if e.non_answer and not any(s["status"].upper() == "ERROR" for s in e.steps)]
    if silent:
        found.append(Divergence(
            "bad_answer", "AGENT",
            f"{len(silent)}/{nf} failing traces end in a non-answer although no step failed",
            WEIGHT["bad_answer"] * len(silent) / nf,
            [{"kind": "prompt", "name": "system"}, {"kind": "model_config", "name": "model"}],
            [e.trace_id for e in silent[:5]]))

    found.sort(key=lambda d: -d.score)
    top = found[0] if found else None
    return {
        "verdict": top.kind if top else "no_divergence",
        "confidence": round(min(0.95, top.score), 2) if top else 0.3,
        "detail": top.detail if top else "failing traces take the same steps as healthy ones",
        "divergences": [asdict(d) for d in found[:6]],
        "suspects": _merge_suspects(found[:3]),
        "healthy_traces": len(healthy), "failing_traces": nf,
    }


def _merge_suspects(divergences: list[Divergence]) -> list[dict]:
    """Suspected parts of the agent, most implicated first, without duplicates."""
    scores: dict = {}
    for d in divergences:
        for rank, s in enumerate(d.suspects):
            key = (s["kind"], s["name"])
            scores[key] = scores.get(key, 0) + d.score / (rank + 1)
    return [{"kind": k, "name": n, "score": round(v, 3)} for (k, n), v in sorted(scores.items(), key=lambda x: -x[1])]
