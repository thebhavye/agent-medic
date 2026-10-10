"""Full loop on a real git history with the seeded regressions (see bisect_agent/simulate.py)."""

import subprocess

import pytest

from bisect_agent import fixer, html_report, orchestrator, simulate, state as st
from bisect_agent.simulate import SimulatedEvaluator


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    return simulate.make_repo(tmp_path / "repo")


def evaluator(repo, state, cls=SimulatedEvaluator):
    return cls(repo, state, repo / "evals" / "cases.jsonl", simulate.offline_reader())


def test_finds_and_fixes_all_three_regressions(repo, monkeypatch):
    monkeypatch.setattr(fixer, "propose_fix", simulate.simulated_fixer([]))
    good = git(repo, "rev-list", "--max-parents=0", "HEAD")
    bad = git(repo, "rev-parse", "HEAD")
    subjects = {s: git(repo, "log", "-1", "--format=%s", s) for s in git(repo, "rev-list", "HEAD").split()}

    state = st.RunState(good=good, bad=bad)
    orchestrator.Orchestrator(repo, state, evaluator(repo, state)).run()

    invs = state.investigations
    assert [(i.signature, i.status) for i in invs] == [("errors", "fixed"), ("quality", "fixed"), ("cost", "fixed")]
    assert subjects[invs[0].culprit] == "Shorten tool parameter names to save prompt tokens"
    assert subjects[invs[1].culprit] == "Cap read_doc output to keep the context window small"
    assert subjects[invs[2].culprit] == "Make the research agent double-check facts before answering"
    assert [a["accepted"] for a in invs[2].attempts] == [False, True]

    # Three fix commits on the fix branch, user's branch untouched.
    log = git(repo, "log", "--format=%s", f"{bad}..{state.fix_branch}").splitlines()
    assert log == [simulate.REPAIRS[s]["commit_title"] for s in ("cost", "quality", "errors")]
    assert git(repo, "rev-parse", "main") == bad
    assert "Agent-Bisect-Run:" in git(repo, "log", "-1", "--format=%B", state.fix_branch)

    page = html_report.render(state, repo)
    assert page.count("first bad commit") == 3
    assert "Drop redundant re-verification loop" in page

    # Resuming a finished run does no new work.
    before = SimulatedEvaluator.runs
    resumed = st.load(repo)
    assert resumed.phase == "done"
    orchestrator.Orchestrator(repo, resumed, evaluator(repo, resumed)).run()
    assert SimulatedEvaluator.runs == before


def test_resumes_mid_bisection(repo, monkeypatch):
    monkeypatch.setattr(fixer, "propose_fix", simulate.simulated_fixer([]))
    good = git(repo, "rev-list", "--max-parents=0", "HEAD")
    state = st.RunState(good=good, bad=git(repo, "rev-parse", "HEAD"))

    class Crash(Exception):
        pass

    class CrashingEvaluator(SimulatedEvaluator):
        budget = 4

        def evaluate_dir(self, path, key, sha=""):
            if key not in self.state.evaluations:
                CrashingEvaluator.budget -= 1
                if CrashingEvaluator.budget < 0:
                    raise Crash
            return super().evaluate_dir(path, key, sha)

    with pytest.raises(Crash):
        orchestrator.Orchestrator(repo, state, evaluator(repo, state, CrashingEvaluator)).run()
    assert len(st.load(repo).evaluations) == 4

    resumed = st.load(repo)
    orchestrator.Orchestrator(repo, resumed, evaluator(repo, resumed)).run()
    assert [i.status for i in resumed.investigations] == ["fixed", "fixed", "fixed"]
