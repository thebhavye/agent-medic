"""Offline simulation: the full agent-bisect loop with no API keys.

A copy of the repo gets the seeded regressions, then the real orchestrator runs on it with
two stand-ins: the evaluator scores a checkout by which planted bugs its code contains, and
the fixer returns known repairs (its first cost fix is deliberately wrong, to show a
rejected attempt). Git, worktrees, bisection, overlays, verification, the fix branch and the
state file are all real. Used by the tests and by `agent-bisect simulate`.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from bisect_agent import fixer, orchestrator, state as st
from bisect_agent.evaluator import Evaluator
from bisect_agent.metrics import from_cases
from bisect_agent.neatlogs_api import NeatlogsReader

ROOT = Path(__file__).resolve().parent.parent


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(dest: Path) -> Path:
    """Copy the target agent into a fresh git repo and seed the demo history."""
    for name in ("target_agent", "evals", "scripts"):
        shutil.copytree(ROOT / name, dest / name, ignore=shutil.ignore_patterns("__pycache__"))
    (dest / ".gitignore").write_text(".bisect/\n__pycache__/\n")
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "config", "user.email", "agent-bisect@example.com")
    _git(dest, "config", "user.name", "agent-bisect simulation")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "Research agent: search and read tools over the Halcyon knowledge base")
    subprocess.run([sys.executable, str(dest / "scripts" / "seed_history.py"), "--repo", str(dest)],
                   check=True, capture_output=True)
    return dest


class SimulatedEvaluator(Evaluator):
    """Scores a checkout by which planted bugs its code contains."""
    runs = 0

    def evaluate_dir(self, path, key, sha=""):
        if key in self.state.evaluations:
            return super().evaluate_dir(path, key, sha)
        SimulatedEvaluator.runs += 1
        tools = (path / "target_agent" / "tools.py").read_text(encoding="utf-8")
        agent = (path / "target_agent" / "agent.py").read_text(encoding="utf-8")
        schema_bug = '"required": ["q"]' in tools
        truncation_bug = "[:300]" in tools
        verify_bug = "double-check" in agent
        cases = []
        for i in range(8):
            failed = schema_bug or (truncation_bug and i < 6)
            cases.append({
                "id": f"case-{i}", "question": "(simulated)", "expect": [], "status": "ok",
                "passed": not failed,
                "answer": "(simulated) I could not retrieve the documents." if schema_bug else "(simulated)",
                "error": "", "tool_errors": 2 if schema_bug else 0,
                "tool_calls": (6 if verify_bug else 3) + i % 2, "turns": 3,
                "input_tokens": (9000 if verify_bug else 4000) + 100 * i, "output_tokens": 500,
                "cost_usd": (0.05 if verify_bug else 0.02) + 0.001 * i,
            })
        out = self.work / "results" / f"{key}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"cases": cases}), encoding="utf-8")
        m = from_cases(cases)
        m.source = "simulated"
        self.state.evaluations[key] = {**m.to_dict(), "sha": sha, "session": key, "results": str(out)}
        st.save(self.state, self.repo)
        self.state.log(f"{key}: pass {m.pass_rate:.0%}, errors {m.error_rate:.0%}, "
                       f"${m.cost_per_run:.4f}/run, {m.tool_calls_per_run:.1f} tool calls/run [simulated]")
        return m


REPAIRS = {
    "errors": {
        "edits": [{"path": "target_agent/tools.py", "find": '"q": {"type"', "replace": '"query": {"type"'},
                  {"path": "target_agent/tools.py", "find": '"required": ["q"]', "replace": '"required": ["query"]'}],
        "diagnosis": "The schema advertises `q` but the handler still reads args['query'], so every "
                     "search_docs call raises KeyError and the agent never sees a document.",
        "original_intent": "Save prompt tokens by shortening tool parameter names.",
        "how_intent_is_kept": "The saving is a single token per turn and changing the handler too would "
                              "break existing callers, so the schema goes back to `query`.",
        "commit_title": "Restore search_docs `query` parameter to match its handler",
    },
    "quality": {
        "edits": [{"path": "target_agent/tools.py", "find": '["body"][:300]', "replace": '["body"]'}],
        "diagnosis": "read_doc now cuts every document at 300 characters, before the facts the "
                     "questions ask about, so answers miss names and figures.",
        "original_intent": "Keep long documents from inflating input tokens on later turns.",
        "how_intent_is_kept": "Documents here are under 700 characters; the cap removes the content the "
                              "tool exists to return, so read_doc returns the full body again.",
        "commit_title": "Return full documents from read_doc",
    },
    "cost": {
        "edits": [{"path": "target_agent/agent.py",
                   "find": '"Before answering, double-check every fact: search for it again with different "\n',
                   "replace": '"Before answering, "\n'}],
        "diagnosis": "The prompt tells the agent to search again and re-read every source for each "
                     "fact, which doubles tool calls and input tokens on every question.",
        "original_intent": "Stop the agent citing the wrong figure.",
        "how_intent_is_kept": "The agent still has to read the full source before answering, which is "
                              "what grounds the figure; only the redundant second search is removed.",
        "commit_title": "Drop redundant re-verification loop from the research prompt",
    },
}

WRONG_COST_FIX = {
    "edits": [{"path": "target_agent/agent.py", "find": "MAX_TURNS = 12", "replace": "MAX_TURNS = 8"}],
    "diagnosis": "Runs take too many turns; capping turns will bound the cost.",
    "original_intent": "Stop the agent citing the wrong figure.",
    "how_intent_is_kept": "The verification instruction stays; the turn cap bounds it.",
    "commit_title": "Cap research agent turns at 8",
}


def simulated_fixer(calls: list[str]):
    def propose(prompt: str) -> dict:
        signature = prompt.split('signature="', 1)[1].split('"', 1)[0]
        calls.append(signature)
        fix = WRONG_COST_FIX if signature == "cost" and calls.count("cost") == 1 else REPAIRS[signature]
        return {**fix, "usage": {"model": "claude-opus-5-5", "input_tokens": 6000, "output_tokens": 900}}
    return propose


def offline_reader() -> NeatlogsReader:
    reader = NeatlogsReader()
    reader.token = reader.api_key = reader.project_id = None
    return reader


def run(dest: Path | None = None) -> tuple[Path, st.RunState]:
    repo = make_repo(dest or Path(tempfile.mkdtemp(prefix="agent-bisect-sim-")))
    fixer.propose_fix = simulated_fixer([])
    good = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    state = st.RunState(good=good, bad=_git(repo, "rev-parse", "HEAD"))
    state.log(f"simulation repo: {repo}")
    st.save(state, repo)
    evaluator = SimulatedEvaluator(repo, state, repo / "evals" / "cases.jsonl", offline_reader())
    orchestrator.Orchestrator(repo, state, evaluator).run()
    return repo, state
