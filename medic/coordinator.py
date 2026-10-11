"""Coordinator: combine the investigators' findings into one diagnosis and a remedy.

Investigators can disagree. The deciding evidence is the regression investigator's replay of
the last healthy release against today's environment:
  - healthy release still passes -> the environment can't explain the failure; the release is the cause
  - healthy release fails too    -> the code isn't to blame; trust the dependency finding
Every rule that fires is written into the rationale, so the decision can be audited.
"""

REMEDY = {
    "code_regression": "code_fix",
    "dependency_contract": "adapt_client",
    "dependency_rate_limit": "retry_policy",
    "dependency_outage": "escalate",
    "dependency_data": "escalate",
    "agent_behavior": "escalate",
    "unexplained": "escalate",
}


def decide(triage: dict, findings: dict) -> dict:
    dep = findings.get("dependency", {})
    reg = findings.get("regression", {})
    loops = findings.get("loops", {})
    dv, rv, lv = dep.get("verdict"), reg.get("verdict"), loops.get("verdict")
    rationale, disagreements = [], []

    loc = findings.get("localize", {})
    nl = findings.get("neatlogs", {})
    for name, f in (("dependency", dep), ("regression", reg), ("loops", loops), ("localize", loc), ("neatlogs", nl)):
        if f:
            rationale.append(f"{name}: {f.get('verdict')}: {f.get('detail')}")

    if rv == "culprit":
        cause = "code_regression"
        if dv in ("contract_changed", "rate_limited", "outage"):
            disagreements.append(
                f"dependency investigator reports {dv}, but the last healthy release passes the eval against "
                "today's API, so that change does not break working code; the release is the cause")
        if lv in ("redundant_calls", "more_calls"):
            rationale.append("loop evidence is consistent with the culprit release")
        confidence = reg.get("confidence", 0.8)
        # Two independent lines of evidence: what the culprit changed, and where traces diverge.
        changed = {(c["kind"], c["name"]) for c in reg.get("changed_surfaces", [])}
        located = {(c["kind"], c["name"]) for c in loc.get("surfaces", [])}
        both = changed & located
        if both:
            parts = ", ".join(f"{k} {n}" for k, n in sorted(both))
            rationale.append(f"the culprit release changed {parts}, and the failing traces diverge at the same "
                             "place: two independent lines of evidence agree")
            confidence = min(0.97, confidence + 0.05)
        elif changed and located:
            disagreements.append(
                "the culprit release changed " + ", ".join(f"{k} {n}" for k, n in sorted(changed)) +
                " but the traces diverge at " + ", ".join(f"{k} {n}" for k, n in sorted(located)[:3]) +
                "; the bisection is direct evidence, so the release is still blamed, and the fix should look at both")
        # A third, independent line: neatlogs' own investigation of the same traces.
        if nl.get("verdict") == "root_cause":
            named = {n for _, n in changed | located}
            text = f"{nl.get('location') or ''} {nl.get('detail') or ''} {nl.get('analysis') or ''}"
            if named and any(n and n in text for n in named):
                rationale.append("neatlogs' investigation points at the same code: "
                                 + ", ".join(sorted(n for n in named if n and n in text)))
                confidence = min(0.98, confidence + 0.02)
            else:
                disagreements.append(f"neatlogs' investigation suggests \"{nl.get('detail')}\", which is not where "
                                     "the bisection and the trace diff point; Medic follows its replayed evidence")
    elif dv == "outage":
        cause, confidence = "dependency_outage", dep.get("confidence", 0.8)
    elif dv == "contract_changed":
        cause, confidence = "dependency_contract", dep.get("confidence", 0.8)
        if rv in ("good_release_fails_today", "no_code_change"):
            rationale.append("the code did not change (or the old code fails too), which confirms the API change")
    elif dv == "content_changed":
        cause, confidence = "dependency_data", dep.get("confidence", 0.7)
        rationale.append("the provider serves different data through an unchanged API; no code change can make "
                         "the data correct, so the data owner has to be told")
    elif dv == "rate_limited":
        cause, confidence = "dependency_rate_limit", dep.get("confidence", 0.7)
    elif lv in ("redundant_calls", "more_calls"):
        cause, confidence = "agent_behavior", loops.get("confidence", 0.6)
        if rv in ("no_code_change", "not_reproduced"):
            disagreements.append("loop evidence without a culprit release: the model may have changed behavior "
                                 "on its own; that needs a human to judge")
    else:
        cause, confidence = "unexplained", 0.3

    decision = {"cause": cause, "remedy": REMEDY[cause], "confidence": round(confidence, 2),
                "rationale": rationale, "disagreements": disagreements, "symptom": triage.get("summary", "")}
    if cause == "code_regression":
        decision["culprit"] = reg["culprit"]
    return decision
