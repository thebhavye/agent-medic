"""The agent loop: detect -> bisect -> explain -> fix -> verify, once per regression.

Each step reads the run state, does one unit of work, and saves the state, so a crash or
Ctrl-C at any point resumes from the last completed step. Fixes are committed on their own
branch (agent-bisect/fix-<run>) on top of the bad commit; the user's branch is never moved.
"""

import hashlib
import json
from pathlib import Path

import neatlogs

from bisect_agent import entire_ctx, fixer, gitops
from bisect_agent.edits import EditError, apply_edits
from bisect_agent.evaluator import Evaluator, SkipCommit, load_cases
from bisect_agent.metrics import SIGNATURES, Metrics, Regression, classify, detect
from bisect_agent.search import bisect
from bisect_agent.state import STATE_DIR, Investigation, RunState, save
from target_agent.llm import cost_usd

CLOSED = ("fixed", "gave_up", "ambiguous")


class Orchestrator:
    def __init__(self, repo: Path, state: RunState, evaluator: Evaluator,
                 max_attempts: int = 3, max_investigations: int = 5) -> None:
        self.repo = repo
        self.state = state
        self.ev = evaluator
        self.max_attempts = max_attempts
        self.max_investigations = max_investigations
        self.fix_root = repo / STATE_DIR / "wt" / "fix"

    def run(self) -> RunState:
        with neatlogs.trace(f"agent-bisect:{self.state.run_id}", kind="AGENT",
                            session_id=f"agent-bisect-{self.state.run_id}"):
            if self.state.phase != "done":
                self._ensure_fix_branch()
            while self.state.phase != "done":
                if self.state.phase == "detect":
                    self.detect()
                else:
                    self.investigate()
                save(self.state, self.repo)
        return self.state

    # -- detect --------------------------------------------------------------------------

    def _ensure_fix_branch(self) -> None:
        st = self.state
        if not st.fix_branch:
            st.fix_branch = f"agent-bisect/fix-{st.run_id}"
            gitops.git("branch", "--force", st.fix_branch, st.bad, cwd=self.repo)
        if not (self.fix_root / ".git").exists():
            gitops.worktree_add(self.fix_root, st.bad, self.repo, branch=st.fix_branch)
        gitops.git("reset", "--hard", cwd=self.fix_root)   # drop a half-applied fix after a crash
        st.fix_tip = gitops.rev_parse("HEAD", self.fix_root)

    def _baseline(self) -> Metrics:
        return self.ev.evaluate_rev(self.state.good, overlays=[])

    def _tip(self) -> Metrics:
        return self.ev.evaluate_dir(self.fix_root, f"tip-{self.state.fix_tip[:12]}", sha=self.state.fix_tip)

    @neatlogs.span(kind="CHAIN", name="detect")
    def detect(self) -> None:
        st = self.state
        self._ensure_fix_branch()
        baseline, tip = self._baseline(), self._tip()
        closed = {i.signature for i in st.investigations if i.status in ("gave_up", "ambiguous")}
        regressions = [r for r in detect(baseline, tip) if r.signature not in closed]
        if not regressions:
            st.log("no regressions left against the baseline" if st.investigations
                   else "no regression between good and bad")
            st.phase = "done"
            return
        if len(st.investigations) >= self.max_investigations:
            st.log(f"stopping after {self.max_investigations} investigations")
            st.phase = "done"
            return
        reg = regressions[0]
        others = ", ".join(r.describe() for r in regressions[1:])
        st.log(f"regression detected: {reg.signature}: {reg.describe()}"
               + (f" (also: {others})" if others else ""))
        st.investigations.append(Investigation(reg.signature, reg.good_value, reg.bad_value))
        st.phase = "investigate"

    # -- investigate ---------------------------------------------------------------------

    def investigate(self) -> None:
        inv = self.state.current
        if inv is None:
            self.state.phase = "detect"
            return
        step = {"bisecting": self.bisect, "explaining": self.explain, "fixing": self.fix}[inv.status]
        step(inv)

    def overlays_for(self, sha: str) -> list[dict]:
        """Fixes from earlier investigations whose culprit is already in `sha`'s history."""
        out = []
        for idx, inv in enumerate(self.state.investigations):
            if inv.status == "fixed" and gitops.is_ancestor(inv.culprit, sha, self.repo):
                accepted = next(a for a in inv.attempts if a.get("accepted"))
                out.append({"id": idx, "edits": accepted["edits"]})
        return out

    def _value(self, sha: str, inv: Investigation) -> float:
        m = self.ev.evaluate_rev(sha, self.overlays_for(sha))
        return getattr(m, SIGNATURES[inv.signature].metric)

    @neatlogs.span(kind="CHAIN", name="bisect")
    def bisect(self, inv: Investigation) -> None:
        st = self.state
        commits = gitops.commits_between(st.good, st.bad, self.repo)
        # Re-measure the bad end with earlier fixes applied, so both endpoints of this
        # bisection are measured the same way as every commit in between.
        inv.good_value = getattr(self._baseline(), SIGNATURES[inv.signature].metric)
        inv.bad_value = self._value(st.bad, inv)
        inv.verdicts.setdefault(st.good, "good")
        inv.verdicts.setdefault(st.bad, "bad")
        if classify(inv.signature, inv.bad_value, inv.good_value, inv.bad_value) != "bad" or \
                inv.bad_value == inv.good_value:
            st.log(f"{inv.signature} regression does not reproduce at the bad commit; skipping it")
            inv.status = "ambiguous"
            st.phase = "detect"
            return
        st.log(f"bisecting {len(commits) - 2} commits between {st.good[:8]} and {st.bad[:8]} "
               f"for {inv.signature} (good={inv.good_value:.4g}, bad={inv.bad_value:.4g})")

        def oracle(sha: str) -> str:
            try:
                verdict = classify(inv.signature, self._value(sha, inv), inv.good_value, inv.bad_value)
            except SkipCommit as exc:
                st.log(f"skip {sha[:8]}: {exc}")
                verdict = "skip"
            inv.verdicts[sha] = verdict
            save(st, self.repo)
            st.log(f"{sha[:8]} {gitops.subject(sha, self.repo)!r}: {verdict}")
            return verdict

        result = bisect(commits, oracle, inv.verdicts)
        if result.culprit is None:
            st.log("skipped commits leave the culprit ambiguous among: "
                   + ", ".join(c[:8] for c in result.candidates))
            inv.status = "ambiguous"
            st.phase = "detect"
            return
        inv.culprit = result.culprit
        inv.culprit_subject = gitops.subject(result.culprit, self.repo)
        st.log(f"first bad commit: {inv.culprit[:8]} {inv.culprit_subject!r}")
        inv.status = "explaining"

    @neatlogs.span(kind="CHAIN", name="explain")
    def explain(self, inv: Investigation) -> None:
        inv.intent = entire_ctx.gather_intent(inv.culprit, self.repo, self.fix_root)
        self.state.log(f"intent for {inv.culprit[:8]} from {inv.intent['source']}"
                       + (f" (checkpoint {inv.intent['checkpoint']})" if inv.intent["checkpoint"] else ""))
        inv.status = "fixing"

    @neatlogs.span(kind="CHAIN", name="fix")
    def fix(self, inv: Investigation) -> None:
        st = self.state
        if len(inv.attempts) >= self.max_attempts:
            st.log(f"no verified fix after {self.max_attempts} attempts; leaving {inv.signature} for a human")
            inv.status = "gave_up"
            st.phase = "detect"
            return

        self._ensure_fix_branch()   # also discards edits left by an attempt that crashed
        tip_before = self._tip()
        culprit_key = inv.culprit[:12] + "".join(f"+fix{o['id']}" for o in self.overlays_for(inv.culprit))
        culprit_cases = load_cases(st.evaluations[culprit_key])
        failing = [c for c in culprit_cases if not c["passed"]] or \
            sorted(culprit_cases, key=lambda c: -c["cost_usd"])   # cost regressions can still "pass"
        sig = SIGNATURES[inv.signature]
        regression = Regression(inv.signature, inv.good_value, inv.bad_value).describe()

        prompt = fixer.build_prompt(
            regression=regression, signature=inv.signature, culprit=inv.culprit, repo=self.repo,
            tip_root=self.fix_root, intent_text=entire_ctx.render(inv.intent),
            failing_cases=failing, previous_attempts=inv.attempts)
        n = len(inv.attempts) + 1
        st.log(f"fix attempt {n}/{self.max_attempts} for {inv.signature}")
        attempt: dict = {"n": n, "accepted": False}
        try:
            proposal = fixer.propose_fix(prompt)
        except (fixer.FixerError, ValueError) as exc:
            attempt["feedback"] = f"no usable proposal: {exc}"
            inv.attempts.append(attempt)
            return
        attempt.update({k: proposal[k] for k in ("diagnosis", "original_intent", "how_intent_is_kept",
                                                 "commit_title", "edits", "usage")})
        attempt["fixer_cost_usd"] = cost_usd(proposal["usage"]["model"], proposal["usage"]["input_tokens"],
                                             proposal["usage"]["output_tokens"])
        try:
            apply_edits(self.fix_root, proposal["edits"])
        except EditError as exc:
            attempt["feedback"] = f"edits did not apply: {exc}"
            inv.attempts.append(attempt)
            return

        try:
            edits_hash = hashlib.sha1(json.dumps(proposal["edits"], sort_keys=True).encode()).hexdigest()[:8]
            cand = self.ev.evaluate_dir(self.fix_root, f"attempt-{len(st.investigations) - 1}-{n}-{edits_hash}")
        except SkipCommit as exc:
            gitops.git("reset", "--hard", cwd=self.fix_root)
            attempt["feedback"] = f"the patched agent crashed: {exc}"
            inv.attempts.append(attempt)
            return
        value = getattr(cand, sig.metric)
        attempt["metrics"] = cand.to_dict()
        verdict = classify(inv.signature, value, inv.good_value, inv.bad_value)
        # Don't accept a fix that trades this regression for a new one.
        collateral = [r for r in detect(tip_before, cand) if r.signature != inv.signature]

        if verdict == "good" and not collateral:
            gitops.git("add", "-A", cwd=self.fix_root)
            gitops.commit(self._commit_message(inv, attempt, cand), cwd=self.fix_root)
            st.fix_tip = gitops.rev_parse("HEAD", self.fix_root)
            inv.fix_commit = st.fix_tip
            attempt["accepted"] = True
            attempt["feedback"] = f"verified: {sig.metric} {inv.bad_value:.4g} -> {value:.4g} (baseline {inv.good_value:.4g})"
            inv.status = "fixed"
            st.phase = "detect"
            st.log(f"fix verified and committed as {st.fix_tip[:8]}: {attempt['feedback']}")
        else:
            gitops.git("reset", "--hard", cwd=self.fix_root)
            reasons = []
            if verdict != "good":
                reasons.append(f"{sig.metric} is {value:.4g}, still closer to the bad value "
                               f"{inv.bad_value:.4g} than the baseline {inv.good_value:.4g}")
            reasons += [f"introduced a new regression: {r.describe()}" for r in collateral]
            attempt["feedback"] = "; ".join(reasons)
            st.log(f"attempt {n} rejected: {attempt['feedback']}")
        inv.attempts.append(attempt)

    def _commit_message(self, inv: Investigation, attempt: dict, cand: Metrics) -> str:
        sig = SIGNATURES[inv.signature]
        return (
            f"{attempt['commit_title']}\n\n"
            f"{attempt['diagnosis']}\n\n"
            f"Regression: {sig.metric} {inv.good_value:.4g} -> {inv.bad_value:.4g}, "
            f"now {getattr(cand, sig.metric):.4g} on the same eval set.\n"
            f"Culprit: {inv.culprit[:12]} {inv.culprit_subject}\n"
            f"Original intent: {attempt['original_intent']}\n"
            f"Intent kept: {attempt['how_intent_is_kept']}\n\n"
            f"Agent-Bisect-Run: {self.state.run_id}\n"
            + (f"Culprit-Entire-Checkpoint: {inv.intent['checkpoint']}\n" if inv.intent.get("checkpoint") else "")
        )
