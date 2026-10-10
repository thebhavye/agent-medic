"""Regression investigator: did a release cause this? Built on agent-bisect.

First it asks the question that separates code from environment: does the last healthy
release still work *today*? If it fails too, the code isn't to blame. Otherwise it bisects
the releases in between to the first bad one and pulls its intent from Entire.
"""

from pathlib import Path

from bisect_agent import entire_ctx, gitops
from bisect_agent.evaluator import Evaluator, SkipCommit
from bisect_agent.metrics import SIGNATURES, classify, detect
from bisect_agent.search import bisect
from bisect_agent.state import RunState, save
from medic import anatomy, paths

# Watcher signal -> agent-bisect signature to bisect on.
SIGNATURE_FOR = {"tool_errors": "errors", "failures": "quality", "quality": "quality", "loops": "tool_calls",
                 "tool_calls": "tool_calls", "cost": "cost"}
HEALTHY = {"pass_rate": 0.75, "error_rate": 0.25}


def investigate(deploy: Path, good: str, bad: str, signal: str, cases: Path, reader, log=print) -> dict:
    finding = {"investigator": "regression", "good": good, "bad": bad}
    if not good or not bad:
        return {**finding, "verdict": "unknown", "confidence": 0.0, "detail": "no healthy release on record"}
    good_sha, bad_sha = gitops.rev_parse(good, deploy), gitops.rev_parse(bad, deploy)
    finding.update(good=good_sha, bad=bad_sha)

    state = RunState(good=good_sha, bad=bad_sha)
    ev = Evaluator(deploy, state, cases, reader)
    try:
        good_now = ev.evaluate_rev(good_sha, [])
    except SkipCommit as exc:
        return {**finding, "verdict": "unknown", "confidence": 0.0, "detail": f"could not run the eval: {exc}"}
    finding["good_release_today"] = good_now.to_dict()
    good_ok = good_now.pass_rate >= HEALTHY["pass_rate"] and good_now.error_rate <= HEALTHY["error_rate"]

    if good_sha == bad_sha:
        return {**finding, "verdict": "no_code_change", "confidence": 0.95 if not good_ok else 0.6,
                "detail": f"no new release since the healthy one ({good_sha[:8]}); "
                          + ("it fails today too" if not good_ok else "and it passes the eval today")}
    if not good_ok:
        return {**finding, "verdict": "good_release_fails_today", "confidence": 0.9,
                "detail": f"the last healthy release {good_sha[:8]} now fails the eval too "
                          f"(pass {good_now.pass_rate:.0%}, errors {good_now.error_rate:.0%}): "
                          "something outside the code changed"}

    bad_now = ev.evaluate_rev(bad_sha, [])
    finding["bad_release_today"] = bad_now.to_dict()
    signature = SIGNATURE_FOR.get(signal, "quality")
    regs = detect(good_now, bad_now, only=[signature]) or detect(good_now, bad_now)
    if not regs:
        return {**finding, "verdict": "not_reproduced", "confidence": 0.6,
                "detail": "the eval shows no difference between the healthy and current release"}
    reg = regs[0]
    metric = SIGNATURES[reg.signature].metric
    commits = gitops.commits_between(good_sha, bad_sha, deploy)
    verdicts = {good_sha: "good", bad_sha: "bad"}

    def oracle(sha: str) -> str:
        try:
            v = classify(reg.signature, getattr(ev.evaluate_rev(sha, []), metric), reg.good_value, reg.bad_value)
        except SkipCommit:
            v = "skip"
        log(f"[medic] regression: {sha[:8]} {gitops.subject(sha, deploy)!r}: {v}")
        return v

    result = bisect(commits, oracle, verdicts)
    save(state, deploy)
    if not result.culprit:
        return {**finding, "verdict": "ambiguous", "confidence": 0.4,
                "detail": "skipped releases hide the boundary: " + ", ".join(c[:8] for c in result.candidates)}
    intent = entire_ctx.gather_intent(result.culprit, deploy, deploy)
    surfaces = anatomy.scan(deploy, [f for f in paths.AGENT_FILES if (deploy / f).exists()])
    changed = anatomy.changed_surfaces(deploy, result.culprit, surfaces)
    return {**finding, "verdict": "culprit", "confidence": 0.9, "signature": reg.signature,
            "changed_surfaces": changed["surfaces"], "graph_changes": changed["graph"],
            "good_value": reg.good_value, "bad_value": reg.bad_value, "culprit": result.culprit,
            "culprit_subject": gitops.subject(result.culprit, deploy), "verdicts": verdicts, "intent": intent,
            "evaluations": state.evaluations,
            "detail": f"first bad release {result.culprit[:8]} {gitops.subject(result.culprit, deploy)!r}: "
                      f"{metric} {reg.good_value:.4g} -> {reg.bad_value:.4g}"}
