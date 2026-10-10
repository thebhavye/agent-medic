"""Dependency investigator: did the knowledge-base API change under us?

It probes the live API and compares status codes and response shapes with the contract the
Watcher recorded while traffic was healthy, then checks that against the errors the tool
spans actually raised.
"""

from medic import contract


def investigate(kb_url: str, baseline_contract: dict | None, examples: list[dict]) -> dict:
    query = next((e["question"] for e in examples if e.get("question")), "pilot plant")
    live = contract.probe(kb_url, query)
    statuses = live["statuses"]
    errors = [err for e in examples for err in e.get("tool_errors", [])]
    error_text = "; ".join(f"{e['tool']}: {e['type']}: {e['message']}" for e in errors[:3])

    finding = {"investigator": "dependency", "live": live, "observed_tool_errors": error_text}
    numeric = [s for s in statuses if isinstance(s, int)]
    if not numeric or all(s >= 500 for s in numeric):
        return {**finding, "verdict": "outage", "confidence": 0.9,
                "detail": f"the knowledge-base API is failing every request ({statuses})"}
    if any(s == 429 for s in numeric):
        share = sum(s == 429 for s in numeric) / len(numeric)
        return {**finding, "verdict": "rate_limited", "confidence": 0.8,
                "detail": f"{share:.0%} of probe requests were rate limited (HTTP 429)"}
    if baseline_contract:
        changes = contract.diff({"search": baseline_contract.get("search"), "doc": baseline_contract.get("doc")},
                                {"search": live["search"], "doc": live["doc"]})
        version_changed = baseline_contract.get("api_version") != live["api_version"]
        if changes or version_changed:
            detail = (f"API version {baseline_contract.get('api_version')} -> {live['api_version']}; "
                      if version_changed else "") + "; ".join(changes)
            return {**finding, "verdict": "contract_changed", "confidence": 0.9 if errors else 0.6,
                    "changes": changes, "detail": detail}
    return {**finding, "verdict": "healthy", "confidence": 0.7,
            "detail": "the API answers normally with the same response shapes as during healthy traffic"}
