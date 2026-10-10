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
    "agent_behavior": "escalate",
    "unexplained": "escalate",
}


def decide(triage: dict, findings: dict) -> dict:
    dep = findings.get("dependency", {})
    reg = findings.get("regression", {})
    loops = findings.get("loops", {})
    dv, rv, lv = dep.get("verdict"), reg.get("verdict"), loops.get("verdict")
    rationale, disagreements = [], []

    for name, f in (("dependency", dep), ("regression", reg), ("loops", loops)):
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
    elif dv == "outage":
        cause, confidence = "dependency_outage", dep.get("confidence", 0.8)
    elif dv == "contract_changed":
        cause, confidence = "dependency_contract", dep.get("confidence", 0.8)
        if rv in ("good_release_fails_today", "no_code_change"):
            rationale.append("the code did not change (or the old code fails too), which confirms the API change")
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
