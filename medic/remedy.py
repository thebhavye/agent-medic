"""Remedy: write a fix, verify it against the live environment, and deploy it.

The fix is proposed by the fixer model as search-and-replace edits on a branch of the
deployment repo. Verification runs the eval set plus a replay of the failing production
questions against the *current* knowledge-base API. Only a verified fix is deployed.
"""

import json
from pathlib import Path

import httpx

from bisect_agent import entire_ctx, fixer, gitops
from bisect_agent.edits import EditError, apply_edits
from bisect_agent.evaluator import Evaluator, SkipCommit, load_cases
from bisect_agent.metrics import SIGNATURES, Regression, classify
from bisect_agent.state import RunState
from medic import paths
from target_agent.llm import cost_usd

MAX_ATTEMPTS = 3
HEALTHY = {"pass_rate": 0.75, "error_rate": 0.15}

DEPENDENCY_SYSTEM_NOTE = """This is not a regression in our code: a dependency of the agent changed. The
knowledge-base API the tools call behaves differently from when traffic was healthy.
Change the client code so the agent works with the API as it behaves now. Keep working with
the old behavior too where that is cheap, because providers often roll changes out gradually."""


def _sample(path: str, **params) -> str:
    try:
        r = httpx.get(paths.KB_URL + path, params=params, timeout=10)
        return f"HTTP {r.status_code}, headers {dict((k, v) for k, v in r.headers.items() if k.lower().startswith(('x-', 'retry')))}\n{r.text[:700]}"
    except httpx.HTTPError as exc:
        return f"request failed: {exc}"


def dependency_prompt(inc, tip_root: Path, previous: list[dict]) -> str:
    dep = inc.findings.get("dependency", {})
    files = []
    for rel in ("target_agent/tools.py",):
        files.append(f'<file path="{rel}">\n{(tip_root / rel).read_text(encoding="utf-8")}\n</file>')
    examples = json.dumps(inc.triage.get("examples", [])[:2], indent=1)
    query = next((e["question"] for e in inc.triage.get("examples", []) if e.get("question")), "pilot plant")
    sections = [
        f"<diagnosis cause=\"{inc.decision['cause']}\">\n{DEPENDENCY_SYSTEM_NOTE}\n\n{dep.get('detail', '')}\n</diagnosis>",
        "<failing_production_traces>\n" + examples + "\n</failing_production_traces>",
        "<contract_when_healthy>\n" + json.dumps(_baseline_contract(), indent=1) + "\n</contract_when_healthy>",
        f"<live_api_now>\nGET /search?q=...\n{_sample('/search', q=query)}\n\nGET /docs/pilot-plant\n"
        f"{_sample('/docs/pilot-plant')}\n</live_api_now>",
        "<current_files>\n" + "\n".join(files) + "\n</current_files>",
    ]
    for i, a in enumerate(previous, 1):
        sections.append(f"<previous_attempt n=\"{i}\">\nedits: {json.dumps(a.get('edits'))}\n"
                        f"result: {a.get('feedback')}\n</previous_attempt>")
    return "\n\n".join(sections)


def _baseline_contract() -> dict:
    state = json.loads(paths.STATE.read_text(encoding="utf-8")) if paths.STATE.exists() else {}
    return state.get("contract") or {}


def code_prompt(inc, tip_root: Path, previous: list[dict]) -> str:
    reg = inc.findings["regression"]
    culprit_cases = [c for key, e in reg.get("evaluations", {}).items() if key.startswith(reg["culprit"][:12])
                     for c in load_cases(e)]
    failing = [c for c in culprit_cases if not c["passed"]] or sorted(culprit_cases, key=lambda c: -c["tool_calls"])
    return fixer.build_prompt(
        regression=Regression(reg["signature"], reg["good_value"], reg["bad_value"]).describe(),
        signature=reg["signature"], culprit=reg["culprit"], repo=paths.DEPLOY, tip_root=tip_root,
        intent_text=entire_ctx.render(reg["intent"]), failing_cases=failing, previous_attempts=previous)


def _verify_cases(inc) -> Path:
    """The eval set plus the failing production questions, replayed as-is."""
    lines = paths.CASES.read_text(encoding="utf-8").splitlines()
    for i, e in enumerate(inc.triage.get("examples", [])):
        if e.get("question"):
            lines.append(json.dumps({"id": f"replay-{i}", "question": e["question"], "expect": []}))
    out = paths.MEDIC_DIR / "verify" / f"{inc.id}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def attempt(inc, reader, log=print) -> dict:
    """One fix attempt: propose, apply, verify. Returns the attempt record."""
    branch = f"medic/fix-{inc.id}"
    root = paths.MEDIC_DIR / "worktrees" / inc.id
    if not (root / ".git").exists():
        gitops.git("branch", "--force", branch, "HEAD", cwd=paths.DEPLOY)
        gitops.worktree_add(root, "HEAD", paths.DEPLOY, branch=branch)
    gitops.git("reset", "--hard", cwd=root)
    previous = inc.remedy.setdefault("attempts", [])
    n = len(previous) + 1
    record: dict = {"n": n, "accepted": False}

    prompt = code_prompt(inc, root, previous) if inc.decision["remedy"] == "code_fix" else \
        dependency_prompt(inc, root, previous)
    try:
        proposal = fixer.propose_fix(prompt)
    except Exception as exc:   # bad output, timeout or provider error: count it as a failed attempt
        record["feedback"] = f"no usable proposal: {type(exc).__name__}: {exc}"
        return record
    record.update({k: proposal.get(k) for k in ("diagnosis", "original_intent", "how_intent_is_kept",
                                                 "commit_title", "edits", "usage")})
    u = proposal.get("usage") or {}
    record["fixer_cost_usd"] = cost_usd(u.get("model", ""), u.get("input_tokens", 0), u.get("output_tokens", 0))
    try:
        apply_edits(root, proposal["edits"])
    except EditError as exc:
        record["feedback"] = f"edits did not apply: {exc}"
        return record

    state = RunState(good=inc.healthy_release, bad=inc.release)
    ev = Evaluator(paths.DEPLOY, state, _verify_cases(inc), reader)
    try:
        m = ev.evaluate_dir(root, f"verify-{inc.id}-{n}")
    except SkipCommit as exc:
        gitops.git("reset", "--hard", cwd=root)
        record["feedback"] = f"the patched agent crashed: {exc}"
        return record
    record["metrics"] = m.to_dict()

    problems = []
    if m.pass_rate < HEALTHY["pass_rate"]:
        problems.append(f"pass rate {m.pass_rate:.0%} is below {HEALTHY['pass_rate']:.0%}")
    if m.error_rate > HEALTHY["error_rate"]:
        problems.append(f"{m.error_rate:.0%} of runs still hit errors")
    reg = inc.findings.get("regression", {})
    if inc.decision["remedy"] == "code_fix" and reg.get("signature"):
        value = getattr(m, SIGNATURES[reg["signature"]].metric)
        if classify(reg["signature"], value, reg["good_value"], reg["bad_value"]) != "good":
            problems.append(f"{SIGNATURES[reg['signature']].metric} is {value:.4g}, still closer to the bad "
                            f"value {reg['bad_value']:.4g} than the healthy {reg['good_value']:.4g}")
    if problems:
        gitops.git("reset", "--hard", cwd=root)
        record["feedback"] = "; ".join(problems)
        return record

    title = proposal.get("commit_title") or "Medic fix"
    body = (f"{title}\n\n{proposal.get('diagnosis', '')}\n\nIncident: {inc.id}\nCause: {inc.decision['cause']}\n"
            f"Verified: pass {m.pass_rate:.0%}, errors {m.error_rate:.0%} on {m.n} runs against the live API.\n\n"
            f"Agent-Medic-Incident: {inc.id}\n")
    gitops.git("add", "-A", cwd=root)
    gitops.git("commit", "-q", "-m", body, cwd=root)
    record.update(accepted=True, commit=gitops.rev_parse("HEAD", root), branch=branch,
                  feedback=f"verified: pass {m.pass_rate:.0%}, errors {m.error_rate:.0%} on {m.n} runs")
    return record


def deploy(inc) -> dict:
    """Ship the verified fix: merge the fix branch into the deployment's main branch."""
    branch = inc.remedy["branch"]
    before = gitops.rev_parse("HEAD", paths.DEPLOY)
    try:
        gitops.git("merge", "--ff-only", branch, cwd=paths.DEPLOY)
    except gitops.GitError:
        # A new release shipped while Medic worked; merge on top of it instead.
        try:
            gitops.git("merge", "--no-edit", branch, cwd=paths.DEPLOY)
        except gitops.GitError as exc:
            gitops.git("merge", "--abort", cwd=paths.DEPLOY, check=False)
            return {"deployed": False, "detail": f"merge conflict with the newer release: {exc}"}
    after = gitops.rev_parse("HEAD", paths.DEPLOY)
    gitops.worktree_remove(paths.MEDIC_DIR / "worktrees" / inc.id, paths.DEPLOY)
    return {"deployed": True, "from": before, "release": after}
