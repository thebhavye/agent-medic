"""Repair tournament: several candidate fixes, each aimed at a different part of the agent,
verified side by side; the best one ships.

A single fix proposal is a guess about where the problem is. The localizer and the regression
investigator usually name more than one suspect: the tool handler or its schema, the prompt
or the turn limit. So Medic writes one candidate per suspected surface, each constrained to
that surface, reviews it, verifies it in its own worktree against the eval set plus the
replayed failing production questions, and ranks the survivors by pass rate, errors, the
regressed metric and the size of the change. The scoreboard is kept as evidence of why the
winner won.
"""

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bisect_agent import fixer, gitops
from bisect_agent.edits import EditError, apply_edits, changed_lines
from medic import anatomy, paths

K = int(os.environ.get("MEDIC_CANDIDATES", 3))

# What a fix to each kind of surface should look like, and how big it may be.
OPERATORS = {
    "prompt": "Edit the instruction text. Keep its purpose; change only the sentence that causes the behavior.",
    "tool_schema": "Align the tool definition (parameter names, descriptions, required fields) with how the "
                   "handler reads it and how the model calls it.",
    "tool_handler": "Change how the handler reads its arguments or returns its result. Let real errors surface "
                    "as tool errors; don't swallow them.",
    "client": "Adapt the dependency client to how the service responds now, keeping the old behavior working.",
    "model_config": "Change the model setting only, to a model that is known to be available.",
    "loop_limit": "Adjust the limit, or add a guard that stops repeated identical calls.",
}
LINE_LIMIT = {"model_config": 4, "loop_limit": 6, "prompt": 10, "tool_schema": 12, "tool_handler": 16, "client": 30}
BROAD_EXCEPT = re.compile(r"^\s*except\s*(Exception|BaseException)?\s*(as\s+\w+)?\s*:", re.M)


def targets(inc) -> list[dict]:
    """Distinct surfaces to aim candidates at, best evidence first."""
    loc = inc.findings.get("localize", {})
    reg = inc.findings.get("regression", {})
    located = {(s["kind"], s["name"]) for s in loc.get("surfaces", [])}
    changed = reg.get("changed_surfaces", [])
    ordered = sorted(changed, key=lambda s: (s["kind"], s["name"]) not in located)   # agreement first
    ordered += loc.get("surfaces", [])
    if inc.decision.get("remedy") in ("adapt_client", "retry_policy"):
        files = [f for f in paths.AGENT_FILES if (paths.DEPLOY / f).exists()]
        ordered += [s.to_dict() for s in anatomy.scan(paths.DEPLOY, files) if s.kind in ("client", "tool_handler")]
    out, seen = [], set()
    for s in ordered:
        key = (s["kind"], s["name"], s["file"], s["start"])
        if key not in seen and s["kind"] in OPERATORS:
            seen.add(key)
            out.append(s)
    return out[:K]


def target_section(t: dict) -> str:
    impact = f"\nEntire graph impact of {t.get('symbol')}:\n{t['impact']}" if t.get("impact") else ""
    return (f"<repair_target kind=\"{t['kind']}\" name=\"{t['name']}\" file=\"{t['file']}\" "
            f"lines=\"{t['start']}-{t['end']}\">\nFix the problem by changing ONLY this part of the agent. "
            f"{OPERATORS[t['kind']]}\nIf this part cannot fix the problem on its own, make no edits and say so in "
            f"the diagnosis.\n\n{t['snippet']}{impact}\n</repair_target>")


def review(edits: list[dict], root: Path, target: dict, suspects: list[dict] | None = None) -> list[str]:
    """A good fix, not just a working one: stays on its target, proportionate, honest, sound, and
    repairs the suspected code instead of routing around it."""
    problems = []
    if not edits:
        return ["no edits proposed"]
    for name in _bypassed(edits, root, suspects or []):
        problems.append(f"bypasses {name}() instead of fixing it: the call is removed but {name} still exists, "
                        "so anything else using it stays broken")
    off_target = [e["path"] for e in edits if e["path"].replace("\\", "/") != target["file"]]
    if off_target:
        problems.append(f"edits outside the target file {target['file']}: {sorted(set(off_target))}")
    else:
        # In the right file is not enough: the edit has to touch the target's own lines, or a
        # line that names it directly (a tool's dispatch entry, a registration, its call site).
        for e in edits:
            span = _location(root / e["path"], e["find"])
            if span and (span[1] < target["start"] - LINE_SLACK or span[0] > target["end"] + LINE_SLACK) \
                    and not _names_target(e["find"], e["replace"], target):
                problems.append(f"edits lines {span[0]}-{span[1]}, outside the target "
                                f"{target['kind']} {target['name']} (lines {target['start']}-{target['end']})")
    changed = changed_lines(edits)
    limit = LINE_LIMIT.get(target["kind"], 20)
    if changed > limit:
        problems.append(f"changes {changed} lines; a {target['kind']} fix should need about {limit}")
    for e in edits:
        if len(BROAD_EXCEPT.findall(e["replace"])) > len(BROAD_EXCEPT.findall(e["find"])):
            problems.append(f"{e['path']} adds a broad except block that would hide failures")
    return problems


LINE_SLACK = 3      # an edit may start or end a few lines outside its target (decorators, a blank line)


def _names_target(find: str, replace: str, target: dict) -> bool:
    """Every line the edit removes or adds refers to the target by name, e.g. the dispatch entry
    `"search_docs": lambda args: search_docs(args["query"])` for the tool_handler search_docs.
    The added lines count too: a narrow fix can't slip in changes to other registrations."""
    name = target.get("name")
    old, new = find.splitlines(), replace.splitlines()
    lines = [line for line in old if line.strip()] + [line for line in new if line.strip() and line not in old]
    return bool(name and lines) and all(re.search(rf"\b{re.escape(name)}\b", line) for line in lines)


def _location(path: Path, find: str) -> tuple[int, int] | None:
    """Line range (1-based) of `find` in the file, or None if it isn't there."""
    if not path.exists() or not find:
        return None
    text = path.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
    pos = text.find(find.replace("\r\n", "\n"))
    if pos < 0:
        return None
    start = text.count("\n", 0, pos) + 1
    return start, start + find.count("\n")


def soundness(edits: list[dict], root: Path) -> list[str]:
    """After the edits are applied: changed files compile and call nothing that doesn't exist."""
    from medic.external import REVIEW_CHECK
    problems = []
    for path in sorted({e["path"] for e in edits if e["path"].endswith(".py")}):
        full = root / path
        if full.exists():
            check = subprocess.run([sys.executable, "-c", REVIEW_CHECK, str(full)], capture_output=True,
                                   text=True, cwd=root, timeout=120)
            if check.returncode != 0:
                problems.append(f"{path}: {(check.stdout or check.stderr).strip()[-240:]}")
    return problems


def _bypassed(edits: list[dict], root: Path, suspects: list[dict]) -> list[str]:
    """Suspected functions an edit stops calling while leaving them defined: a workaround."""
    names = {s.get("symbol") for s in suspects if s.get("kind") in ("tool_handler", "client") and s.get("symbol")}
    out = []
    for name in sorted(n for n in names if n):
        call = re.compile(rf"\b{re.escape(name)}\(")
        removed = any(call.search(e["find"]) and not call.search(e["replace"]) for e in edits)
        still_defined = any(re.search(rf"^\s*def {re.escape(name)}\(", (root / f).read_text(encoding="utf-8"), re.M)
                            for f in {e["path"] for e in edits} if (root / f).exists())
        if removed and still_defined:
            out.append(name)
    return out


def _changed_lines(edits: list[dict]) -> int:
    return sum(max(len(e["find"].splitlines()), len(e["replace"].splitlines())) for e in edits)


def run(inc, base_prompt, verify, log=print) -> dict:
    """base_prompt(root) -> str; verify(root, key) -> (metrics_dict, problems). Returns the scoreboard
    and the winning candidate (or None)."""
    head = gitops.rev_parse("HEAD", paths.DEPLOY)
    previous = [c for a in inc.remedy.get("attempts", []) for c in a.get("tournament", [])]
    aims = targets(inc) or [None]
    n_attempt = len(inc.remedy.get("attempts", [])) + 1

    # The parts the evidence implicates: what the culprit changed and where the traces diverge.
    suspects = (inc.findings.get("regression", {}).get("changed_surfaces", [])
                + inc.findings.get("localize", {}).get("surfaces", []))
    culprit_parts = {(s["kind"], s["name"], s["file"], s["start"])
                     for s in inc.findings.get("regression", {}).get("changed_surfaces", [])}

    # Worktrees one at a time (git locks its worktree list); fixing and verifying in parallel.
    roots = []
    for i in range(len(aims)):
        root = paths.MEDIC_DIR / "worktrees" / f"{inc.id}-a{n_attempt}-c{i}"
        gitops.worktree_add(root, head, paths.DEPLOY)
        roots.append(root)

    def compete(i: int, target: dict | None) -> dict:
        root = roots[i]
        record = {"candidate": i, "target": {k: target[k] for k in ("kind", "name", "file", "start", "end")}
                  if target else None, "root": str(root)}
        prompt = base_prompt(root)
        if target:
            prompt += "\n\n" + target_section(target)
        tried = [c for c in previous if c.get("target") == record["target"]]
        if tried:
            prompt += "\n\n<earlier_candidates_on_this_target>\n" + "\n".join(
                f"edits: {json.dumps(c.get('edits'))}\nresult: {c.get('verdict')}: {c.get('feedback')}"
                for c in tried[-2:]) + "\n</earlier_candidates_on_this_target>"
        try:
            proposal = fixer.propose_fix(prompt)
        except Exception as exc:
            return {**record, "verdict": "no_proposal", "feedback": f"{type(exc).__name__}: {exc}"}
        record.update({k: proposal.get(k) for k in ("commit_title", "diagnosis", "original_intent",
                                                     "how_intent_is_kept", "edits", "usage")})
        problems = review(proposal.get("edits") or [], root, target, suspects) if target else []
        if problems:
            return {**record, "verdict": "rejected_in_review", "feedback": "; ".join(problems)}
        try:
            apply_edits(root, proposal["edits"])
        except EditError as exc:
            return {**record, "verdict": "did_not_apply", "feedback": str(exc)}
        # Checks that need the fix in place: does the patched file compile, does it call real things.
        unsound = soundness(proposal["edits"], root)
        if unsound:
            return {**record, "verdict": "rejected_in_review", "feedback": "; ".join(unsound)}
        metrics, verify_problems = verify(root, f"cand-{inc.id}-a{n_attempt}-c{i}")
        record.update(metrics=metrics, changed_lines=_changed_lines(proposal["edits"]))
        if verify_problems:
            return {**record, "verdict": "failed_verification", "feedback": "; ".join(verify_problems)}
        return {**record, "verdict": "passed", "feedback": "verified"}

    def safely(pair) -> dict:
        """One candidate failing (an eval timeout, a crash) must not discard the others."""
        i, target = pair
        try:
            return compete(i, target)
        except Exception as exc:
            return {"candidate": i, "root": str(roots[i]), "verdict": "error",
                    "target": {k: target[k] for k in ("kind", "name", "file", "start", "end")} if target else None,
                    "feedback": f"{type(exc).__name__}: {exc}"[:400]}

    with ThreadPoolExecutor(max_workers=max(1, len(aims))) as pool:
        board = list(pool.map(safely, enumerate(aims)))
    for c in board:
        log(f"[medic] candidate {c['candidate']} on {_label(c)}: {c['verdict']}"
            + (f" (pass {c['metrics']['pass_rate']:.0%}, {c.get('changed_lines')} lines)" if c.get("metrics") else "")
            + (f": {c['feedback'][:140]}" if c["verdict"] != "passed" else ""))

    passed = [c for c in board if c["verdict"] == "passed"]
    # Fixing what the culprit broke beats working around it; diff size only breaks ties after that.
    def at_root_cause(c: dict) -> bool:
        t = c.get("target") or {}
        return (t.get("kind"), t.get("name"), t.get("file"), t.get("start")) in culprit_parts

    winner = min(passed, key=lambda c: (-c["metrics"]["pass_rate"], c["metrics"]["error_rate"],
                                        not at_root_cause(c), c["metrics"]["tool_calls_per_run"],
                                        c["changed_lines"])) if passed else None
    for c in board:
        c["at_root_cause"] = at_root_cause(c)
    for c in board:
        c["winner"] = c is winner
        if c is not winner:
            gitops.worktree_remove(Path(c["root"]), paths.DEPLOY)
    return {"board": board, "winner": winner}


def _label(c: dict) -> str:
    t = c.get("target")
    return f"{t['kind']} {t['name']} ({t['file']}:{t['start']})" if t else "the whole agent"
