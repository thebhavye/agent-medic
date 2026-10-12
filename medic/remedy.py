"""Remedy: write a fix, verify it against the live environment, and deploy it.

The fix is proposed by the fixer model as search-and-replace edits on a branch of the
deployment repo. Verification runs the eval set plus a replay of the failing production
questions against the *current* knowledge-base API. Only a verified fix is deployed.
"""

import json
import re
from pathlib import Path

import httpx

from bisect_agent import entire_ctx, fixer, gitops
from bisect_agent.edits import EditError, apply_edits
from bisect_agent.evaluator import Evaluator, SkipCommit, load_cases
from bisect_agent.metrics import SIGNATURES, Regression, classify
from bisect_agent.state import RunState
from medic import paths, tournament
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
    # The fix is made on the release that's live in production (in a user's repo, their checkout
    # may be anywhere: Medic never fixes or touches it).
    base = inc.release or gitops.rev_parse("HEAD", paths.DEPLOY)
    if not (root / ".git").exists():
        gitops.git("branch", "--force", branch, base, cwd=paths.DEPLOY)
        gitops.worktree_add(root, base, paths.DEPLOY, branch=branch)
    # Every attempt starts from the live release. A fix that was rejected or rolled back in a
    # canary must not stay on the branch underneath the next one.
    gitops.git("reset", "--hard", base, cwd=root)
    previous = inc.remedy.setdefault("attempts", [])
    n = len(previous) + 1
    record: dict = {"n": n, "accepted": False}

    def base_prompt(r: Path) -> str:
        return code_prompt(inc, r, previous) if inc.decision["remedy"] == "code_fix" else \
            dependency_prompt(inc, r, previous)

    if tournament.targets(inc):
        return _tournament_attempt(inc, reader, root, branch, n, base_prompt, log)

    prompt = base_prompt(root)
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

    problems = judge(inc, m)
    if problems:
        gitops.git("reset", "--hard", cwd=root)
        record["feedback"] = "; ".join(problems)
        return record

    title = proposal.get("commit_title") or "Medic fix"
    body = (f"{title}\n\n{proposal.get('diagnosis', '')}\n\nIncident: {inc.id}\nCause: {inc.decision['cause']}\n"
            f"Verified: pass {m.pass_rate:.0%}, errors {m.error_rate:.0%} on {m.n} runs against the live API.\n\n"
            f"Agent-Medic-Incident: {inc.id}\n")
    gitops.git("add", "-A", cwd=root)
    gitops.commit(body, cwd=root)
    record.update(accepted=True, commit=gitops.rev_parse("HEAD", root), branch=branch,
                  feedback=f"verified: pass {m.pass_rate:.0%}, errors {m.error_rate:.0%} on {m.n} runs")
    return record


def judge(inc, m) -> list[str]:
    """Why a verified run still isn't good enough (empty list: it is)."""
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
    return problems


def _tournament_attempt(inc, reader, root: Path, branch: str, n: int, base_prompt, log) -> dict:
    """One candidate per suspected surface, verified side by side; the best one is committed."""
    def verify(candidate_root: Path, key: str):
        ev = Evaluator(candidate_root, RunState(good=inc.healthy_release, bad=inc.release), _verify_cases(inc), reader)
        try:
            m = ev.evaluate_dir(candidate_root, key)
        except SkipCommit as exc:
            return {}, [f"the patched agent crashed: {exc}"]
        return m.to_dict(), judge(inc, m)

    result = tournament.run(inc, base_prompt, verify, log)
    board = [{k: v for k, v in c.items() if k != "root"} for c in result["board"]]
    record: dict = {"n": n, "accepted": False, "tournament": board,
                    "fixer_cost_usd": sum(cost_usd((c.get("usage") or {}).get("model", ""),
                                                   (c.get("usage") or {}).get("input_tokens", 0),
                                                   (c.get("usage") or {}).get("output_tokens", 0)) for c in board)}
    w = result["winner"]
    if not w:
        record["feedback"] = "no candidate passed: " + "; ".join(
            f"{tournament._label(c)}: {c['verdict']}" + (f" ({c['feedback'][:100]})" if c.get("feedback") else "")
            for c in board)
        return record
    apply_edits(root, w["edits"])
    gitops.worktree_remove(Path(w["root"]), paths.DEPLOY)
    m = w["metrics"]
    label = tournament._label(w)
    title = w.get("commit_title") or "Medic fix"
    body = (f"{title}\n\n{w.get('diagnosis', '')}\n\nIncident: {inc.id}\nCause: {inc.decision['cause']}\n"
            f"Chosen from {len(board)} candidates: {label} won with pass {m['pass_rate']:.0%}, errors "
            f"{m['error_rate']:.0%} on {m['n']} runs, {w['changed_lines']} changed lines.\n\n"
            f"Agent-Medic-Incident: {inc.id}\n")
    gitops.git("add", "-A", cwd=root)
    gitops.commit(body, cwd=root)
    others = [c for c in board if not c.get("winner")]
    record.update({k: w.get(k) for k in ("commit_title", "diagnosis", "original_intent", "how_intent_is_kept",
                                         "edits", "usage")})
    record.update(accepted=True, commit=gitops.rev_parse("HEAD", root), branch=branch, metrics=m, winner=label,
                  feedback=f"tournament: {label} won (pass {m['pass_rate']:.0%}, {w['changed_lines']} lines) over "
                           + (", ".join(f"{tournament._label(c)} [{c['verdict']}]" for c in others) or "no rivals"))
    return record


def _changed_spans(healthy: str, live: str, file: str) -> list[tuple[int, int]]:
    """The line spans (in the live file) where `file` differs between the two releases."""
    out = gitops.git("diff", "-U0", healthy, live, "--", file, cwd=paths.DEPLOY)
    spans = []
    for m in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", out, re.M):
        start, count = int(m.group(1)), int(m.group(2) or 1)
        spans.append((start, start + max(count, 1) - 1))
    return spans


def restore_attempt(inc, reader, log=print) -> dict | None:
    """The last resort before paging a human: undo the culprit's change to the surfaces first aid pinned.

    First aid already proved that the healthy value of those surfaces fixes production. When no
    written fix passes, a fix PR that restores exactly that is better than none, as long as the
    culprit changed nothing else in those files (the restore can't take an unrelated change with
    it) and it verifies like any other fix. None when this doesn't apply."""
    fa, healthy = inc.first_aid or {}, (inc.first_aid or {}).get("base") or inc.healthy_release
    pinned = set(fa.get("values", [])) | set(fa.get("functions", []))
    trial = fa.get("trial") or {}
    if not pinned or not healthy or trial.get("pass_rate", 0) < HEALTHY["pass_rate"]:
        return None
    root = paths.MEDIC_DIR / "worktrees" / inc.id
    base = inc.release or gitops.rev_parse("HEAD", paths.DEPLOY)
    surfaces = [t for t in tournament.targets(inc) if t["name"] in pinned]
    files = sorted({t["file"] for t in surfaces})
    if not files or not (root / ".git").exists():
        return None
    for f in files:
        mine = [t for t in surfaces if t["file"] == f]
        for start, end in _changed_spans(healthy, base, f):
            if not any(t["start"] - tournament.LINE_SLACK <= start and end <= t["end"] + tournament.LINE_SLACK
                       for t in mine):
                log(f"[medic] incident {inc.id}: can't restore {f}: the culprit also changed lines {start}-{end}")
                return None
    gitops.git("reset", "--hard", base, cwd=root)
    gitops.git("checkout", healthy, "--", *files, cwd=root)
    n = len(inc.remedy.get("attempts", [])) + 1
    record: dict = {"n": n, "accepted": False, "restore": {"from": healthy, "files": files, "surfaces": sorted(pinned)}}
    ev = Evaluator(root, RunState(good=inc.healthy_release, bad=inc.release), _verify_cases(inc), reader)
    try:
        m = ev.evaluate_dir(root, f"verify-{inc.id}-restore")
    except SkipCommit as exc:
        gitops.git("reset", "--hard", base, cwd=root)
        record["feedback"] = f"restoring {', '.join(sorted(pinned))} crashed: {exc}"
        return record
    record["metrics"] = m.to_dict()
    problems = judge(inc, m)
    if problems:
        gitops.git("reset", "--hard", base, cwd=root)
        record["feedback"] = "restore: " + "; ".join(problems)
        return record
    names = ", ".join(sorted(pinned))
    culprit = inc.findings.get("regression", {}).get("culprit", inc.release or "")[:8]
    title = f"Restore {names} from {healthy[:8]}"
    body = (f"{title}\n\nNo rewritten fix passed verification, so this undoes {culprit}'s change to {names} "
            f"and keeps the rest of that commit. It is what first aid served in production "
            f"(pass {trial['pass_rate']:.0%}). Whatever {culprit} meant to achieve there needs another way.\n\n"
            f"Incident: {inc.id}\nCause: {inc.decision['cause']}\n"
            f"Verified: pass {m.pass_rate:.0%}, errors {m.error_rate:.0%} on {m.n} runs.\n\n"
            f"Agent-Medic-Incident: {inc.id}\n")
    gitops.git("add", "-A", cwd=root)
    gitops.commit(body, cwd=root)
    record.update(accepted=True, commit=gitops.rev_parse("HEAD", root), branch=f"medic/fix-{inc.id}",
                  commit_title=title, diagnosis=body.split("\n\n")[1],
                  feedback=f"restored {names} from {healthy[:8]}: pass {m.pass_rate:.0%}, errors {m.error_rate:.0%}")
    return record


def deploy(inc) -> dict:
    """Ship the verified fix: merge the fix branch into the deployment's main branch."""
    branch = inc.remedy["branch"]
    before = gitops.rev_parse("HEAD", paths.DEPLOY)
    try:
        gitops.git(*gitops.NO_HOOKS, "merge", "--ff-only", branch, cwd=paths.DEPLOY)
    except gitops.GitError:
        # A new release shipped while Medic worked; merge on top of it instead.
        try:
            gitops.git(*gitops.NO_HOOKS, "merge", "--no-edit", branch, cwd=paths.DEPLOY)
        except gitops.GitError as exc:
            gitops.git("merge", "--abort", cwd=paths.DEPLOY, check=False)
            return {"deployed": False, "detail": f"merge conflict with the newer release: {exc}"}
    after = gitops.rev_parse("HEAD", paths.DEPLOY)
    gitops.worktree_remove(paths.MEDIC_DIR / "worktrees" / inc.id, paths.DEPLOY)
    return {"deployed": True, "from": before, "release": after}
